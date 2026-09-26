"""The only database access in a health calculation.

Kept apart from the engine so that the scoring logic itself stays pure. This
module reads; ``engine.score`` decides.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.db.models import Count, Q

from csm.accounts.models import Account
from csm.activities.models import Activity
from csm.core.enums import TicketKind, TicketStatus
from csm.engagement.models import AdoptionEvent, CsatResponse, Ticket
from csm.health import config
from csm.health.engine import ScoringInput


def baseline_for(account: Account, as_of: date) -> date:
    """The floor for every day-count factor.

    Falls back to today rather than to the epoch when unset, so a missing
    baseline reads as a clean start rather than as years of neglect.
    """
    return account.scoring_baseline_date or as_of


def gather_inputs(account: Account, as_of: date) -> ScoringInput:
    """Read everything the engine needs for one account."""
    baseline = baseline_for(account, as_of)

    ticket_counts = Ticket.objects.filter(
        account=account, status=TicketStatus.OPEN
    ).aggregate(
        bugs=Count("id", filter=Q(ticket_kind=TicketKind.BUG)),
        general=Count("id", filter=Q(ticket_kind=TicketKind.GENERAL)),
    )
    has_ticket_data = Ticket.objects.filter(account=account).exists()

    last_adoption = (
        AdoptionEvent.objects.filter(account=account, occurred_on__lte=as_of)
        .order_by("-occurred_on")
        .values_list("occurred_on", flat=True)
        .first()
    )

    latest_csat = (
        CsatResponse.objects.filter(account=account, recorded_on__lte=as_of)
        .order_by("-recorded_on", "-id")
        .values("score", "recorded_on")
        .first()
    )

    activities = Activity.objects.filter(account=account, occurred_on__lte=as_of)

    last_general = (
        activities.counting_for_general_contact()
        .order_by("-occurred_on")
        .values_list("occurred_on", flat=True)
        .first()
    )
    last_weekly = (
        activities.counting_for_weekly_contact()
        .order_by("-occurred_on")
        .values_list("occurred_on", flat=True)
        .first()
    )

    window_start = as_of - timedelta(days=config.MEETING_WINDOW_DAYS)
    meetings_in_window = (
        activities.meetings().filter(occurred_on__gte=window_start).count()
    )

    return ScoringInput(
        lifecycle_stage=account.lifecycle_stage,
        is_managed_service=account.is_managed_service,
        baseline_date=baseline,
        required_meeting_frequency=account.required_meeting_frequency,
        open_bug_tickets=ticket_counts["bugs"] or 0,
        open_general_tickets=ticket_counts["general"] or 0,
        has_ticket_data=has_ticket_data,
        last_adoption_event_on=last_adoption,
        latest_csat_score=latest_csat["score"] if latest_csat else None,
        latest_csat_on=latest_csat["recorded_on"] if latest_csat else None,
        last_general_contact_on=last_general,
        last_weekly_contact_on=last_weekly,
        meetings_in_window=meetings_in_window,
    )
