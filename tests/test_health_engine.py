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
# A year back, so the day-count factors score on real data in these tests.
BASELINE = TODAY - timedelta(days=365)


def days_ago(n: int) -> date:
    return TODAY - timedelta(days=n)


def rated(*scores: int, on: date | None = None) -> tuple[factors.CsatRating, ...]:
    """One rating per score, each from a different contact."""
    on = on or days_ago(10)
    return tuple(
        factors.CsatRating(score=value, recorded_on=on, contact_id=i + 1, rating_id=i + 1)
        for i, value in enumerate(scores)
    )


def make_input(**overrides) -> ScoringInput:
    """An all-green non-managed account, adjusted by the overrides."""
    defaults = {
        "lifecycle_stage": LifecycleStage.ACTIVE.value,
        "is_managed_service": False,
        "baseline_date": BASELINE,
        "open_bug_tickets": 0,
        "open_general_tickets": 0,
        "last_adoption_event_on": days_ago(3),
        "csat_ratings": rated(5),
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
    result = score(make_input(csat_ratings=rated(3)), TODAY)
    assert result.final_score == Decimal("87.50")
    assert result.status == HealthStatus.HEALTHY


def test_non_managed_all_yellow_is_watch_at_exactly_50():
    result = score(
        make_input(
            open_general_tickets=2,
            last_adoption_event_on=days_ago(20),
            csat_ratings=rated(3),
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
            csat_ratings=(),
        ),
        TODAY,
    )
    assert factor_of(result, FactorId.MONTHLY_ADOPTION.value).color == FactorColor.GREEN
    assert factor_of(result, FactorId.GENERAL_CONTACT.value).color == FactorColor.GREEN
    # Only CSAT is yellow, because nobody has recorded a rating yet.
    assert result.final_score == Decimal("87.50")


def test_freshly_set_up_managed_account_with_no_meetings_is_red():
    """Filling in the dates earns nothing: only completed meetings count."""
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
    assert meeting.color == FactorColor.RED
    assert meeting.points == config.RED_POINTS


def test_one_completed_meeting_on_setup_day_meets_a_monthly_requirement():
    result = score(
        managed_input(
            baseline_date=TODAY, required_meeting_frequency=1, meetings_in_window=1
        ),
        TODAY,
    )
    assert factor_of(result, FactorId.MEETING_ATTENDANCE.value).color == FactorColor.GREEN


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
    result = factors.csat(ratings=rated(value, on=days_ago(5)), as_of=TODAY)
    assert result.color == expected


@pytest.mark.parametrize(
    ("scores", "average", "expected"),
    [
        ((4, 4, 3, 4), "3.75", FactorColor.YELLOW),  # just under green
        ((4, 4, 4, 4), "4.00", FactorColor.GREEN),  # on the green line
        ((3, 3, 2, 4), "3.00", FactorColor.YELLOW),  # on the yellow line
        ((3, 3, 2, 3), "2.75", FactorColor.RED),  # just under yellow
        ((2, 5, 5, 5), "4.25", FactorColor.GREEN),  # one unhappy voice of four
    ],
)
def test_csat_bands_the_average_of_every_contact(scores, average, expected):
    result = factors.csat(ratings=rated(*scores), as_of=TODAY)
    assert result.detail["average"] == average
    assert result.detail["contacts"] == len(scores)
    assert result.color == expected


def test_a_contact_rated_twice_in_a_quarter_counts_once_at_their_latest():
    """One person asked twice is still one voice, and their newer view stands."""
    ratings = (
        factors.CsatRating(score=1, recorded_on=date(2026, 4, 10), contact_id=7, rating_id=1),
        factors.CsatRating(score=5, recorded_on=date(2026, 6, 1), contact_id=7, rating_id=2),
        factors.CsatRating(score=4, recorded_on=date(2026, 5, 1), contact_id=8, rating_id=3),
    )
    result = factors.csat(ratings=ratings, as_of=TODAY)
    assert result.detail["average"] == "4.50"
    assert result.detail["contacts"] == 2
    assert result.detail["ratings"] == 3


def test_ratings_with_no_contact_each_count_on_their_own():
    ratings = (
        factors.CsatRating(score=2, recorded_on=days_ago(5), rating_id=1),
        factors.CsatRating(score=4, recorded_on=days_ago(5), rating_id=2),
    )
    result = factors.csat(ratings=ratings, as_of=TODAY)
    assert result.detail["average"] == "3.00"
    assert result.detail["contacts"] == 2


def test_only_the_latest_rated_quarter_counts():
    """Q1's poor ratings are history once Q2 has a round of its own."""
    ratings = (
        factors.CsatRating(score=1, recorded_on=date(2026, 3, 31), contact_id=1, rating_id=1),
        factors.CsatRating(score=1, recorded_on=date(2026, 3, 30), contact_id=2, rating_id=2),
        factors.CsatRating(score=5, recorded_on=date(2026, 4, 1), contact_id=1, rating_id=3),
    )
    result = factors.csat(ratings=ratings, as_of=TODAY)
    assert result.detail["quarter"] == "2026 Q2"
    assert result.detail["average"] == "5.00"
    assert result.color == FactorColor.GREEN


def test_last_quarter_holds_until_the_new_one_is_rated():
    """On 1 July nobody has been surveyed yet; Q2's result still stands."""
    result = factors.csat(ratings=rated(2, 3, on=date(2026, 6, 20)), as_of=date(2026, 7, 1))
    assert result.detail["quarter"] == "2026 Q2"
    assert result.color == FactorColor.RED


def test_csat_quarters_summarises_each_quarter_newest_first():
    ratings = rated(5, 3, on=date(2026, 5, 1)) + (
        factors.CsatRating(score=2, recorded_on=date(2026, 1, 15), contact_id=9, rating_id=9),
    )
    quarters = factors.csat_quarters(ratings)
    assert [(q.label, str(q.average), q.voices) for q in quarters] == [
        ("2026 Q2", "4.00", 2),
        ("2026 Q1", "2.00", 1),
    ]


def test_missing_csat_scores_yellow_and_is_flagged():
    result = factors.csat(ratings=(), as_of=TODAY)
    assert result.color == FactorColor.YELLOW
    assert result.points == config.CSAT_MISSING_POINTS
    assert result.reason == "csat_missing"
    assert result.detail["data_available"] is False


def test_stale_csat_is_treated_as_missing():
    stale_day = days_ago(config.CSAT_VALIDITY_DAYS + 1)
    result = factors.csat(ratings=rated(5, on=stale_day), as_of=TODAY)
    assert result.color == FactorColor.YELLOW
    assert result.reason == "csat_stale"

    fresh_day = days_ago(config.CSAT_VALIDITY_DAYS)
    fresh = factors.csat(ratings=rated(5, on=fresh_day), as_of=TODAY)
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
    result = score(managed_input(csat_ratings=rated(3)), TODAY)
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
            csat_ratings=rated(3),  # yellow
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
            csat_ratings=rated(5, on=date(2026, 9, 1)),
        ),
        date(2026, 9, 21),
    )
    assert result.status == HealthStatus.AT_RISK
    assert result.capped_by_red_factors is True
    assert result.breakdown()["capped_by_red_factors"] is True


# ---------------------------------------------------------------------------
# CSAT points are proportional to the average, with a floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scores", "points"),
    [
        ((1,), 5),  # the lowest average still earns something
        ((1, 2), 15),  # 1.50: halfway from 5 to 25
        ((2,), 25),
        ((2, 3), Decimal("37.50")),  # 2.50
        ((3,), 50),
        ((3, 4, 4), Decimal("66.75")),  # 3.67
        ((4,), 75),
        ((4, 5), Decimal("87.50")),  # 4.50
        ((5,), 100),
    ],
)
def test_csat_points_follow_the_average(scores, points):
    assert factors.csat(ratings=rated(*scores), as_of=TODAY).points == points


@pytest.mark.parametrize(
    ("scores", "contribution", "final"),
    [
        ((1,), "1.00", "81.00"),  # 5 points at 20%: one percent, not zero
        ((2,), "5.00", "85.00"),
    ],
)
def test_a_red_csat_still_adds_to_a_managed_account(scores, contribution, final):
    red = score(managed_input(csat_ratings=rated(*scores)), TODAY)
    blob = red.breakdown()["factors"][FactorId.CSAT.value]
    assert blob["color"] == FactorColor.RED
    assert blob["contribution"] == contribution
    assert red.final_score == Decimal(final)


def test_a_red_csat_still_counts_towards_the_red_factor_cap():
    """It earns points, but it is red - and the cap counts colours."""
    result = score(
        make_input(
            csat_ratings=rated(1),
            last_adoption_event_on=days_ago(60),  # red
            open_bug_tickets=0,
            last_general_contact_on=days_ago(2),
        ),
        TODAY,
    )
    assert result.status == HealthStatus.AT_RISK


def test_fractional_points_survive_the_json_breakdown():
    import json

    blob = score(make_input(csat_ratings=rated(3, 4, 4)), TODAY).breakdown()
    stored = json.loads(json.dumps(blob))
    assert stored["factors"][FactorId.CSAT.value]["points"] == "66.75"
    assert stored["factors"][FactorId.CSAT.value]["contribution"] == "16.69"
