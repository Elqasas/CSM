"""The Account table and its lookup tables.

Account carries three classifications that are deliberately independent of one
another: Class (strategic tier), Health Status (computed) and Size (operational
tier). Lifecycle Stage is a fourth, separate axis describing the commercial
journey. None of them feeds another's computation; in particular the scoring
engine never reads Class.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django_countries.fields import CountryField

from csm.core.enums import (
    AccountClass,
    AccountSize,
    CustomFieldType,
    Deployment,
    HealthStatus,
    LicenseType,
    LifecycleStage,
    MeetingFrequency,
    ReviewStatus,
    Role,
)
from csm.core.models import SoftDeleteModel, SoftDeleteQuerySet, TimeStampedModel


class Partner(TimeStampedModel):
    """Reseller or delivery partner.

    A lookup table rather than free text on Account: partner names arrive dirty
    from the spreadsheet, and grouping accounts by partner is a real need.
    """

    name = models.CharField(max_length=255, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "partners"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class AccountQuerySet(SoftDeleteQuerySet):
    def scorable(self) -> AccountQuerySet:
        """Accounts whose lifecycle stage means health is calculated."""
        from csm.core.enums import SCORABLE_STAGES

        return self.alive().filter(lifecycle_stage__in=list(SCORABLE_STAGES))

    def search(self, term: str) -> AccountQuerySet:
        """Global search. Account fields only: client name and client code.

        Contacts are deliberately excluded, per the specification.
        """
        term = (term or "").strip()
        if not term:
            return self.none()
        return self.filter(
            models.Q(client_name__icontains=term) | models.Q(client_code__icontains=term)
        )

    def with_renewal_quarter(self) -> AccountQuerySet:
        """Annotate the renewal quarter and year, derived from the contract end.

        Quarter and Years of Renewal are computed rather than stored: both are
        pure functions of the contract dates, so storing them would add two more
        things to keep in step on every edit for no read benefit.

        The aliases carry a suffix because ``renewal_quarter`` is already a
        property on the model. Annotating over it does not raise; it fails later
        and obscurely, when the row is materialised and Django tries to assign
        through a property with no setter. For a single account, read the
        property. Use this only to group or aggregate by quarter, where a
        property cannot reach.
        """
        return self.annotate(
            renewal_quarter_num=models.functions.ExtractQuarter("contract_end_date"),
            renewal_year_num=models.functions.ExtractYear("contract_end_date"),
        )


class Account(TimeStampedModel, SoftDeleteModel):
    # -- Identity and location ------------------------------------------------
    client_code = models.CharField(
        max_length=32,
        unique=True,
        help_text="Natural key. Used to match rows on Excel import.",
    )
    client_name = models.CharField(max_length=255)
    country = CountryField(blank=True)
    location = models.CharField(max_length=255, blank=True, default="")

    # -- Technical and contractual -------------------------------------------
    license_type = models.CharField(
        max_length=16,
        choices=LicenseType.choices,
        blank=True,
        default="",
        verbose_name="License",
        help_text="The product edition this client is on.",
    )
    deployment = models.CharField(
        max_length=8, choices=Deployment.choices, default=Deployment.CLOUD
    )
    is_managed_service = models.BooleanField(
        default=False,
        help_text="Selects the scoring factor set. Managed uses five factors, non-managed four.",
    )
    boq_users_count = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        verbose_name="BOQ users",
        help_text="Seats the client contracted for. Separate from the License edition.",
    )
    added_users_count = models.IntegerField(
        default=0,
        help_text=(
            "Seats added since the contract, or given back. Negative on a "
            "downgrade; the total may not go below zero."
        ),
    )
    total_users_count = models.GeneratedField(
        expression=models.F("boq_users_count") + models.F("added_users_count"),
        output_field=models.IntegerField(),
        db_persist=True,
        help_text="Derived. The dashboard sums this across the portfolio.",
    )

    # -- Contract and lifecycle ----------------------------------------------
    contract_start_date = models.DateField(null=True, blank=True)
    contract_end_date = models.DateField(null=True, blank=True, db_index=True)
    renewal_count = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="How many times this client has actually renewed. Not derivable.",
    )
    lifecycle_stage = models.CharField(
        max_length=16,
        choices=LifecycleStage.choices,
        default=LifecycleStage.ACTIVE,
        db_index=True,
        help_text="Defaults to Active, which is what a blank import cell becomes.",
    )
    stage_changed_at = models.DateTimeField(null=True, blank=True)

    scoring_baseline_date = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Floor for every day-count factor. Set to the import date at import, "
            "and reset whenever the account enters a scored stage from an unscored "
            "one, so a clean start never reads as neglect."
        ),
    )

    # -- Responsible team -----------------------------------------------------
    csm = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="csm_accounts",
    )
    acm = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="acm_accounts",
    )
    partner = models.ForeignKey(
        Partner, null=True, blank=True, on_delete=models.SET_NULL, related_name="accounts"
    )

    # -- Activity and engagement ---------------------------------------------
    quarterly_meeting_date = models.DateField(
        null=True, blank=True, help_text="The next scheduled quarterly meeting."
    )
    required_meeting_frequency = models.SmallIntegerField(
        null=True,
        blank=True,
        choices=MeetingFrequency.choices,
        help_text=(
            "Meetings per month, for Managed Service clients. While unset, the "
            "Meeting Attendance factor is omitted and the account is scored out of 80."
        ),
    )
    events_count = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    digital_content_count = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    has_event = models.GeneratedField(
        expression=models.Q(events_count__gt=0),
        output_field=models.BooleanField(),
        db_persist=True,
    )
    event_location = models.CharField(max_length=255, blank=True, default="")

    # -- Advocacy -------------------------------------------------------------
    g2_review_status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.NONE
    )
    g2_review_url = models.URLField(max_length=500, blank=True, default="")
    gartner_review_status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.NONE
    )
    gartner_review_url = models.URLField(max_length=500, blank=True, default="")

    # -- Classification -------------------------------------------------------
    account_class = models.CharField(
        max_length=4,
        choices=AccountClass.choices,
        default=AccountClass.C,
        db_index=True,
        verbose_name="Class",
        help_text="Prioritisation only. Never read by the scoring engine.",
    )
    size = models.CharField(
        max_length=12, choices=AccountSize.choices, default=AccountSize.MID
    )

    # -- Links, media and notes ----------------------------------------------
    website_url = models.URLField(max_length=500, blank=True, default="")
    tenant_url = models.URLField(max_length=500, blank=True, default="")
    drive_url = models.URLField(max_length=500, blank=True, default="")
    linkedin_url = models.URLField(max_length=500, blank=True, default="")
    description = models.TextField(blank=True, default="")
    logo = models.ImageField(upload_to="account_logos/", null=True, blank=True)

    # -- Cached health projection --------------------------------------------
    # Written only by csm.health.services. Cached rather than derived because the
    # dashboard filter and the account list sort on it.
    computed_health_score = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    computed_health_max = models.SmallIntegerField(null=True, blank=True)
    # NULL here means the score has never been calculated, which is distinct
    # from every listed status, so the empty string would be a second, ambiguous
    # way of saying the same thing.
    computed_health_status = models.CharField(
        max_length=16, choices=HealthStatus.choices, null=True, blank=True
    )
    effective_health_status = models.CharField(
        max_length=16,
        choices=HealthStatus.choices,
        null=True,
        blank=True,
        db_index=True,
        help_text="The computed status, unless an active override replaces it.",
    )
    health_calculated_at = models.DateTimeField(null=True, blank=True)
    red_since = models.DateField(
        null=True,
        blank=True,
        help_text="When the account most recently entered At Risk. Keys the health alert episode.",
    )
    health_alert_suppressed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when a user closes a health alert while still red. Cleared on recovery.",
    )

    # -- Provenance -----------------------------------------------------------
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    imported_at = models.DateTimeField(null=True, blank=True)

    objects = AccountQuerySet.as_manager()

    class Meta:
        db_table = "accounts"
        ordering = ["client_name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(boq_users_count__gte=0),
                name="accounts_boq_users_non_negative",
            ),
            # Added users may be negative - a downgrade gives seats back - but
            # the two together may not be, because nobody has fewer than no
            # seats. Written as a comparison against the BOQ so the database
            # enforces the sum rather than the application remembering to.
            models.CheckConstraint(
                condition=models.Q(
                    added_users_count__gte=models.F("boq_users_count") * -1
                ),
                name="accounts_total_users_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(contract_end_date__isnull=True)
                | models.Q(contract_start_date__isnull=True)
                | models.Q(contract_end_date__gte=models.F("contract_start_date")),
                name="accounts_contract_end_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(required_meeting_frequency__isnull=True)
                | models.Q(required_meeting_frequency__in=[1, 2, 4]),
                name="accounts_meeting_frequency_allowed",
            ),
            models.CheckConstraint(
                condition=models.Q(computed_health_max__isnull=True)
                | models.Q(computed_health_max__in=[80, 100]),
                name="accounts_health_max_allowed",
            ),
        ]
        indexes = [
            models.Index(fields=["csm"]),
            models.Index(fields=["acm"]),
            models.Index(fields=["client_name"]),
            models.Index(fields=["lifecycle_stage", "effective_health_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.client_code} — {self.client_name}"

    # -- Derived values, computed rather than stored --------------------------

    @property
    def renewal_quarter(self) -> int | None:
        """Calendar quarter of the contract end date."""
        if not self.contract_end_date:
            return None
        return (self.contract_end_date.month - 1) // 3 + 1

    @property
    def contract_term_years(self) -> float | None:
        """Length of the current contract in years."""
        if not (self.contract_start_date and self.contract_end_date):
            return None
        return round((self.contract_end_date - self.contract_start_date).days / 365.25, 2)

    def days_to_renewal(self, as_of: dt.date) -> int | None:
        if not self.contract_end_date:
            return None
        return (self.contract_end_date - as_of).days

    @property
    def is_scorable(self) -> bool:
        from csm.core.enums import SCORABLE_STAGES

        return self.lifecycle_stage in SCORABLE_STAGES

    @property
    def meeting_frequency_missing(self) -> bool:
        """A managed account with no frequency set is scored out of 80."""
        return self.is_managed_service and self.required_meeting_frequency is None

    def clean(self):
        from django.core.exceptions import ValidationError

        errors = {}
        if self.csm_id and self.csm.role != Role.CSM:
            errors["csm"] = "The assigned CSM must be a user with the CSM role."
        if self.acm_id and self.acm.role != Role.ACM:
            errors["acm"] = "The assigned ACM must be a user with the ACM role."
        if errors:
            raise ValidationError(errors)


class AccountCustomFieldDefinition(TimeStampedModel):
    """A field an administrator has named, not one built into the schema.

    Three renderings, not an open type system: the request behind this was
    "let me add a field and say what kind it is", not a schema builder.
    Deactivating rather than deleting keeps a field's past values readable on
    the accounts that already carry one, the same reasoning
    ``Partner.is_active`` already uses.
    """

    name = models.CharField(max_length=100, unique=True)
    field_type = models.CharField(
        max_length=8, choices=CustomFieldType.choices, default=CustomFieldType.TEXT
    )
    #: One option per line. Only read when ``field_type`` is DROPDOWN - kept
    #: rather than cleared if the type is ever changed away from it, so
    #: switching back does not lose the list that was typed in.
    choices_text = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        db_table = "account_custom_field_definitions"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def choice_list(self) -> list[str]:
        return [line.strip() for line in self.choices_text.splitlines() if line.strip()]


class AccountCustomFieldValue(TimeStampedModel):
    """One field's value on one account."""

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="custom_field_values"
    )
    definition = models.ForeignKey(
        AccountCustomFieldDefinition, on_delete=models.CASCADE, related_name="values"
    )
    value = models.TextField(blank=True, default="")

    class Meta:
        db_table = "account_custom_field_values"
        constraints = [
            models.UniqueConstraint(
                fields=["account", "definition"], name="one_value_per_field_per_account"
            )
        ]

    def __str__(self) -> str:
        return f"{self.definition.name}={self.value!r} on {self.account.client_code}"
