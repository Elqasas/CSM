"""Health inputs the specification assumes but never defines storage for.

The scoring engine reads open ticket counts, CSAT ratings and adoption events.
None of the three appears in the spec's data model, so they are defined here.
CSAT and adoption are manual entry. Tickets come from Jira Service Management
once it is configured (``csm/integrations/jira``) and are entered by hand only
until then; a synced ticket carries its issue key and has no author.

EngagementItem exists for a different reason: dashboard card 11 asks for an
events and digital-content trend over time, which a scalar count cannot produce.
The imported scalars stay on Account as a pre-import baseline.
"""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from csm.accounts.models import Account
from csm.contacts.models import Contact
from csm.core.enums import (
    AdoptionEventType,
    CsatSource,
    EngagementItemType,
    TicketKind,
    TicketSource,
    TicketStatus,
)


class TicketQuerySet(models.QuerySet):
    def open(self) -> TicketQuerySet:
        return self.filter(status=TicketStatus.OPEN)


class Ticket(models.Model):
    """Feeds the Open Tickets factor: weighted score = bugs x 3 + general x 1."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="tickets")
    ticket_kind = models.CharField(max_length=8, choices=TicketKind.choices)
    status = models.CharField(
        max_length=8, choices=TicketStatus.choices, default=TicketStatus.OPEN
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    opened_on = models.DateField()
    closed_on = models.DateField(null=True, blank=True)

    source = models.CharField(
        max_length=8, choices=TicketSource.choices, default=TicketSource.MANUAL
    )
    external_key = models.CharField(
        max_length=64, blank=True, default="", help_text="The Jira issue key, e.g. SUP-123."
    )
    external_type = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Jira's own issue type name. ticket_kind is what it scores as.",
    )
    external_status = models.CharField(
        max_length=64, blank=True, default="", help_text="Jira's own status name, for display."
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Null means the ticket was synced from Jira.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TicketQuerySet.as_manager()

    class Meta:
        db_table = "tickets"
        ordering = ["-opened_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(status=TicketStatus.CLOSED, closed_on__isnull=False)
                    | ~models.Q(status=TicketStatus.CLOSED) & models.Q(closed_on__isnull=True)
                ),
                name="tickets_closed_on_matches_status",
            ),
            # One row per issue per account. An issue raised under two JSM
            # organizations belongs to both clients, so the account is part of
            # the key; without the index two overlapping sync runs could both
            # find nothing and both insert.
            models.UniqueConstraint(
                fields=["source", "external_key", "account"],
                condition=~models.Q(external_key=""),
                name="tickets_one_row_per_issue_per_account",
            ),
        ]
        indexes = [
            models.Index(
                fields=["account"],
                condition=models.Q(status=TicketStatus.OPEN),
                name="tickets_open_by_account_idx",
            )
        ]

    def __str__(self) -> str:
        return f"[{self.ticket_kind}] {self.title}"

    @property
    def is_synced(self) -> bool:
        """Jira owns this row: the app neither closes nor deletes it."""
        return self.source != TicketSource.MANUAL


class CsatResponse(models.Model):
    """Feeds the CSAT factor. Manual assessment by a CSM or ACM in this phase."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="csat_responses")
    contact = models.ForeignKey(
        Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="csat_responses"
    )
    score = models.SmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="1 to 5. Four or five is green, three is yellow, one or two is red.",
    )
    recorded_on = models.DateField()
    comment = models.TextField(blank=True, default="")
    source = models.CharField(max_length=8, choices=CsatSource.choices, default=CsatSource.MANUAL)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "csat_responses"
        ordering = ["-recorded_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(score__gte=1) & models.Q(score__lte=5),
                name="csat_score_between_1_and_5",
            )
        ]
        indexes = [models.Index(fields=["account", "-recorded_on"])]

    def __str__(self) -> str:
        return f"CSAT {self.score} on {self.recorded_on}"


class AdoptionEvent(models.Model):
    """Feeds the Monthly Adoption factor.

    Either a phishing campaign or a course assignment qualifies; the factor
    measures days since the most recent of either.
    """

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="adoption_events")
    event_type = models.CharField(max_length=20, choices=AdoptionEventType.choices)
    occurred_on = models.DateField()
    title = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "adoption_events"
        ordering = ["-occurred_on", "-id"]
        indexes = [models.Index(fields=["account", "-occurred_on"])]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} on {self.occurred_on}"


class EngagementItem(models.Model):
    """A dated event or piece of digital content, for the dashboard trend."""

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="engagement_items"
    )
    item_type = models.CharField(max_length=16, choices=EngagementItemType.choices)
    occurred_on = models.DateField()
    title = models.CharField(max_length=255, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    quantity = models.SmallIntegerField(default=1, validators=[MinValueValidator(1)])

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "engagement_items"
        ordering = ["-occurred_on", "-id"]
        indexes = [models.Index(fields=["occurred_on", "item_type"])]

    def __str__(self) -> str:
        return f"{self.get_item_type_display()} on {self.occurred_on}"
