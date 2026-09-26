"""The six scoring factors, as pure functions.

Nothing here touches the database or reads the clock. Every function takes the
values it needs plus an ``as_of`` date, so the entire scoring surface is testable
without fixtures and every boundary can be asserted on both sides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from csm.core.enums import FactorColor, FactorId, FactorOutcome
from csm.health import config


@dataclass(frozen=True)
class FactorResult:
    factor: str
    outcome: str
    color: str | None
    points: int | None
    detail: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def is_scored(self) -> bool:
        return self.outcome == FactorOutcome.SCORED

    @property
    def is_omitted(self) -> bool:
        return self.outcome == FactorOutcome.OMITTED


_POINTS = {
    FactorColor.GREEN: config.GREEN_POINTS,
    FactorColor.YELLOW: config.YELLOW_POINTS,
    FactorColor.RED: config.RED_POINTS,
}


def _scored(factor: str, color: str, **detail: Any) -> FactorResult:
    return FactorResult(
        factor=factor,
        outcome=FactorOutcome.SCORED.value,
        color=color,
        points=_POINTS[color],
        detail=detail,
    )


def _omitted(factor: str, reason: str, **detail: Any) -> FactorResult:
    return FactorResult(
        factor=factor,
        outcome=FactorOutcome.OMITTED.value,
        color=None,
        points=None,
        detail=detail,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# The shared day-count helper
# ---------------------------------------------------------------------------


def days_since(event_date: date | None, baseline: date, as_of: date) -> int:
    """Days between the reference date and ``as_of``.

    ``baseline`` is a floor, not merely a fallback for a missing date. That
    single choice does three things at once: it gives the import clean-start rule
    for free, it stops a backdated entry making an account look worse than a
    fresh import, and the outer clamp keeps a future-dated entry from producing a
    negative count that would read as green through an unintended path.
    """
    reference = baseline if event_date is None else max(event_date, baseline)
    return max(0, (as_of - reference).days)


def _day_count_color(days: int) -> str:
    if days < config.DAYS_GREEN_MAX_EXCLUSIVE:
        return FactorColor.GREEN.value
    if days <= config.DAYS_YELLOW_MAX_INCLUSIVE:
        return FactorColor.YELLOW.value
    return FactorColor.RED.value


# ---------------------------------------------------------------------------
# Factors
# ---------------------------------------------------------------------------


def open_tickets(
    *, open_bug_tickets: int, open_general_tickets: int, has_ticket_data: bool = True
) -> FactorResult:
    """Weighted ticket load: bugs count triple, general tickets count once."""
    weighted = (
        open_bug_tickets * config.TICKET_BUG_WEIGHT
        + open_general_tickets * config.TICKET_GENERAL_WEIGHT
    )
    if weighted <= config.TICKET_GREEN_MAX:
        color = FactorColor.GREEN.value
    elif weighted <= config.TICKET_YELLOW_MAX:
        color = FactorColor.YELLOW.value
    else:
        color = FactorColor.RED.value
    return _scored(
        FactorId.OPEN_TICKETS.value,
        color,
        bugs=open_bug_tickets,
        general=open_general_tickets,
        weighted=weighted,
        data_available=has_ticket_data,
    )


def monthly_adoption(
    *, last_adoption_event_on: date | None, baseline: date, as_of: date
) -> FactorResult:
    """Days since the most recent phishing campaign or course assignment.

    Either kind of event qualifies; the factor measures whichever is more recent.
    """
    days = days_since(last_adoption_event_on, baseline, as_of)
    return _scored(
        FactorId.MONTHLY_ADOPTION.value,
        _day_count_color(days),
        days_since=days,
        last_event_on=last_adoption_event_on.isoformat() if last_adoption_event_on else None,
    )


def csat(*, latest_score: int | None, latest_on: date | None, as_of: date) -> FactorResult:
    """Most recent CSAT rating, if it is recent enough to still be signal."""
    stale = False
    if latest_score is not None and latest_on is not None and config.CSAT_VALIDITY_DAYS:
        stale = (as_of - latest_on).days > config.CSAT_VALIDITY_DAYS

    if latest_score is None or latest_on is None or stale:
        return FactorResult(
            factor=FactorId.CSAT.value,
            outcome=FactorOutcome.SCORED.value,
            color=FactorColor.YELLOW.value,
            points=config.CSAT_MISSING_POINTS,
            detail={
                "score": latest_score,
                "recorded_on": latest_on.isoformat() if latest_on else None,
                "stale": stale,
                "data_available": False,
            },
            reason="csat_stale" if stale else "csat_missing",
        )

    if latest_score >= config.CSAT_GREEN_MIN:
        color = FactorColor.GREEN.value
    elif latest_score == config.CSAT_YELLOW:
        color = FactorColor.YELLOW.value
    else:
        color = FactorColor.RED.value
    return _scored(
        FactorId.CSAT.value,
        color,
        score=latest_score,
        recorded_on=latest_on.isoformat(),
        data_available=True,
    )


def general_contact(
    *, last_contact_on: date | None, baseline: date, as_of: date
) -> FactorResult:
    """Days since the last activity of any contact type. Meetings do count here."""
    days = days_since(last_contact_on, baseline, as_of)
    return _scored(
        FactorId.GENERAL_CONTACT.value,
        _day_count_color(days),
        days_since=days,
        last_contact_on=last_contact_on.isoformat() if last_contact_on else None,
    )


def weekly_contact(
    *, last_contact_on: date | None, baseline: date, as_of: date
) -> FactorResult:
    """Binary: has anyone made non-meeting contact within the last seven days?

    Meetings are excluded and counted only under Meeting Attendance, so a single
    meeting cannot score under two factors at once.
    """
    days = days_since(last_contact_on, baseline, as_of)
    color = (
        FactorColor.GREEN.value
        if days <= config.WEEKLY_CONTACT_MAX_DAYS
        else FactorColor.RED.value
    )
    return _scored(
        FactorId.WEEKLY_CONTACT.value,
        color,
        days_since=days,
        last_contact_on=last_contact_on.isoformat() if last_contact_on else None,
    )


def meeting_attendance(
    *,
    meetings_in_window: int,
    required_per_month: int | None,
    baseline: date,
    as_of: date,
) -> FactorResult:
    """Share of the required meeting frequency met over the trailing 30 days.

    Omitted entirely when the required frequency is not set. The remaining
    weights are deliberately not redistributed, so the account is scored out of
    80 and cannot show green until someone fills the frequency in.
    """
    if required_per_month is None:
        return _omitted(
            FactorId.MEETING_ATTENDANCE.value,
            reason="required_meeting_frequency_not_set",
            meetings_in_window=meetings_in_window,
        )

    days_since_baseline = (as_of - baseline).days
    if days_since_baseline < config.MEETING_BASELINE_GRACE_DAYS:
        return _scored(
            FactorId.MEETING_ATTENDANCE.value,
            FactorColor.GREEN.value,
            meetings_in_window=meetings_in_window,
            required_per_month=required_per_month,
            in_baseline_grace=True,
            days_since_baseline=days_since_baseline,
        )

    # Integer comparison throughout. Computing a float percentage and comparing
    # it against 100 and 50 would put a rounding step between the data and a
    # business decision.
    if meetings_in_window >= required_per_month:
        color = FactorColor.GREEN.value
    elif 2 * meetings_in_window >= required_per_month:
        color = FactorColor.YELLOW.value
    else:
        color = FactorColor.RED.value

    compliance_pct = int(
        (Decimal(meetings_in_window) / Decimal(required_per_month) * 100).to_integral_value()
    )
    return _scored(
        FactorId.MEETING_ATTENDANCE.value,
        color,
        meetings_in_window=meetings_in_window,
        required_per_month=required_per_month,
        compliance_pct=compliance_pct,
        in_baseline_grace=False,
    )
