"""Health history and overrides."""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MinLengthValidator
from django.db import models

from csm.accounts.models import Account
from csm.core.enums import (
    FactorSetId,
    HealthStatus,
    OverrideClearedReason,
    ScoreTrigger,
)


class HealthScoreHistory(models.Model):
    """One row for every calculation, from either trigger.

    The score is stored as a decimal rather than an integer because a
    non-managed account with three green factors and one yellow is exactly
    87.50. Rounding at write time would corrupt the override rule, which turns
    exact numeric equality into a business decision.
    """

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="health_history"
    )
    calculated_at = models.DateTimeField(auto_now_add=True)
    calculated_on = models.DateField(
        help_text="Business-local date, denormalised so the trend chart groups by day."
    )
    trigger = models.CharField(max_length=16, choices=ScoreTrigger.choices)

    score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    max_score = models.SmallIntegerField(null=True, blank=True)
    computed_status = models.CharField(max_length=16, choices=HealthStatus.choices)
    effective_status = models.CharField(max_length=16, choices=HealthStatus.choices)
    override_active = models.BooleanField(default=False)

    factor_set = models.CharField(
        max_length=12, choices=FactorSetId.choices, null=True, blank=True,
        help_text="Snapshotted so history stays readable after a client changes type.",
    )
    engine_version = models.CharField(max_length=16, blank=True, default="")
    breakdown = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "health_score_history"
        ordering = ["-calculated_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(score__isnull=True)
                | (models.Q(score__gte=0) & models.Q(score__lte=100)),
                name="health_history_score_in_range",
            ),
            models.CheckConstraint(
                condition=models.Q(max_score__isnull=True) | models.Q(max_score__in=[80, 100]),
                name="health_history_max_score_allowed",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "-calculated_at"]),
            models.Index(fields=["calculated_on"]),
        ]

    def __str__(self) -> str:
        return f"{self.account_id} {self.calculated_on} {self.effective_status}"


class HealthStatusOverride(models.Model):
    """A manual health status, with the anchor that invalidates it.

    Stored as a table rather than columns on Account so the governance trail
    survives: who set it, why, when, and what cleared it.

    ``score_at_override`` is the computed score at the instant the override was
    created. Anchoring here rather than to the previous history row means the
    rule reads from one row and depends on no history ordering and on no job
    having run.
    """

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="health_overrides"
    )
    status = models.CharField(max_length=16, choices=HealthStatus.choices)
    reason = models.TextField(
        validators=[MinLengthValidator(10)],
        help_text="Mandatory. Recorded so a later reader knows why the number was set aside.",
    )

    score_at_override = models.DecimalField(max_digits=5, decimal_places=2)
    max_at_override = models.SmallIntegerField()

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="health_overrides"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    cleared_at = models.DateTimeField(null=True, blank=True)
    cleared_reason = models.CharField(
        max_length=24, choices=OverrideClearedReason.choices, null=True, blank=True
    )
    cleared_score = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    cleared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        db_table = "health_status_overrides"
        ordering = ["-created_at"]
        constraints = [
            # At most one active override per account, enforced by the database
            # rather than by an application check that could race.
            models.UniqueConstraint(
                fields=["account"],
                condition=models.Q(cleared_at__isnull=True),
                name="health_one_active_override_per_account",
            ),
            models.CheckConstraint(
                condition=~models.Q(status=HealthStatus.NOT_APPLICABLE),
                name="health_override_status_must_be_scored",
            ),
        ]

    def __str__(self) -> str:
        state = "active" if self.cleared_at is None else "cleared"
        return f"Override {self.status} on account {self.account_id} ({state})"

    @property
    def is_active(self) -> bool:
        return self.cleared_at is None
