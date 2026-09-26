"""The serialization boundary.

Templates and JSON endpoints render accounts through a presenter rather than
touching the model. When a user may not see health information, the presenter
*raises* instead of quietly returning None.

That choice is deliberate. A silent None looks like "no score yet" and would sail
through review, through tests, and into a page an ACM can read. An exception
fails loudly the first time anyone writes the leak, which is while they are
still looking at it.
"""

from __future__ import annotations

from functools import cached_property

from django.core.exceptions import PermissionDenied

from csm.core.access import capabilities as cap
from csm.core.enums import HealthStatus


class AccountPresenter:
    """An account, rendered only as far as this user is permitted to see it."""

    #: Everything a presenter will expose without a health capability.
    SAFE_FIELDS = (
        "id",
        "client_code",
        "client_name",
        "country",
        "location",
        "license_type",
        "account_class",
        "size",
        "lifecycle_stage",
        "is_managed_service",
        "deployment",
        "contract_start_date",
        "contract_end_date",
        "renewal_count",
        # The seat counts. Commercial, not health: how many people the client
        # bought for says nothing about how the relationship is going.
        "boq_users_count",
        "added_users_count",
        "total_users_count",
        "description",
        "tenant_url",
        "drive_url",
        "website_url",
        "linkedin_url",
        # Who is responsible. No health signal: these say who owns the
        # relationship, not how it is going.
        "csm",
        "acm",
        "partner",
        "quarterly_meeting_date",
        "logo",
    )

    def __init__(self, account, ability):
        self._account = account
        self._ability = ability

    def __getattr__(self, name):
        if name in type(self).SAFE_FIELDS:
            return getattr(self._account, name)
        raise AttributeError(
            f"{name!r} is not exposed by AccountPresenter. Add it to SAFE_FIELDS "
            "if it carries no health signal, or go through a health property."
        )

    # -- Health, gated ----------------------------------------------------

    def _require_health(self) -> None:
        if not self._ability.can(cap.HEALTH_VIEW):
            raise PermissionDenied(
                "This user may not see health information for any account."
            )

    @property
    def health_status(self) -> str | None:
        self._require_health()
        return self._account.effective_health_status

    @property
    def health_status_label(self) -> str:
        self._require_health()
        value = self._account.effective_health_status
        return HealthStatus(value).label if value else "Not calculated"

    @property
    def health_score(self):
        self._require_health()
        return self._account.computed_health_score

    @property
    def health_max(self):
        self._require_health()
        return self._account.computed_health_max

    @property
    def health_calculated_at(self):
        self._require_health()
        return self._account.health_calculated_at

    @property
    def red_since(self):
        self._require_health()
        return self._account.red_since

    @cached_property
    def health_override(self):
        """The active manual override, or None.

        Gated like every other health attribute: an override *is* a health
        status, and a page that may not show the score must not show the
        judgement that replaced it either.

        Cached because a template reads it several times over - once to decide
        whether to render the banner, then for each line inside it - and
        deliberately not prefetched on the account list: the only surfaces that
        ask for it show one account, and a per-row call from a list template
        would be a query per row.
        """
        from csm.health.services import active_override

        self._require_health()
        return active_override(self._account)

    @property
    def is_overridden(self) -> bool:
        self._require_health()
        return self.health_override is not None

    @property
    def computed_health_status_label(self) -> str:
        """What the arithmetic says, as opposed to what is being shown.

        Only interesting while an override is active, where the difference
        between the two is the whole point of saying so.
        """
        self._require_health()
        value = self._account.computed_health_status
        return HealthStatus(value).label if value else "Not calculated"

    @property
    def meeting_frequency_missing(self) -> bool:
        """Whether the score is capped because the meeting frequency is unset."""
        self._require_health()
        return self._account.meeting_frequency_missing

    # -- Display labels ---------------------------------------------------
    # Templates need the human label, not the stored value. These go through
    # the presenter rather than letting a template reach past it to the model,
    # which is the one habit that would make the whole boundary decorative.

    @property
    def account_class_label(self) -> str:
        return self._account.get_account_class_display()

    @property
    def lifecycle_stage_label(self) -> str:
        return self._account.get_lifecycle_stage_display()

    @property
    def deployment_label(self) -> str:
        return self._account.get_deployment_display()

    @property
    def license_label(self) -> str:
        return self._account.get_license_type_display() or "Not set"

    @property
    def size_label(self) -> str:
        return self._account.get_size_display()

    @property
    def country_label(self) -> str:
        country = self._account.country
        return country.name if country else ""

    @cached_property
    def custom_fields(self) -> list[tuple[str, str]]:
        """Admin-defined fields with a value on this account, in field order.

        A relation rather than a name in ``SAFE_FIELDS``, and carries no health
        signal - it's free text an administrator named, the same as
        ``description`` already on this list.
        """
        import contextlib
        import datetime as dt

        from csm.core.enums import CustomFieldType

        rows = []
        for value in (
            self._account.custom_field_values.select_related("definition")
            .filter(definition__is_active=True, value__gt="")
            .order_by("definition__name")
        ):
            shown = value.value
            if value.definition.field_type == CustomFieldType.DATETIME:
                with contextlib.suppress(ValueError):
                    shown = dt.datetime.fromisoformat(value.value).strftime("%d %b %Y %H:%M")
            rows.append((value.definition.name, shown))
        return rows

    # -- Convenience ------------------------------------------------------

    @property
    def shows_health(self) -> bool:
        """Safe for a template to ask before rendering a health block."""
        return self._ability.can(cap.HEALTH_VIEW)

    @property
    def can_edit(self) -> bool:
        return self._ability.can_edit_account(self._account)

    @property
    def can_delete(self) -> bool:
        return self._ability.can_delete_account(self._account)

    #: What the row-actions menu can offer. Asked as a set so the template can
    #: decide whether the menu is worth rendering at all without spelling the
    #: same eight conditions twice.
    ROW_ACTION_CAPABILITIES = (
        cap.TASK_CREATE,
        cap.ACTIVITY_CREATE,
        cap.TICKET_EDIT,
        cap.ADOPTION_RECORD,
        cap.CSAT_RECORD,
        cap.CONTACT_EDIT,
    )

    @property
    def has_row_actions(self) -> bool:
        """Whether this reader can do anything at all from a table row.

        A menu button that opens an empty list is worse than no button: it
        promises something and then refuses. A manager, who may read everything
        and change nothing, gets no button.
        """
        return (
            self.can_edit
            or self.can_delete
            or any(self._ability.can(name) for name in self.ROW_ACTION_CAPABILITIES)
        )

    def as_dict(self) -> dict:
        """A JSON-safe projection carrying only what this user may receive."""
        data = {name: getattr(self._account, name) for name in type(self).SAFE_FIELDS}
        data["country"] = str(data["country"])
        if self.shows_health:
            data["health_status"] = self._account.effective_health_status
            data["health_score"] = (
                str(self._account.computed_health_score)
                if self._account.computed_health_score is not None
                else None
            )
            data["health_max"] = self._account.computed_health_max
        return data

    def __str__(self) -> str:
        return str(self._account)
