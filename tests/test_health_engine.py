"""Pure scoring tests. No database, no clock, no fixtures.

Every threshold is asserted on both sides of its boundary, because a boundary is
where a scoring engine actually goes wrong.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from csm.core.enums import (
    NON_SCORABLE_STAGES,
    SCORABLE_STAGES,
    FactorColor,
    FactorId,
    HealthStatus,
    LifecycleStage,
)
from csm.health import config, factors
from csm.health.engine import ScoringInput, band, score

TODAY = date(2026, 6, 15)
# Far enough back that the meeting-attendance grace window has expired, so the
# factor scores on real data in these tests.
BASELINE = TODAY - timedelta(days=365)


def days_ago(n: int) -> date:
    return TODAY - timedelta(days=n)


def make_input(**overrides) -> ScoringInput:
    """An all-green non-managed account, adjusted by the overrides."""
    defaults = {
        "lifecycle_stage": LifecycleStage.ACTIVE.value,
        "is_managed_service": False,
        "baseline_date": BASELINE,
        "open_bug_tickets": 0,
        "open_general_tickets": 0,
        "last_adoption_event_on": days_ago(3),
        "latest_csat_score": 5,
        "latest_csat_on": days_ago(10),
        "last_general_contact_on": days_ago(2),
        "last_weekly_contact_on": days_ago(2),
        "meetings_in_window": 4,
    }
    defaults.update(overrides)
    return ScoringInput(**defaults)


def managed_input(**overrides) -> ScoringInput:
    base = {"is_managed_service": True, "required_meeting_frequency": 4}
    base.update(overrides)
    return make_input(**base)


def factor_of(result, factor_id) -> factors.FactorResult:
    return next(f for f in result.factors if f.factor == factor_id)


# ---------------------------------------------------------------------------
# Configuration invariants
# ---------------------------------------------------------------------------


def test_factor_set_weights_sum_to_one():
    assert config.MANAGED.total_weight == Decimal("1.00")
    assert config.NON_MANAGED.total_weight == Decimal("1.00")


def test_managed_has_five_factors_and_non_managed_four():
    assert len(config.MANAGED.factors) == 5
    assert len(config.NON_MANAGED.factors) == 4
    assert config.MANAGED.weight_per_factor == Decimal("0.20")
    assert config.NON_MANAGED.weight_per_factor == Decimal("0.25")


def test_weekly_contact_is_managed_only_and_general_contact_non_managed_only():
    assert FactorId.WEEKLY_CONTACT.value in config.MANAGED.factors
    assert FactorId.WEEKLY_CONTACT.value not in config.NON_MANAGED.factors
    assert FactorId.GENERAL_CONTACT.value in config.NON_MANAGED.factors
    assert FactorId.GENERAL_CONTACT.value not in config.MANAGED.factors


# ---------------------------------------------------------------------------
# The golden set
# ---------------------------------------------------------------------------


def test_non_managed_all_green_scores_100():
    result = score(make_input(), TODAY)
    assert result.final_score == Decimal("100.00")
    assert result.max_score == 100
    assert result.status == HealthStatus.HEALTHY


def test_managed_all_green_scores_100():
    result = score(managed_input(), TODAY)
    assert result.final_score == Decimal("100.00")
    assert result.max_score == 100
    assert result.status == HealthStatus.HEALTHY


def test_managed_without_meeting_frequency_is_capped_at_watch():
    """The decision that the arithmetic alone would not deliver.

    Four green factors at twenty percent is exactly 80.00, which the band rule
    calls Healthy. The omission cap is what keeps such an account off green.
    """
    result = score(managed_input(required_meeting_frequency=None), TODAY)

    assert result.final_score == Decimal("80.00")
    assert result.max_score == 80
    assert result.omitted == (FactorId.MEETING_ATTENDANCE.value,)
    assert result.capped_by_omission is True
    assert result.status == HealthStatus.WATCH

    omitted = factor_of(result, FactorId.MEETING_ATTENDANCE.value)
    assert omitted.is_omitted
    assert omitted.reason == "required_meeting_frequency_not_set"
    assert omitted.points is None


def test_omission_does_not_renormalise_the_other_weights():
    result = score(managed_input(required_meeting_frequency=None), TODAY)
    scored = [f for f in result.factors if f.is_scored]
    assert len(scored) == 4
    # Each still contributes twenty points, not twenty-five.
    assert result.final_score == Decimal("80.00")


def test_non_managed_three_green_one_yellow_is_exactly_87_50():
    """Proves the score is a Decimal. Rounding here would corrupt the override rule."""
    result = score(make_input(latest_csat_score=3), TODAY)
    assert result.final_score == Decimal("87.50")
    assert result.status == HealthStatus.HEALTHY


def test_non_managed_all_yellow_is_watch_at_exactly_50():
    result = score(
        make_input(
            open_general_tickets=2,
            last_adoption_event_on=days_ago(20),
            latest_csat_score=3,
            last_general_contact_on=days_ago(20),
        ),
        TODAY,
    )
    assert result.final_score == Decimal("50.00")
    assert result.status == HealthStatus.WATCH


def test_meeting_activity_does_not_satisfy_weekly_contact():
    """The specification's guard against one event scoring under two factors."""
    result = score(
        managed_input(
            last_weekly_contact_on=days_ago(10),  # last call or email
            meetings_in_window=4,  # a meeting two days ago is in here
        ),
        TODAY,
    )
    weekly = factor_of(result, FactorId.WEEKLY_CONTACT.value)
    meeting = factor_of(result, FactorId.MEETING_ATTENDANCE.value)
    assert weekly.color == FactorColor.RED
    assert meeting.color == FactorColor.GREEN


def test_freshly_imported_account_with_no_history_is_green_not_red():
    """The import baseline rule: day zero is a clean start, not total neglect."""
    result = score(
        make_input(
            baseline_date=TODAY,
            last_adoption_event_on=None,
            last_general_contact_on=None,
            last_weekly_contact_on=None,
            latest_csat_score=None,
            latest_csat_on=None,
        ),
        TODAY,
    )
    assert factor_of(result, FactorId.MONTHLY_ADOPTION.value).color == FactorColor.GREEN
    assert factor_of(result, FactorId.GENERAL_CONTACT.value).color == FactorColor.GREEN
    # Only CSAT is yellow, because nobody has recorded a rating yet.
    assert result.final_score == Decimal("87.50")


def test_freshly_imported_managed_account_gets_the_meeting_grace_window():
    """A count-in-window factor gets no help from the baseline floor."""
    result = score(
        managed_input(
            baseline_date=TODAY,
            meetings_in_window=0,
            last_adoption_event_on=None,
            last_general_contact_on=None,
            last_weekly_contact_on=None,
        ),
        TODAY,
    )
    meeting = factor_of(result, FactorId.MEETING_ATTENDANCE.value)
    assert meeting.color == FactorColor.GREEN
    assert meeting.detail["in_baseline_grace"] is True


def test_meeting_grace_expires_after_thirty_days():
    baseline = TODAY - timedelta(days=config.MEETING_BASELINE_GRACE_DAYS)
    result = score(managed_input(baseline_date=baseline, meetings_in_window=0), TODAY)
    meeting = factor_of(result, FactorId.MEETING_ATTENDANCE.value)
    assert meeting.detail["in_baseline_grace"] is False
    assert meeting.color == FactorColor.RED


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", sorted(NON_SCORABLE_STAGES))
def test_unscored_stages_return_none(stage):
    assert score(make_input(lifecycle_stage=stage), TODAY) is None


@pytest.mark.parametrize("stage", sorted(SCORABLE_STAGES))
def test_all_scorable_stages_produce_identical_results(stage):
    """No stage-dependent branching of any kind, proven rather than asserted."""
    reference = score(make_input(lifecycle_stage=LifecycleStage.ACTIVE.value), TODAY)
    result = score(make_input(lifecycle_stage=stage), TODAY)
    assert result.final_score == reference.final_score
    assert result.status == reference.status
    assert result.breakdown()["factors"] == reference.breakdown()["factors"]


def test_stage_sets_are_complete_and_disjoint():
    assert SCORABLE_STAGES.isdisjoint(NON_SCORABLE_STAGES)
    assert {s.value for s in LifecycleStage} == SCORABLE_STAGES | NON_SCORABLE_STAGES


# ---------------------------------------------------------------------------
# Class independence
# ---------------------------------------------------------------------------


def test_scoring_input_cannot_carry_account_class():
    """Independence from Class is structural: the field does not exist."""
    fields = set(ScoringInput.__dataclass_fields__)
    assert not {"account_class", "klass", "size", "country"} & fields


# ---------------------------------------------------------------------------
# Boundaries: Open Tickets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bugs", "general", "expected"),
    [
        (0, 0, FactorColor.GREEN),
        (0, 1, FactorColor.YELLOW),
        (0, 3, FactorColor.YELLOW),
        (0, 4, FactorColor.RED),
        (1, 0, FactorColor.YELLOW),  # one bug is 3, still yellow
        (1, 1, FactorColor.RED),  # 4
        (2, 0, FactorColor.RED),  # 6
    ],
)
def test_open_tickets_boundaries(bugs, general, expected):
    result = factors.open_tickets(open_bug_tickets=bugs, open_general_tickets=general)
    assert result.color == expected


# ---------------------------------------------------------------------------
# Boundaries: day-count factors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (0, FactorColor.GREEN),
        (14, FactorColor.GREEN),
        (15, FactorColor.YELLOW),
        (29, FactorColor.YELLOW),
        (30, FactorColor.YELLOW),
        (31, FactorColor.RED),
    ],
)
def test_general_contact_boundaries(days, expected):
    result = factors.general_contact(
        last_contact_on=days_ago(days), baseline=BASELINE, as_of=TODAY
    )
    assert result.color == expected
    assert result.detail["days_since"] == days


@pytest.mark.parametrize(
    ("days", "expected"),
    [(14, FactorColor.GREEN), (15, FactorColor.YELLOW), (30, FactorColor.YELLOW), (31, FactorColor.RED)],
)
def test_monthly_adoption_boundaries(days, expected):
    result = factors.monthly_adoption(
        last_adoption_event_on=days_ago(days), baseline=BASELINE, as_of=TODAY
    )
    assert result.color == expected


@pytest.mark.parametrize(
    ("days", "expected"),
    [(0, FactorColor.GREEN), (6, FactorColor.GREEN), (7, FactorColor.GREEN), (8, FactorColor.RED)],
)
def test_weekly_contact_boundary_is_inclusive_at_seven(days, expected):
    result = factors.weekly_contact(
        last_contact_on=days_ago(days), baseline=BASELINE, as_of=TODAY
    )
    assert result.color == expected


def test_weekly_contact_is_binary_never_yellow():
    for days in range(0, 40):
        result = factors.weekly_contact(
            last_contact_on=days_ago(days), baseline=BASELINE, as_of=TODAY
        )
        assert result.color in (FactorColor.GREEN, FactorColor.RED)


# ---------------------------------------------------------------------------
# Boundaries: CSAT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, FactorColor.RED),
        (2, FactorColor.RED),
        (3, FactorColor.YELLOW),
        (4, FactorColor.GREEN),
        (5, FactorColor.GREEN),
    ],
)
def test_csat_boundaries(value, expected):
    result = factors.csat(latest_score=value, latest_on=days_ago(5), as_of=TODAY)
    assert result.color == expected


def test_missing_csat_scores_yellow_and_is_flagged():
    result = factors.csat(latest_score=None, latest_on=None, as_of=TODAY)
    assert result.color == FactorColor.YELLOW
    assert result.points == config.CSAT_MISSING_POINTS
    assert result.reason == "csat_missing"
    assert result.detail["data_available"] is False


def test_stale_csat_is_treated_as_missing():
    stale_day = days_ago(config.CSAT_VALIDITY_DAYS + 1)
    result = factors.csat(latest_score=5, latest_on=stale_day, as_of=TODAY)
    assert result.color == FactorColor.YELLOW
    assert result.reason == "csat_stale"

    fresh_day = days_ago(config.CSAT_VALIDITY_DAYS)
    fresh = factors.csat(latest_score=5, latest_on=fresh_day, as_of=TODAY)
    assert fresh.color == FactorColor.GREEN


# ---------------------------------------------------------------------------
# Boundaries: Meeting Attendance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("meetings", "required", "expected"),
    [
        (4, 4, FactorColor.GREEN),  # exactly 100%
        (5, 4, FactorColor.GREEN),  # over-achievement never penalised
        (3, 4, FactorColor.YELLOW),  # 75%
        (2, 4, FactorColor.YELLOW),  # exactly 50% is yellow
        (1, 4, FactorColor.RED),  # 25%
        (0, 4, FactorColor.RED),
        (2, 2, FactorColor.GREEN),
        (1, 2, FactorColor.YELLOW),  # exactly 50%
        (0, 2, FactorColor.RED),
        (1, 1, FactorColor.GREEN),
        (0, 1, FactorColor.RED),
    ],
)
def test_meeting_attendance_boundaries(meetings, required, expected):
    result = factors.meeting_attendance(
        meetings_in_window=meetings,
        required_per_month=required,
        baseline=BASELINE,
        as_of=TODAY,
    )
    assert result.color == expected


# ---------------------------------------------------------------------------
# days_since: the baseline floor
# ---------------------------------------------------------------------------


def test_baseline_is_a_floor_not_merely_a_fallback():
    """A backdated entry must not make an account look worse than a clean start."""
    baseline = days_ago(10)
    assert factors.days_since(days_ago(200), baseline, TODAY) == 10
    assert factors.days_since(days_ago(3), baseline, TODAY) == 3
    assert factors.days_since(None, baseline, TODAY) == 10


def test_future_dated_entries_clamp_to_zero():
    future = TODAY + timedelta(days=5)
    assert factors.days_since(future, BASELINE, TODAY) == 0


# ---------------------------------------------------------------------------
# Final bands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("100.00"), HealthStatus.HEALTHY),
        (Decimal("80.00"), HealthStatus.HEALTHY),
        (Decimal("79.99"), HealthStatus.WATCH),
        (Decimal("50.00"), HealthStatus.WATCH),
        (Decimal("49.99"), HealthStatus.AT_RISK),
        (Decimal("0.00"), HealthStatus.AT_RISK),
    ],
)
def test_band_boundaries(value, expected):
    assert band(value, has_omissions=False) == expected


@pytest.mark.parametrize("value", [Decimal("80.00"), Decimal("100.00")])
def test_omission_cap_downgrades_healthy_to_watch(value):
    assert band(value, has_omissions=True) == HealthStatus.WATCH


@pytest.mark.parametrize("value", [Decimal("79.99"), Decimal("50.00"), Decimal("10.00")])
def test_omission_cap_leaves_lower_bands_alone(value):
    assert band(value, has_omissions=True) == band(value, has_omissions=False)


# ---------------------------------------------------------------------------
# Breakdown
# ---------------------------------------------------------------------------


def test_breakdown_records_every_factor_with_its_contribution():
    result = score(managed_input(latest_csat_score=3), TODAY)
    blob = result.breakdown()

    assert set(blob["factors"]) == set(config.MANAGED.factors)
    assert blob["factors"][FactorId.CSAT.value]["contribution"] == "10.00"
    assert blob["factors"][FactorId.OPEN_TICKETS.value]["contribution"] == "20.00"
    assert blob["final_score"] == "90.00"
    assert blob["engine_version"] == config.ENGINE_VERSION


def test_weakest_factors_names_what_is_dragging_the_account_down():
    result = score(
        managed_input(
            last_weekly_contact_on=days_ago(30),  # red
            open_bug_tickets=2,  # red
            latest_csat_score=3,  # yellow
        ),
        TODAY,
    )
    weakest = result.weakest_factors(limit=2)
    assert {f.factor for f in weakest} == {
        FactorId.WEEKLY_CONTACT.value,
        FactorId.OPEN_TICKETS.value,
    }


# ---------------------------------------------------------------------------
# The red-factor cap
# ---------------------------------------------------------------------------


def test_half_the_factors_red_forces_at_risk_from_exactly_fifty():
    """The case the cap exists for.

    No contact and no adoption are red, nobody has raised a ticket so that is
    green, and last week's rating is green. The arithmetic lands on 50.00, which
    the band rule calls Watch by a margin of nothing at all.
    """
    assert band(Decimal("50.00"), has_omissions=False, red_factors=0, scored_factors=4) == (
        HealthStatus.WATCH
    )
    assert band(Decimal("50.00"), has_omissions=False, red_factors=2, scored_factors=4) == (
        HealthStatus.AT_RISK
    )


def test_the_cap_is_a_share_not_a_count():
    """Two red of five is not half, and must not redline a managed account.

    Weekly contact and meeting attendance turn red quickly and together, so a
    flat count of two would fire eight days after the last call and teach
    everyone to ignore the status.
    """
    assert band(Decimal("60.00"), has_omissions=False, red_factors=2, scored_factors=5) == (
        HealthStatus.WATCH
    )
    assert band(Decimal("60.00"), has_omissions=False, red_factors=3, scored_factors=5) == (
        HealthStatus.AT_RISK
    )


def test_the_cap_does_not_rescue_a_healthy_account_with_one_red():
    assert band(Decimal("80.00"), has_omissions=False, red_factors=1, scored_factors=5) == (
        HealthStatus.HEALTHY
    )


def test_at_risk_wins_over_the_omission_cap():
    """An account tripping both is not rescued into Watch by the gentler one."""
    assert band(
        Decimal("80.00"), has_omissions=True, red_factors=2, scored_factors=4
    ) == HealthStatus.AT_RISK


def test_no_scored_factors_does_not_divide_by_zero():
    assert band(Decimal("0.00"), has_omissions=False, red_factors=0, scored_factors=0) == (
        HealthStatus.AT_RISK
    )


def test_the_result_records_the_cap_only_when_it_changed_the_answer():
    """Otherwise it would claim credit on every account that was At Risk anyway."""
    changed = band(Decimal("50.00"), has_omissions=False, red_factors=2, scored_factors=4)
    assert changed == HealthStatus.AT_RISK

    result = score(
        ScoringInput(
            lifecycle_stage=LifecycleStage.ACTIVE,
            is_managed_service=False,
            baseline_date=date(2025, 1, 1),
            last_general_contact_on=date(2025, 1, 1),
            last_adoption_event_on=date(2025, 1, 1),
            latest_csat_score=5,
            latest_csat_on=date(2026, 9, 1),
        ),
        date(2026, 9, 21),
    )
    assert result.status == HealthStatus.AT_RISK
    assert result.capped_by_red_factors is True
    assert result.breakdown()["capped_by_red_factors"] is True
