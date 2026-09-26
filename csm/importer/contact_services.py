"""Validating and applying a contacts batch.

Same shape as the accounts side: stage, preview, then write the staged rows
without re-reading the file.

Matching is by ``(account, full_name)``. Email would be the better key and is
not usable: the spreadsheet these files come from leaves it blank often enough
that matching on it would create a second copy of everyone who has no address on
record. Name is imperfect - two people at one client can share one - but the
failure is visible in the preview, where a row says "update" against a contact
the reader can see.
"""

from __future__ import annotations

from django.db import transaction

from csm.accounts.models import Account
from csm.contacts.models import Contact, ContactChannel
from csm.core import clock
from csm.importer.contact_parsing import parse_contacts
from csm.importer.models import ImportBatch, ImportRow, ImportStatus, RowAction
from csm.importer.parsing import WorkbookError


@transaction.atomic
def validate(batch: ImportBatch) -> ImportBatch:
    batch.rows.all().delete()
    batch.parse_error = ""

    known = dict(Account.objects.alive().values_list("client_code", "client_name"))

    try:
        with batch.upload.open("rb") as handle:
            parsed, unknown = parse_contacts(
                handle, known_codes=set(known),
                filename=batch.original_name or batch.upload.name,
            )
    except WorkbookError as exc:
        batch.status = ImportStatus.FAILED
        batch.parse_error = str(exc)
        batch.save(update_fields=["status", "parse_error", "updated_at"])
        return batch

    # Which rows update an existing contact rather than adding one. Resolved
    # here so the preview can say so, rather than at write time when it is too
    # late to be told.
    existing = {
        (code.lower(), name.lower())
        for code, name in Contact.objects.filter(
            account__in=Account.objects.alive()
        ).values_list("account__client_code", "full_name")
    }

    staged = []
    for row in parsed:
        action = RowAction.CREATE
        key = (row.client_code.lower(), row.full_name.lower())
        if row.client_code and row.full_name and key in existing:
            action = RowAction.UPDATE
            row.warn("Full name", "This client already has a contact by this name; "
                                  "the row will update them.")

        staged.append(
            ImportRow(
                batch=batch,
                row_number=row.row_number,
                client_code=row.client_code[:64],
                client_name=known.get(row.client_code, row.full_name)[:255],
                action=action,
                cleaned=row.cleaned,
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


@transaction.atomic
def apply(batch: ImportBatch, *, actor) -> dict:
    """Write every importable staged contact row. One transaction."""
    if batch.status != ImportStatus.VALIDATED:
        raise ValueError("Only a validated batch can be applied.")

    accounts = dict(Account.objects.alive().values_list("client_code", "pk"))

    created = updated = 0
    for row in batch.rows.filter(errors=[]).exclude(action=RowAction.SKIP):
        account_id = accounts.get(row.client_code)
        if account_id is None:
            continue  # the account was deleted between preview and apply

        cleaned = dict(row.cleaned)
        channels = cleaned.pop("channels", [])

        contact, was_created = Contact.objects.update_or_create(
            account_id=account_id,
            full_name=cleaned.pop("full_name"),
            defaults=cleaned,
        )
        created += int(was_created)
        updated += int(not was_created)

        _sync_channels(contact, channels)

        row.contact = contact
        row.save(update_fields=["contact"])

    batch.status = ImportStatus.APPLIED
    batch.applied_at = clock.now()
    batch.applied_by = actor
    batch.save(update_fields=["status", "applied_at", "applied_by", "updated_at"])

    return {"created": created, "updated": updated}


def _sync_channels(contact, channels) -> None:
    """Replace the contact's channels with the ones in the file.

    Replaced rather than merged: the file is the statement of how this person is
    reachable, and merging would make a removed channel impossible to express.
    An empty cell is not a statement, so it leaves the existing set alone.
    """
    if not channels:
        return

    wanted = set(channels)
    existing = set(contact.channels.values_list("channel", flat=True))

    contact.channels.exclude(channel__in=wanted).delete()
    ContactChannel.objects.bulk_create(
        [ContactChannel(contact=contact, channel=c) for c in wanted - existing]
    )
