"""Generator tests: idempotency, the renewal cycle and the alert lifecycle."""

from __future__ import annotations

import datetime as dt

import pytest
import time_machine

from csm.core.enums import (
    ActivityType,
    HealthStatus,
    LifecycleStage,
    TaskCloseReason,
    TaskStatus,
    TaskType,
    TicketKind,
)
from csm.health import services as health_services
from csm.tasks import generators
from csm.tasks.models import Task
from csm.tasks.services import complete_task, delete_task
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
    with time_machine.travel("2026-06-15 09:00:00+00:00", tick=False):
        yield


def days_ago(n: int) -> dt.date:
    return TODAY - dt.timedelta(days=n)


def in_days(n: int) -> dt.date:
    return TODAY + dt.timedelta(days=n)


# ---------------------------------------------------------------------------
# Auto-Renewal
# ---------------------------------------------------------------------------


def test_renewal_generates_one_task_at_each_checkpoint():
    AccountFactory(contract_end_date=in_days(90))

    assert generators.generate_renewal_tasks(as_of=TODAY) == 1
    task = Task.objects.get(task_type=TaskType.AUTO_RENEWAL)
    assert task.due_date == TODAY
    assert "90 days" in task.title


def test_running_the_generator_repeatedly_creates_nothing_further():
    """The key contains no timestamp, so the job is safe to run any number of times."""
    AccountFactory(contract_end_date=in_days(60))

    for _ in range(5):
        generators.generate_renewal_tasks(as_of=TODAY)

    assert Task.objects.filter(task_type=TaskType.AUTO_RENEWAL).count() == 1


def test_the_three_checkpoints_are_separate_tasks():
    account = AccountFactory(contract_end_date=in_days(90))
    generators.generate_renewal_tasks(as_of=TODAY)
    generators.generate_renewal_tasks(as_of=in_days(30))  # the 60-day mark
    generators.generate_renewal_tasks(as_of=in_days(60))  # the 30-day mark

    tasks = Task.objects.filter(account=account, task_type=TaskType.AUTO_RENEWAL)
    assert tasks.count() == 3
    assert sorted(t.due_date for t in tasks) == [TODAY, in_days(30), in_days(60)]


def test_closing_the_ninety_day_task_does_not_suppress_the_sixty_day_one(csm_user):
    AccountFactory(contract_end_date=in_days(90))
    generators.generate_renewal_tasks(as_of=TODAY)
    complete_task(Task.objects.get(), actor=csm_user)

    generators.generate_renewal_tasks(as_of=in_days(30))
    assert Task.objects.filter(task_type=TaskType.AUTO_RENEWAL).count() == 2


def test_an_old_checkpoint_is_not_resurrected_after_completion(csm_user):
    AccountFactory(contract_end_date=in_days(90))
    generators.generate_renewal_tasks(as_of=TODAY)
    complete_task(Task.objects.get(), actor=csm_user)

    generators.generate_renewal_tasks(as_of=TODAY)
    assert Task.objects.count() == 1


def test_a_missed_checkpoint_is_caught_up_within_a_week_but_no_further():
    AccountFactory(contract_end_date=in_days(90))
    # Six days late: still inside the catch-up window.
    assert generators.generate_renewal_tasks(as_of=in_days(6)) == 1

    Task.objects.all().delete()
    AccountFactory(contract_end_date=in_days(90), client_code="LATE")
    # Eight days late: the checkpoint is allowed to lapse rather than arrive
    # already overdue.
    assert generators.generate_renewal_tasks(as_of=in_days(8)) == 0


def test_no_renewal_reminders_for_closed_lost_or_churned_accounts():
    AccountFactory(contract_end_date=in_days(90), lifecycle_stage=LifecycleStage.CHURNED)
    AccountFactory(
        contract_end_date=in_days(90),
        lifecycle_stage=LifecycleStage.CLOSED_LOST,
        client_code="LOST1",
    )
    assert generators.generate_renewal_tasks(as_of=TODAY) == 0


def test_renewing_the_contract_starts_a_fresh_cycle_automatically():
    """The end date is part of the key, so no reset logic is needed."""
    account = AccountFactory(contract_end_date=in_days(90))
    generators.generate_renewal_tasks(as_of=TODAY)

    old_end = account.contract_end_date
    account.contract_end_date = old_end + dt.timedelta(days=365)
    account.save(update_fields=["contract_end_date"])

    cancelled = generators.cancel_stale_renewal_tasks(account, old_end)
    assert cancelled == 1
    assert Task.objects.get().close_reason == TaskCloseReason.CONTRACT_DATE_CHANGED

    # The new cycle begins on its own when the new ninety-day mark arrives.
    assert generators.generate_renewal_tasks(as_of=in_days(365)) == 1


def test_import_catchup_creates_only_the_most_urgent_checkpoint():
    """An account imported twenty days from renewal gets one task, not three."""
    account = AccountFactory(contract_end_date=in_days(20))

    task = generators.generate_renewal_catchup(account, as_of=TODAY)

    assert task is not None
    assert Task.objects.filter(task_type=TaskType.AUTO_RENEWAL).count() == 1
    assert task.auto_key.endswith(":30")
    assert task.due_date == TODAY


def test_import_catchup_does_nothing_outside_the_renewal_window():
    account = AccountFactory(contract_end_date=in_days(200))
    assert generators.generate_renewal_catchup(account, as_of=TODAY) is None


# ---------------------------------------------------------------------------
# Auto-Quarterly-Meeting
# ---------------------------------------------------------------------------


def test_quarterly_task_appears_seven_days_before_the_meeting():
    AccountFactory(quarterly_meeting_date=in_days(7))
    assert generators.generate_quarterly_tasks(as_of=TODAY) == 1
    assert Task.objects.get().due_date == TODAY


def test_quarterly_task_is_not_created_too_early():
    AccountFactory(quarterly_meeting_date=in_days(8))
    assert generators.generate_quarterly_tasks(as_of=TODAY) == 0


def test_quarterly_generation_is_idempotent():
    AccountFactory(quarterly_meeting_date=in_days(3))
    for _ in range(4):
        generators.generate_quarterly_tasks(as_of=TODAY)
    assert Task.objects.count() == 1


def test_a_stale_meeting_date_produces_a_nudge_to_set_the_next_one():
    """Otherwise a manually kept field goes stale and reminders stop for good."""
    AccountFactory(quarterly_meeting_date=days_ago(40))

    assert generators.generate_quarterly_tasks(as_of=TODAY) == 1
    task = Task.objects.get()
    assert "Schedule the next quarterly meeting" in task.title
    assert task.auto_key.startswith("qmeeting_rollover:")

    # And only once.
    assert generators.generate_quarterly_tasks(as_of=TODAY) == 0


def test_rescheduling_a_meeting_cancels_the_reminder_for_the_old_date():
    account = AccountFactory(quarterly_meeting_date=in_days(3))
    generators.generate_quarterly_tasks(as_of=TODAY)
    old_date = account.quarterly_meeting_date

    account.quarterly_meeting_date = in_days(40)
    account.save(update_fields=["quarterly_meeting_date"])
    assert generators.cancel_stale_quarterly_tasks(account, old_date) == 1
    assert Task.objects.get().close_reason == TaskCloseReason.MEETING_DATE_CHANGED


# ---------------------------------------------------------------------------
# Auto-HealthAlert
# ---------------------------------------------------------------------------


def at_risk_account(**kwargs):
    """An account whose factors put it well into the red."""
    account = AccountFactory(scoring_baseline_date=days_ago(365), **kwargs)
    CsatFactory(account=account, score=1, recorded_on=TODAY)
    TicketFactory(account=account, ticket_kind=TicketKind.BUG)
    TicketFactory(account=account, ticket_kind=TicketKind.BUG)
    return account


def recover(account):
    account.tickets.update(status="CLOSED", closed_on=TODAY)
    CsatFactory(account=account, score=5, recorded_on=TODAY)
    AdoptionEventFactory(account=account, occurred_on=TODAY)
    ActivityFactory(account=account, activity_type=ActivityType.CALL, occurred_on=TODAY)


def test_turning_red_opens_exactly_one_alert(django_capture_on_commit_callbacks):
    account = at_risk_account()
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    alerts = Task.objects.filter(task_type=TaskType.AUTO_HEALTH_ALERT)
    assert alerts.count() == 1
    assert alerts.get().status == TaskStatus.PENDING


def test_repeated_recalculation_while_red_does_not_pile_up_alerts(
    django_capture_on_commit_callbacks,
):
    account = at_risk_account()
    for offset in range(4):
        with django_capture_on_commit_callbacks(execute=True):
            health_services.recalculate(account, as_of=TODAY + dt.timedelta(days=offset))

    assert Task.objects.filter(task_type=TaskType.AUTO_HEALTH_ALERT).count() == 1


def test_the_alert_names_the_weakest_factors(django_capture_on_commit_callbacks):
    account = at_risk_account()
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    alert = Task.objects.get(task_type=TaskType.AUTO_HEALTH_ALERT)
    assert "At Risk" in alert.description
    assert "Weakest factors" in alert.description
    assert "Open Tickets" in alert.description


def test_recovering_closes_the_alert_automatically(django_capture_on_commit_callbacks):
    account = at_risk_account()
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    recover(account)
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    alert = Task.objects.get(task_type=TaskType.AUTO_HEALTH_ALERT)
    assert alert.status == TaskStatus.AUTO_CLOSED
    assert alert.close_reason == TaskCloseReason.HEALTH_RECOVERED


def test_closing_an_alert_by_hand_while_still_red_is_final(
    django_capture_on_commit_callbacks, csm_user
):
    account = at_risk_account()
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    complete_task(Task.objects.get(task_type=TaskType.AUTO_HEALTH_ALERT), actor=csm_user)

    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=in_days(1))

    assert Task.objects.filter(task_type=TaskType.AUTO_HEALTH_ALERT).count() == 1


def test_a_new_red_episode_after_recovery_earns_a_new_alert(
    django_capture_on_commit_callbacks, csm_user
):
    account = at_risk_account()
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)
    complete_task(Task.objects.get(task_type=TaskType.AUTO_HEALTH_ALERT), actor=csm_user)

    recover(account)
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=in_days(1))

    # Relapse: bad CSAT and bugs again, and by day forty the contact and
    # adoption factors have decayed back to red as well.
    CsatFactory(account=account, score=1, recorded_on=in_days(2))
    TicketFactory(account=account, ticket_kind=TicketKind.BUG)
    TicketFactory(account=account, ticket_kind=TicketKind.BUG)
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=in_days(40))

    alerts = Task.objects.filter(task_type=TaskType.AUTO_HEALTH_ALERT)
    assert alerts.count() == 2
    assert alerts.filter(status=TaskStatus.PENDING).count() == 1


def test_moving_to_an_unscored_stage_closes_the_alert(django_capture_on_commit_callbacks):
    account = at_risk_account()
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    account.lifecycle_stage = LifecycleStage.CHURNED
    account.save(update_fields=["lifecycle_stage"])
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    assert Task.objects.get(task_type=TaskType.AUTO_HEALTH_ALERT).status == TaskStatus.AUTO_CLOSED


def test_an_override_to_a_non_red_status_closes_the_alert(
    django_capture_on_commit_callbacks, csm_user
):
    """The alert follows the effective status, so an override silences it."""
    account = at_risk_account()
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    with django_capture_on_commit_callbacks(execute=True):
        health_services.apply_override(
            account,
            status=HealthStatus.WATCH.value,
            reason="Known issue, the fix ships on Thursday and the client agrees.",
            actor=csm_user,
        )

    assert Task.objects.get(task_type=TaskType.AUTO_HEALTH_ALERT).status == TaskStatus.AUTO_CLOSED


def test_a_health_alert_can_never_exist_on_a_poc_account(
    django_capture_on_commit_callbacks,
):
    """A cross-engine invariant worth asserting rather than trusting.

    POC accounts are not scored, so they can never be At Risk and can never
    generate an alert. If that ever changed, the POC routing rule would send
    health information to an ACM, which is the one thing the design forbids.
    """
    account = at_risk_account(lifecycle_stage=LifecycleStage.POC)
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    assert not Task.objects.filter(task_type=TaskType.AUTO_HEALTH_ALERT).exists()


# ---------------------------------------------------------------------------
# Deleting generated tasks
# ---------------------------------------------------------------------------


def test_deleting_a_generated_task_cancels_it_rather_than_freeing_its_key():
    """A hard delete would let the next run recreate it, which looks like a bug."""
    AccountFactory(contract_end_date=in_days(90))
    generators.generate_renewal_tasks(as_of=TODAY)
    task = Task.objects.get()

    delete_task(task)
    task.refresh_from_db()
    assert task.status == TaskStatus.CANCELLED

    generators.generate_renewal_tasks(as_of=TODAY)
    assert Task.objects.count() == 1


def test_assignments_follow_the_routing_table():
    account = AccountFactory(contract_end_date=in_days(90))
    generators.generate_renewal_tasks(as_of=TODAY)
    task = Task.objects.get()

    assert {a.user_id for a in task.assignments.all()} == {account.csm_id, account.acm_id}


def test_a_health_alert_is_assigned_to_the_csm_alone(django_capture_on_commit_callbacks):
    account = at_risk_account()
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    alert = Task.objects.get(task_type=TaskType.AUTO_HEALTH_ALERT)
    assert {a.user_id for a in alert.assignments.all()} == {account.csm_id}
