"""The pure scoring function.

``score()`` takes a frozen input dataclass and a date, and returns a frozen
result. It reads no database, calls no clock, and has no side effects. That is
what makes the whole scoring matrix testable as plain parametrized unit tests,
and it is why ``as_of`` is a parameter rather than a call to ``date.today()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from csm.core.enums import SCORABLE_STAGES, FactorId, HealthStatus
from csm.health import config, factors
from csm.health.factors import FactorResult

_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class ScoringInput:
    """Everything the engine is allowed to know about an account.

    Note what is absent: the account's Class, its Size, its country, its name.
    The engine cannot let any of them influence a score because it never
    receives them.
    """

    lifecycle_stage: str
    is_managed_service: bool
    baseline_date: date

    required_meeting_frequency: int | None = None

    open_bug_tickets: int = 0
    open_general_tickets: int = 0
    has_ticket_data: bool = True

    last_adoption_event_on: date | None = None

    latest_csat_score: int | None = None
    latest_csat_on: date | None = None

    last_general_contact_on: date | None = None
    last_weekly_contact_on: date | None = None

    meetings_in_window: int = 0


@dataclass(frozen=True)
class ScoreResult:
    factor_set: str
    final_score: Decimal
    max_score: int
    status: str
    capped_by_omission: bool
    factors: tuple[FactorResult, ...]
    omitted: tuple[str, ...]
    capped_by_red_factors: bool = False
    engine_version: str = config.ENGINE_VERSION

    def breakdown(self) -> dict:
        """The JSON blob persisted with every history row."""
        return {
            "engine_version": self.engine_version,
            "factor_set": self.factor_set,
            "final_score": str(self.final_score),
            "max_score": self.max_score,
            "status": self.status,
            "capped_by_omission": self.capped_by_omission,
            "capped_by_red_factors": self.capped_by_red_factors,
            "omitted": list(self.omitted),
            "factors": {
                f.factor: {
                    "outcome": f.outcome,
                    "color": f.color,
                    "points": f.points,
                    "weight": str(self._weight()),
                    "contribution": str(self._contribution(f)),
                    "reason": f.reason,
                    "detail": f.detail,
                }
                for f in self.factors
            },
        }

    def _weight(self) -> Decimal:
        return config.select_factor_set(self.factor_set == config.MANAGED.id).weight_per_factor

    def _contribution(self, f: FactorResult) -> Decimal:
        if not f.is_scored or f.points is None:
            return Decimal("0.00")
        return (Decimal(f.points) * self._weight()).quantize(_CENTS)

    def weakest_factors(self, limit: int = 2) -> tuple[FactorResult, ...]:
        """The scored factors dragging the account down, worst first."""
        ranked = sorted(
            (f for f in self.factors if f.is_scored and f.points is not None),
            key=lambda f: f.points,
        )
        return tuple(f for f in ranked if f.points < config.GREEN_POINTS)[:limit]


def band(
    score: Decimal, *, has_omissions: bool, red_factors: int = 0, scored_factors: int = 0
) -> str:
    """Map a final score to a health status, with two structural caps.

    **The omission cap** makes the 80-maximum rule hold at its one awkward
    point. Four green factors at twenty percent each is exactly 80.00, which the
    band rule would otherwise call Healthy, so an account missing its meeting
    frequency could show green while still missing a factor. The cap forces
    Watch instead.

    **The red-factor cap** closes the mirror-image gap at the bottom. A
    neglected non-managed account - no contact, no adoption, no tickets because
    nobody raised any, and a recent good rating - lands on exactly 50.00, which
    the band rule reads as Watch by a margin of nothing. Half the things measured
    being red is not a Watch, and a status that rests on hitting a boundary
    exactly is one weight change from flipping.

    It is a share, not a count. Two red of four is half the picture; two red of
    five is not, and a flat count would send a managed account to At Risk eight
    days after its last call, because weekly contact and meeting attendance turn
    red quickly and together.

    Both are caps rather than reweightings on purpose. The weights are the
    specification's; changing them would silently restate every score already
    written to history.
    """
    if score >= config.BAND_HEALTHY_MIN:
        status = HealthStatus.HEALTHY.value
    elif score >= config.BAND_WATCH_MIN:
        status = HealthStatus.WATCH.value
    else:
        status = HealthStatus.AT_RISK.value

    # At Risk first: it is the more severe of the two caps, so an account that
    # trips both is not rescued into Watch by the gentler one.
    if _mostly_red(red_factors, scored_factors):
        return HealthStatus.AT_RISK.value

    if has_omissions and status == HealthStatus.HEALTHY.value:
        return HealthStatus.WATCH.value
    return status


def _mostly_red(red_factors: int, scored_factors: int) -> bool:
    if not scored_factors:
        return False
    return Decimal(red_factors) / Decimal(scored_factors) >= config.RED_SHARE_FOR_AT_RISK


def is_scorable(lifecycle_stage: str) -> bool:
    return lifecycle_stage in SCORABLE_STAGES


def score(inp: ScoringInput, as_of: date) -> ScoreResult | None:
    """Compute the health score, or None when the stage is not scored.

    Active, Adoption, Renewal and Expansion run through this identical code path
    with no stage-dependent branching of any kind.
    """
    if not is_scorable(inp.lifecycle_stage):
        return None

    factor_set = config.select_factor_set(inp.is_managed_service)
    results: list[FactorResult] = []

    for factor_id in factor_set.factors:
        results.append(_evaluate(factor_id, inp, as_of))

    scored = [f for f in results if f.is_scored and f.points is not None]
    omitted = tuple(f.factor for f in results if f.is_omitted)

    total = sum(
        (Decimal(f.points) * factor_set.weight_per_factor for f in scored),
        Decimal("0"),
    ).quantize(_CENTS, rounding=ROUND_HALF_UP)

    max_score = int(
        (Decimal(config.GREEN_POINTS) * factor_set.weight_per_factor * len(scored)).to_integral_value()
    )

    red_factors = sum(1 for f in scored if f.points == config.RED_POINTS)
    status = band(
        total,
        has_omissions=bool(omitted),
        red_factors=red_factors,
        scored_factors=len(scored),
    )

    return ScoreResult(
        factor_set=factor_set.id,
        final_score=total,
        max_score=max_score,
        status=status,
        capped_by_omission=bool(omitted) and total >= config.BAND_HEALTHY_MIN,
        # Recorded only when the cap actually changed the answer, so the
        # interface can explain an At Risk the arithmetic does not account for
        # without claiming credit on every account that was At Risk anyway.
        capped_by_red_factors=(
            _mostly_red(red_factors, len(scored)) and total >= config.BAND_WATCH_MIN
        ),
        factors=tuple(results),
        omitted=omitted,
    )


def _evaluate(factor_id: str, inp: ScoringInput, as_of: date) -> FactorResult:
    if factor_id == FactorId.OPEN_TICKETS:
        return factors.open_tickets(
            open_bug_tickets=inp.open_bug_tickets,
            open_general_tickets=inp.open_general_tickets,
            has_ticket_data=inp.has_ticket_data,
        )
    if factor_id == FactorId.MONTHLY_ADOPTION:
        return factors.monthly_adoption(
            last_adoption_event_on=inp.last_adoption_event_on,
            baseline=inp.baseline_date,
            as_of=as_of,
        )
    if factor_id == FactorId.CSAT:
        return factors.csat(
            latest_score=inp.latest_csat_score,
            latest_on=inp.latest_csat_on,
            as_of=as_of,
        )
    if factor_id == FactorId.GENERAL_CONTACT:
        return factors.general_contact(
            last_contact_on=inp.last_general_contact_on,
            baseline=inp.baseline_date,
            as_of=as_of,
        )
    if factor_id == FactorId.WEEKLY_CONTACT:
        return factors.weekly_contact(
            last_contact_on=inp.last_weekly_contact_on,
            baseline=inp.baseline_date,
            as_of=as_of,
        )
    if factor_id == FactorId.MEETING_ATTENDANCE:
        return factors.meeting_attendance(
            meetings_in_window=inp.meetings_in_window,
            required_per_month=inp.required_meeting_frequency,
            baseline=inp.baseline_date,
            as_of=as_of,
        )
    raise ValueError(f"Unknown factor: {factor_id}")
