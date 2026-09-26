"""Validating and applying an import batch.

``validate`` parses the uploaded workbook into staged rows. ``apply`` writes the
staged rows and nothing else - it re-reads no file, so what was previewed is
what is written.

Applying is one transaction. A sheet that fails half way through would otherwise
leave a portfolio that is neither the old one nor the new one, and the natural
recovery - upload it again - would double whatever had already landed.
"""

from __future__ import annotations

from django.db import transaction

from csm.accounts.models import (
    Account,
    AccountCustomFieldDefinition,
    AccountCustomFieldValue,
    Partner,
)
from csm.core import clock
from csm.core.events import ImportApplied, publish
from csm.health.services import recalculate
from csm.importer.models import ImportBatch, ImportRow, ImportStatus, RowAction
from csm.importer.parsing import WorkbookError, active_custom_fields, parse
from csm.users.models import User

#: Warnings that describe what a *new* account defaults to. On an update whose
#: sheet lacks the column the stored value is kept, so the warning is untrue.
_DEFAULTING_WARNINGS = {
    "Lifecycle stage": "lifecycle_stage",
    "Required meeting frequency": "required_meeting_frequency",
    "CSM": "csm_id",
}

#: Keys held on a staged row that are not Account fields.
NON_FIELD_KEYS = ("partner_name", "custom_fields")


def _people() -> dict:
    """Active staff indexed by email and by folded name.

    A name maps to a *list*, because two people can share one and the parser has
    to be able to tell that it is ambiguous rather than take the first.
    """
    from csm.importer.parsing import normalise_header

    by_email = {}
    by_name: dict[str, list] = {}
    for user in User.objects.filter(is_active=True).only("id", "email", "full_name"):
        by_email[user.email.lower()] = user
        by_name.setdefault(normalise_header(user.full_name), []).append(user)
    return {"by_email": by_email, "by_name": by_name}


@transaction.atomic
def validate(batch: ImportBatch) -> ImportBatch:
    """Parse the workbook into staged rows and mark the batch validated."""
    batch.rows.all().delete()
    batch.parse_error = ""

    try:
        with batch.upload.open("rb") as handle:
            # The filename is what picks the reader: without it every upload
            # goes down the xlsx path and a CSV fails as "not a zip file".
            parsed, unknown = parse(
                handle,
                people=_people(),
                filename=batch.original_name or batch.upload.name,
                custom_fields=active_custom_fields(),
            )
    except WorkbookError as exc:
        batch.status = ImportStatus.FAILED
        batch.parse_error = str(exc)
        batch.save(update_fields=["status", "parse_error", "updated_at"])
        return batch

    # The default manager is unfiltered, so this deliberately sees soft-deleted
    # accounts too. ``client_code`` is unique at the database level whether or
    # not a row is deleted, so a code that belongs to a deleted account cannot
    # simply be created again - and updating it without saying so would import
    # an account that stays invisible everywhere, which looks exactly like the
    # import having silently skipped it.
    existing = dict(Account.objects.values_list("client_code", "pk"))
    deleted_codes = set(
        Account.objects.dead().values_list("client_code", flat=True)
    )

    staged = []
    for row in parsed:
        action = RowAction.CREATE
        if row.client_code and row.client_code in existing:
            action = RowAction.UPDATE
            row.warnings = [
                w for w in row.warnings
                if _DEFAULTING_WARNINGS.get(w["column"], "client_code") in row.cleaned
            ]
            if row.client_code in deleted_codes:
                row.warn(
                    "Client code",
                    "This code belongs to a deleted account. Importing restores it.",
                )
            else:
                row.warn(
                    "Client code",
                    "An account with this code already exists; the row will update it.",
                )

        staged.append(
            ImportRow(
                batch=batch,
                row_number=row.row_number,
                client_code=row.client_code[:64],
                client_name=row.client_name[:255],
                action=action,
                cleaned=_json_safe(row.cleaned),
                errors=row.errors,
                warnings=row.warnings,
            )
        )

    ImportRow.objects.bulk_create(staged)

    batch.status = ImportStatus.VALIDATED
    batch.unknown_columns = unknown
    batch.save(
        update_fields=["status", "parse_error", "unknown_columns", "updated_at"]
    )
    return batch


def _json_safe(cleaned: dict) -> dict:
    """Dates are not JSON, and the staged row has to survive a round trip."""
    out = {}
    for key, value in cleaned.items():
        out[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


def _restore_dates(cleaned: dict) -> dict:
    import datetime as dt

    out = dict(cleaned)
    for key in ("contract_start_date", "contract_end_date", "quarterly_meeting_date"):
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = dt.date.fromisoformat(value)
    return out


@transaction.atomic
def apply(batch: ImportBatch, *, actor) -> dict:
    """Write every importable staged row. One transaction, no re-parsing."""
    if batch.status != ImportStatus.VALIDATED:
        raise ValueError("Only a validated batch can be applied.")

    created = updated = 0
    touched = []
    live_definitions = set(
        AccountCustomFieldDefinition.objects.values_list("pk", flat=True)
    )

    for row in batch.rows.filter(errors=[]).exclude(action=RowAction.SKIP):
        cleaned = _restore_dates(row.cleaned)
        partner_name = cleaned.pop("partner_name", "")
        custom_values = cleaned.pop("custom_fields", {})
        for key in NON_FIELD_KEYS:
            cleaned.pop(key, None)

        if partner_name:
            cleaned["partner"] = Partner.objects.get_or_create(name=partner_name)[0]

        # The import baseline: every day-count factor starts from the import
        # date, so an account with no history behind it does not read as months
        # of neglect on its first evening.
        cleaned["scoring_baseline_date"] = batch.baseline_date
        cleaned["imported_at"] = clock.now()

        # Restoring rather than resurrecting-in-place: an updated row that kept
        # its deleted_at would import an account invisible to every list in the
        # application, which is indistinguishable from the import having failed.
        cleaned["deleted_at"] = None
        cleaned["deleted_by"] = None

        account, was_created = Account.objects.update_or_create(
            client_code=cleaned.pop("client_code"), defaults=cleaned
        )
        if was_created:
            account.created_by = actor
            account.save(update_fields=["created_by"])
            created += 1
        else:
            updated += 1

        # Keyed by definition pk as staged. A definition deleted between preview
        # and apply has nothing left to hold a value, so it is skipped.
        for definition_id, value in custom_values.items():
            if int(definition_id) not in live_definitions:
                continue
            AccountCustomFieldValue.objects.update_or_create(
                account=account,
                definition_id=int(definition_id),
                defaults={"value": value},
            )

        row.account = account
        row.save(update_fields=["account"])
        touched.append(account)

    batch.status = ImportStatus.APPLIED
    batch.applied_at = clock.now()
    batch.applied_by = actor
    batch.save(update_fields=["status", "applied_at", "applied_by", "updated_at"])

    # Scored after every row is written, not per row: an account's factors read
    # its own data only, but scoring inside the loop would run against a
    # half-imported portfolio and write history rows nobody asked for.
    scored = 0
    for account in touched:
        if recalculate(account) is not None:
            scored += 1

    publish(
        ImportApplied(
            batch_id=batch.pk,
            account_ids=tuple(a.pk for a in touched),
            baseline_date=batch.baseline_date,
        )
    )

    return {
        "created": created,
        "updated": updated,
        "scored": scored,
    }
