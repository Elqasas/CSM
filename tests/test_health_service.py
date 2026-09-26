"""Health service tests: persistence, activation, overrides and decay."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import time_machine
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from csm.accounts.models import Account
from csm.core.enums import (
    NON_SCORABLE_STAGES,
    ActivityType,
    HealthStatus,
    LifecycleStage,
    OverrideClearedReason,
    ScoreTrigger,
    TicketKind,
)
from csm.health import services
from csm.health.models import HealthScoreHistory, HealthStatusOverride
from tests.factories import (
    AccountFactory,
    ActivityFactory,
    AdoptionEventFactory,
    CsatFactory,
    TicketFactory,
)

pytestmark = pytest.mark.django_db

TODAY = dt.date(2026, 6, 15)


@pytest.fixture(autouse=True)
def _pin_the_clock():
    """Pin wall-clock time to the date the fixtures are built around.

    Overrides are user actions, so they read the clock rather than taking an
    as-of date. Without pinning, fixtures anchored to a fixed date would age
    against the real calendar and the scores would drift.
    """
    with time_machine.travel("2026-06-15 09:00:00+00:00", tick=False):
        yield


def days_ago(n: int) -> dt.date:
    return TODAY - dt.timedelta(days=n)


def healthy_account(**kwargs) -> Account:
    """An account whose every factor reads green."""
    account = AccountFactory(scoring_baseline_date=days_ago(365), **kwargs)
    AdoptionEventFactory(account=account, occurred_on=days_ago(3))
    CsatFactory(account=account, score=5, recorded_on=days_ago(5))
    ActivityFactory(account=account, activity_type=ActivityType.CALL, occurred_on=days_ago(2))
    return account


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_recalculate_writes_cached_columns_and_one_history_row():
    account = healthy_account()

    result = services.recalculate(account, trigger=ScoreTrigger.EVENT, as_of=TODAY)
    account.refresh_from_db()

    assert result.final_score == Decimal("100.00")
    assert account.computed_health_score == Decimal("100.00")
    assert account.computed_health_max == 100
    assert account.effective_health_status == HealthStatus.HEALTHY
    assert account.health_calculated_at is not None

    history = HealthScoreHistory.objects.filter(account=account)
    assert history.count() == 1
    row = history.get()
    assert row.trigger == ScoreTrigger.EVENT
    assert row.calculated_on == TODAY
    assert row.breakdown["factors"]["OPEN_TICKETS"]["color"] == "GREEN"


def test_every_recalculation_appends_a_history_row():
    account = healthy_account()
    for _ in range(3):
        services.recalculate(account, as_of=TODAY)
    assert HealthScoreHistory.objects.filter(account=account).count() == 3


def test_managed_without_frequency_is_scored_out_of_eighty_and_capped():
    account = healthy_account(managed_no_frequency=True)
    ActivityFactory(account=account, activity_type=ActivityType.MEETING, occurred_on=days_ago(1))

    result = services.recalculate(account, as_of=TODAY)
    account.refresh_from_db()

    assert result.final_score == Decimal("80.00")
    assert account.computed_health_max == 80
    assert account.effective_health_status == HealthStatus.WATCH
    assert result.omitted == ("MEETING_ATTENDANCE",)


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", sorted(NON_SCORABLE_STAGES))
def test_unscored_stages_record_not_applicable_and_no_score(stage):
    account = healthy_account()
    account.lifecycle_stage = stage
    account.save(update_fields=["lifecycle_stage"])

    result = services.recalculate(account, trigger=ScoreTrigger.STAGE_CHANGE, as_of=TODAY)
    account.refresh_from_db()

    assert result is None
    assert account.computed_health_score is None
    assert account.effective_health_status == HealthStatus.NOT_APPLICABLE

    row = HealthScoreHistory.objects.filter(account=account).latest("calculated_at")
    assert row.score is None
    assert row.effective_status == HealthStatus.NOT_APPLICABLE


def test_daily_pass_skips_unscored_accounts():
    scored = healthy_account()
    AccountFactory(churned=True)
    AccountFactory(onboarding=True)
    AccountFactory(poc=True)

    visited = services.recalculate_all(as_of=TODAY)

    assert visited == 1
    assert HealthScoreHistory.objects.filter(account=scored).count() == 1
    assert HealthScoreHistory.objects.exclude(account=scored).count() == 0


# ---------------------------------------------------------------------------
# The override rule
# ---------------------------------------------------------------------------


def test_override_survives_a_recalculation_with_identical_inputs(csm_user):
    account = healthy_account()
    services.recalculate(account, as_of=TODAY)

    override = services.apply_override(
        account,
        status=HealthStatus.AT_RISK.value,
        reason="Escalated by the client's CISO, ignore the green number.",
        actor=csm_user,
    )

    services.recalculate(account, as_of=TODAY)
    override.refresh_from_db()
    account.refresh_from_db()

    assert override.cleared_at is None
    assert account.effective_health_status == HealthStatus.AT_RISK


def test_override_clears_on_any_score_change_even_within_the_same_band(csm_user):
    """The headline requirement: the comparison is the score, never the band."""
    account = healthy_account(is_managed_service=False)
    # Two yellow factors and two green: 75.00, which is Watch.
    CsatFactory(account=account, score=3, recorded_on=days_ago(1))
    TicketFactory(account=account, ticket_kind=TicketKind.GENERAL)
    services.recalculate(account, as_of=TODAY)
    account.refresh_from_db()
    assert account.computed_health_score == Decimal("75.00")

    override = services.apply_override(
        account,
        status=HealthStatus.HEALTHY.value,
        reason="Reviewed with the account team, the yellow is a known one-off.",
        actor=csm_user,
    )
    assert override.score_at_override == Decimal("75.00")

    # Move the score to 87.50: a different number, still not Healthy's band edge.
    account.refresh_from_db()
    CsatFactory(account=account, score=5, recorded_on=TODAY)
    services.recalculate(account, as_of=TODAY)

    override.refresh_from_db()
    account.refresh_from_db()

    assert override.cleared_at is not None
    assert override.cleared_reason == OverrideClearedReason.SCORE_CHANGED
    assert override.cleared_score == Decimal("87.50")
    assert account.effective_health_status == HealthStatus.HEALTHY


def test_only_one_active_override_per_account_is_possible(csm_user):
    account = healthy_account()
    services.apply_override(
        account,
        status=HealthStatus.WATCH.value,
        reason="Watching this one after the support escalation.",
        actor=csm_user,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        HealthStatusOverride.objects.create(
            account=account,
            status=HealthStatus.AT_RISK,
            reason="A second active override should be impossible.",
            score_at_override=Decimal("50.00"),
            max_at_override=100,
            created_by=csm_user,
        )


def test_applying_a_second_override_supersedes_the_first(csm_user):
    account = healthy_account()
    first = services.apply_override(
        account, status=HealthStatus.WATCH.value,
        reason="Initial judgement call after the outage.", actor=csm_user,
    )
    second = services.apply_override(
        account, status=HealthStatus.AT_RISK.value,
        reason="Downgraded further after the second incident.", actor=csm_user,
    )
    first.refresh_from_db()
    account.refresh_from_db()

    assert first.cleared_reason == OverrideClearedReason.SUPERSEDED
    assert second.cleared_at is None
    assert account.effective_health_status == HealthStatus.AT_RISK


def test_override_requires_a_real_reason(csm_user):
    account = healthy_account()
    with pytest.raises(ValidationError):
        services.apply_override(account, status=HealthStatus.WATCH.value, reason=".", actor=csm_user)


def test_override_rejected_on_an_account_with_no_score(csm_user):
    account = healthy_account(onboarding=True)
    with pytest.raises(services.OverrideNotAllowed):
        services.apply_override(
            account,
            status=HealthStatus.HEALTHY.value,
            reason="There is no number here to override.",
            actor=csm_user,
        )


def test_moving_to_an_unscored_stage_clears_the_override(csm_user):
    account = healthy_account()
    override = services.apply_override(
        account, status=HealthStatus.AT_RISK.value,
        reason="Manual downgrade pending the renewal conversation.", actor=csm_user,
    )

    account.lifecycle_stage = LifecycleStage.CHURNED
    account.save(update_fields=["lifecycle_stage"])
    services.recalculate(account, trigger=ScoreTrigger.STAGE_CHANGE, as_of=TODAY)

    override.refresh_from_db()
    assert override.cleared_reason == OverrideClearedReason.STAGE_NOT_SCORABLE


def test_clearing_an_override_restores_the_computed_status(csm_user):
    account = healthy_account()
    services.apply_override(
        account, status=HealthStatus.AT_RISK.value,
        reason="Temporary manual downgrade during the incident.", actor=csm_user,
    )
    services.clear_override(account, actor=csm_user)
    account.refresh_from_db()
    assert account.effective_health_status == HealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# Decay over time
# ---------------------------------------------------------------------------


def _decay_walker(account):
    def status_on(day_offset: int) -> str:
        as_of = TODAY + dt.timedelta(days=day_offset)
        services.recalculate(account, trigger=ScoreTrigger.DAILY, as_of=as_of)
        account.refresh_from_db()
        return account.effective_health_status

    return status_on


def test_non_managed_decay_reaches_at_risk_when_half_the_factors_are_red():
    """Neglect alone now redlines a non-managed account, by the red-factor cap.

    The arithmetic puts two of four red at exactly 50.00, which the band rule
    reads as Watch by a margin of nothing. Half the things measured being red is
    not a Watch, so the cap forces At Risk. Without it the account waited for the
    CSAT rating to age past its validity window - another five months - before
    anything said so.
    """
    account = AccountFactory(scoring_baseline_date=days_ago(365))
    AdoptionEventFactory(account=account, occurred_on=TODAY)
    CsatFactory(account=account, score=5, recorded_on=TODAY)
    ActivityFactory(account=account, activity_type=ActivityType.CALL, occurred_on=TODAY)
    status_on = _decay_walker(account)

    assert status_on(0) == HealthStatus.HEALTHY
    assert status_on(14) == HealthStatus.HEALTHY  # last green day
    assert status_on(15) == HealthStatus.WATCH  # adoption and contact turn yellow
    assert status_on(30) == HealthStatus.WATCH  # still yellow at exactly 30
    # Both turn red here. The score is 50.00 either way; the cap is what makes
    # the status say so.
    assert status_on(31) == HealthStatus.AT_RISK
    assert status_on(181) == HealthStatus.AT_RISK  # and stays there as CSAT ages


def test_managed_decay_reaches_at_risk_on_neglect_alone():
    """A managed account has three decaying factors, so neglect does redline it."""
    account = AccountFactory(managed=True, scoring_baseline_date=days_ago(365))
    AdoptionEventFactory(account=account, occurred_on=TODAY)
    CsatFactory(account=account, score=5, recorded_on=TODAY)
    ActivityFactory(account=account, activity_type=ActivityType.CALL, occurred_on=TODAY)
    ActivityFactory(account=account, activity_type=ActivityType.MEETING, occurred_on=TODAY)
    status_on = _decay_walker(account)

    assert status_on(0) == HealthStatus.HEALTHY
    assert status_on(7) == HealthStatus.HEALTHY  # weekly contact still green at 7
    assert status_on(8) == HealthStatus.WATCH  # weekly contact turns red
    assert status_on(31) == HealthStatus.AT_RISK  # adoption and meetings gone too


def test_red_since_records_the_start_of_the_episode_and_clears_on_recovery():
    account = AccountFactory(scoring_baseline_date=days_ago(365))
    CsatFactory(account=account, score=1, recorded_on=TODAY)
    TicketFactory(account=account, ticket_kind=TicketKind.BUG)
    TicketFactory(account=account, ticket_kind=TicketKind.BUG)

    services.recalculate(account, as_of=TODAY)
    account.refresh_from_db()
    assert account.effective_health_status == HealthStatus.AT_RISK
    assert account.red_since == TODAY

    # The episode start does not move while the account stays red.
    services.recalculate(account, as_of=TODAY + dt.timedelta(days=2))
    account.refresh_from_db()
    assert account.red_since == TODAY

    # Recovery clears it.
    account.tickets.update(status="CLOSED", closed_on=TODAY)
    CsatFactory(account=account, score=5, recorded_on=TODAY)
    AdoptionEventFactory(account=account, occurred_on=TODAY)
    ActivityFactory(account=account, activity_type=ActivityType.CALL, occurred_on=TODAY)
    services.recalculate(account, as_of=TODAY)
    account.refresh_from_db()
    assert account.red_since is None


def test_recalculate_defaults_to_today_in_the_business_timezone():
    account = healthy_account()
    services.recalculate(account)
    row = HealthScoreHistory.objects.filter(account=account).latest("calculated_at")
    assert row.calculated_on == dt.date(2026, 6, 15)


# ---------------------------------------------------------------------------
# Meetings are counted once
# ---------------------------------------------------------------------------


def test_a_meeting_does_not_satisfy_weekly_contact_through_the_database_path():
    account = healthy_account(managed=True)
    account.activities.all().delete()
    ActivityFactory(account=account, activity_type=ActivityType.MEETING, occurred_on=days_ago(1))

    result = services.recalculate(account, as_of=TODAY)
    breakdown = result.breakdown()["factors"]

    assert breakdown["WEEKLY_CONTACT"]["color"] == "RED"
    assert breakdown["MEETING_ATTENDANCE"]["detail"]["meetings_in_window"] == 1


def test_voided_activities_are_ignored_by_scoring(csm_user):
    account = AccountFactory(scoring_baseline_date=days_ago(365))
    activity = ActivityFactory(
        account=account, activity_type=ActivityType.CALL, occurred_on=TODAY
    )
    result = services.recalculate(account, as_of=TODAY)
    assert result.breakdown()["factors"]["GENERAL_CONTACT"]["detail"]["days_since"] == 0

    activity.is_voided = True
    activity.save(update_fields=["is_voided"])

    result = services.recalculate(account, as_of=TODAY)
    # Falls back to the baseline floor, which is a year ago.
    assert result.breakdown()["factors"]["GENERAL_CONTACT"]["color"] == "RED"


def test_an_at_risk_reached_only_by_the_cap_still_raises_an_alert(
    django_capture_on_commit_callbacks,
):
    """The cap must participate in the alert lifecycle, not bypass it.

    The alert keys off the effective status rather than the score, so this is
    expected to work - but an account that is At Risk without the arithmetic
    saying so is exactly the shape of thing that slips past a status check
    written against a number.
    """
    from csm.core.enums import TaskType
    from csm.tasks.models import Task

    account = AccountFactory(scoring_baseline_date=days_ago(365))
    AdoptionEventFactory(account=account, occurred_on=days_ago(40))
    CsatFactory(account=account, score=5, recorded_on=TODAY)
    ActivityFactory(
        account=account, activity_type=ActivityType.CALL, occurred_on=days_ago(40)
    )

    # The alert is raised by a subscriber to HealthRecalculated, which
    # publishes on commit - and a test transaction never commits.
    with django_capture_on_commit_callbacks(execute=True):
        result = services.recalculate(account, as_of=TODAY)
    account.refresh_from_db()

    assert result.final_score == Decimal("50.00"), "the arithmetic says Watch"
    assert result.capped_by_red_factors is True
    assert account.effective_health_status == HealthStatus.AT_RISK
    assert account.red_since is not None
    assert Task.objects.filter(
        account=account, task_type=TaskType.AUTO_HEALTH_ALERT, status="PENDING"
    ).exists()
