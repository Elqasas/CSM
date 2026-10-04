"""The six scoring factors, as pure functions.

Nothing here touches the database or reads the clock. Every function takes the
values it needs plus an ``as_of`` date, so the entire scoring surface is testable
without fixtures and every boundary can be asserted on both sides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from csm.core.enums import FactorColor, FactorId, FactorOutcome
from csm.health import config


@dataclass(frozen=True)
class FactorResult:
    factor: str
    outcome: str
    color: str | None
    # Whole numbers from the stepped factors; CSAT's are proportional and may
    # carry two decimal places.
    points: int | Decimal | None
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


@dataclass(frozen=True)
class CsatRating:
    """One rating, as the engine sees it.

    ``contact_id`` is there only to count each person once; ``rating_id`` breaks
    a tie between two ratings from one contact on the same day, newest winning.
    """

    score: int
    recorded_on: date
    contact_id: int | None = None
    rating_id: int = 0


@dataclass(frozen=True)
class CsatQuarter:
    """A calendar quarter's CSAT: each contact's latest rating, averaged."""

    year: int
    quarter: int
    average: Decimal
    voices: int  # ratings counted: one per contact, plus each unnamed one
    ratings: int  # ratings recorded in the quarter, superseded ones included
    latest_on: date

    @property
    def label(self) -> str:
        return f"{self.year} Q{self.quarter}"

    @property
    def color(self) -> str:
        return csat_color(self.average)


def quarter_of(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3 + 1


def csat_quarters(ratings) -> list[CsatQuarter]:
    """Every quarter that holds a rating, newest first.

    Within a quarter a contact's latest rating replaces their earlier ones, so a
    follow-up that changed someone's mind counts as their view rather than as a
    second person agreeing with the first.
    """
    by_quarter: dict[tuple[int, int], list[CsatRating]] = {}
    for rating in ratings:
        by_quarter.setdefault(quarter_of(rating.recorded_on), []).append(rating)

    quarters = []
    for (year, quarter), rows in by_quarter.items():
        latest_per_voice: dict[Any, CsatRating] = {}
        for rating in sorted(rows, key=lambda r: (r.recorded_on, r.rating_id)):
            voice = rating.contact_id
            if voice is None:
                voice = ("unnamed", rating.rating_id)
            latest_per_voice[voice] = rating
        total = sum(r.score for r in latest_per_voice.values())
        average = (Decimal(total) / Decimal(len(latest_per_voice))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        quarters.append(
            CsatQuarter(
                year=year,
                quarter=quarter,
                average=average,
                voices=len(latest_per_voice),
                ratings=len(rows),
                latest_on=max(r.recorded_on for r in rows),
            )
        )
    return sorted(quarters, key=lambda q: (q.year, q.quarter), reverse=True)


def csat_points(average: Decimal) -> int | Decimal:
    """Proportional to the average: see ``config.CSAT_POINTS_PER_STEP``."""
    at_two = config.CSAT_POINTS_PER_STEP
    if average >= 2:
        points = (average - 1) * config.CSAT_POINTS_PER_STEP
    else:
        # 1.00 is not zero: the line from 1 to 2 starts at CSAT_POINTS_AT_ONE.
        points = config.CSAT_POINTS_AT_ONE + (average - 1) * (at_two - config.CSAT_POINTS_AT_ONE)
    points = points.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # A whole number stays an int, so it reads and stores as the stepped
    # factors' points do.
    return int(points) if points == points.to_integral_value() else points


def csat_color(average: Decimal) -> str:
    if average >= config.CSAT_GREEN_MIN:
        return FactorColor.GREEN.value
    if average >= config.CSAT_YELLOW_MIN:
        return FactorColor.YELLOW.value
    return FactorColor.RED.value


def csat(*, ratings, as_of: date) -> FactorResult:
    """The latest rated quarter's average, if it is recent enough to be signal."""
    quarters = csat_quarters(r for r in ratings if r.recorded_on <= as_of)
    current = quarters[0] if quarters else None

    stale = False
    if current is not None and config.CSAT_VALIDITY_DAYS:
        stale = (as_of - current.latest_on).days > config.CSAT_VALIDITY_DAYS

    if current is None or stale:
        return FactorResult(
            factor=FactorId.CSAT.value,
            outcome=FactorOutcome.SCORED.value,
            color=FactorColor.YELLOW.value,
            points=config.CSAT_MISSING_POINTS,
            detail={
                "quarter": current.label if current else None,
                "average": str(current.average) if current else None,
                "recorded_on": current.latest_on.isoformat() if current else None,
                "stale": stale,
                "data_available": False,
            },
            reason="csat_stale" if stale else "csat_missing",
        )

    return FactorResult(
        factor=FactorId.CSAT.value,
        outcome=FactorOutcome.SCORED.value,
        color=current.color,
        points=csat_points(current.average),
        detail={
            "quarter": current.label,
            "average": str(current.average),
            "contacts": current.voices,
            "ratings": current.ratings,
            "recorded_on": current.latest_on.isoformat(),
            "data_available": True,
        },
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
) -> FactorResult:
    """Share of the required meeting frequency met so far this calendar month.

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

    # No grace window after the baseline: setting the frequency or the
    # account's dates earns nothing on its own. Only completed meetings count,
    # so a managed account with none in the window is red from the start.
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
    )
