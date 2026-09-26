"""The activity log.

Append-only. There is no edit view and no delete: a correction is a void plus a
fresh entry, so the history a CSM reads is the history that was actually
recorded. Voided rows are excluded from every scoring query.

``occurred_on`` is a date rather than a timestamp on purpose. Every scoring
factor is a day count, and storing a date removes an entire class of timezone
off-by-one bugs.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from csm.accounts.models import Account
from csm.contacts.models import Contact
from csm.core.enums import (
    GENERAL_CONTACT_TYPES,
    WEEKLY_CONTACT_TYPES,
    ActivitySource,
    ActivityType,
)


class ActivityQuerySet(models.QuerySet):
    def live(self) -> ActivityQuerySet:
        return self.filter(is_voided=False)

    def counting_for_general_contact(self) -> ActivityQuerySet:
        return self.live().filter(activity_type__in=[t.value for t in GENERAL_CONTACT_TYPES])

    def counting_for_weekly_contact(self) -> ActivityQuerySet:
        """Excludes meetings, which are counted only under Meeting Attendance."""
        return self.live().filter(activity_type__in=[t.value for t in WEEKLY_CONTACT_TYPES])

    def meetings(self) -> ActivityQuerySet:
        return self.live().filter(activity_type=ActivityType.MEETING)


class Activity(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="activities")
    task = models.OneToOneField(
        "tasks.Task",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="logged_activity",
        help_text=(
            "The task, meeting or call whose completion wrote this row. Null for "
            "anything recorded by an integration or a backfill."
        ),
    )
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activities",
        help_text="Optional, but shown prominently in the form to encourage use.",
    )

    activity_type = models.CharField(max_length=12, choices=ActivityType.choices)
    occurred_on = models.DateField(db_index=True)
    subject = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    source = models.CharField(
        max_length=12,
        choices=ActivitySource.choices,
        default=ActivitySource.MANUAL,
        help_text="Reserved. Outlook and Teams sync will write through this field.",
    )
    # NULL rather than blank, because the partial unique index below skips
    # NULL rows and would otherwise collide on every manually logged activity.
    external_id = models.CharField(max_length=128, null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    is_voided = models.BooleanField(default=False)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=255, blank=True, default="")

    objects = ActivityQuerySet.as_manager()

    class Meta:
        db_table = "activities"
        ordering = ["-occurred_on", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                condition=models.Q(external_id__isnull=False),
                name="activities_external_id_unique",
            ),
        ]
        indexes = [
            # These three drive every scoring query.
            models.Index(
                fields=["account", "-occurred_on"],
                condition=models.Q(is_voided=False),
                name="act_account_date_live_idx",
            ),
            models.Index(
                fields=["account", "activity_type", "-occurred_on"],
                condition=models.Q(is_voided=False),
                name="act_account_type_date_idx",
            ),
            models.Index(fields=["contact", "-occurred_on"], name="act_contact_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_activity_type_display()} on {self.occurred_on}"
