"""Notification tests.

The rule under test is that notifications follow assignment and nothing else.
"""

from __future__ import annotations

import datetime as dt

import pytest
import time_machine
from django.core import mail

from csm.core.enums import NotificationChannel, NotificationKind, TaskCategory, TaskType
from csm.health import services as health_services
from csm.notifications.dispatcher import (
    send_digests,
    send_pending_emails,
    send_task_reminders,
)
from csm.notifications.models import Notification
from csm.tasks import generators
from csm.tasks.services import create_task
from tests.factories import AccountFactory, CsatFactory, TicketFactory, UserFactory

pytestmark = pytest.mark.django_db

TODAY = dt.date(2026, 6, 15)


@pytest.fixture(autouse=True)
def _pin_the_clock():
    with time_machine.travel("2026-06-15 09:00:00+00:00", tick=False):
        yield


def in_days(n: int) -> dt.date:
    return TODAY + dt.timedelta(days=n)


def test_generating_a_task_notifies_nobody_at_the_time(
    django_capture_on_commit_callbacks,
):
    """The generators run at one in the morning.

    Notifying then produced a pile of messages nobody was awake for, and
    silence on the morning the work was actually due. The digest at seven is
    what announces these, so creation itself says nothing.
    """
    AccountFactory(contract_end_date=in_days(90))
    with django_capture_on_commit_callbacks(execute=True):
        generators.generate_renewal_tasks(as_of=TODAY)

    assert not Notification.objects.exists()


def test_the_digest_is_what_announces_a_generated_task(
    django_capture_on_commit_callbacks, csm_user
):
    """The other half of the rule above: silent at creation, named in the
    morning. Without this the change would simply have lost the notification."""
    account = AccountFactory(csm=csm_user, acm=None, contract_end_date=in_days(90))
    with django_capture_on_commit_callbacks(execute=True):
        generators.generate_renewal_tasks(as_of=TODAY)

    assert send_digests(as_of=TODAY) == 1
    digest = Notification.objects.get(
        user=csm_user, channel=NotificationChannel.EMAIL
    )
    assert account.client_name in digest.body


def test_a_health_alert_notifies_the_csm_alone_and_emails_immediately(
    django_capture_on_commit_callbacks,
):
    account = AccountFactory(scoring_baseline_date=TODAY - dt.timedelta(days=365))
    CsatFactory(account=account, score=1, recorded_on=TODAY)
    TicketFactory(account=account, ticket_kind="BUG")
    TicketFactory(account=account, ticket_kind="BUG")

    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    alerts = Notification.objects.filter(kind=NotificationKind.HEALTH_ALERT)
    assert set(alerts.values_list("user_id", flat=True)) == {account.csm_id}
    # A health alert does not wait for the morning digest.
    assert alerts.filter(channel=NotificationChannel.EMAIL).exists()


def test_a_senior_csm_with_no_assigned_accounts_receives_nothing(
    django_capture_on_commit_callbacks, senior_csm_user
):
    """Access is broad, notifications are not. This is the whole rule."""
    AccountFactory(contract_end_date=in_days(90))
    AccountFactory(contract_end_date=in_days(60), client_code="SECOND")

    with django_capture_on_commit_callbacks(execute=True):
        generators.generate_renewal_tasks(as_of=TODAY)

    assert not Notification.objects.filter(user=senior_csm_user).exists()


def test_notifying_twice_for_the_same_occasion_creates_one_notification(
    django_capture_on_commit_callbacks,
):
    """Through the health alert, which is the one thing still notified at once."""
    account = AccountFactory(scoring_baseline_date=TODAY - dt.timedelta(days=365))
    CsatFactory(account=account, score=1, recorded_on=TODAY)
    TicketFactory(account=account, ticket_kind="BUG")
    TicketFactory(account=account, ticket_kind="BUG")

    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)
    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=TODAY)

    # One alert task (the partial unique index), and one message about it (the
    # dedupe key). The second recalculation finds both already there.
    assert Notification.objects.filter(channel=NotificationChannel.IN_APP).count() == 1


def test_an_inactive_user_is_not_notified(django_capture_on_commit_callbacks):
    """Checked on the digest, since that is where routine tasks now surface."""
    dormant = UserFactory(
        csm=True, email="dormant@dphish.com", full_name="Ari Stone", is_active=False
    )
    account = AccountFactory(contract_end_date=in_days(90), csm=dormant, acm=None)

    with django_capture_on_commit_callbacks(execute=True):
        generators.generate_renewal_tasks(as_of=TODAY)
    send_digests(as_of=TODAY)

    assert not Notification.objects.filter(user=dormant).exists()
    assert account.tasks.exists(), "the task is still raised; only the message is withheld"


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def test_the_digest_gathers_a_persons_due_tasks_into_one_email(
    django_capture_on_commit_callbacks, csm_user
):
    account = AccountFactory(csm=csm_user, acm=None)
    with django_capture_on_commit_callbacks(execute=True):
        for n in range(3):
            create_task(
                account=account,
                task_type=TaskType.MANUAL_NEXT_ACTION,
                title=f"Follow up {n}",
                due_date=TODAY,
                category=TaskCategory.INTERNAL,
                created_by=csm_user,
            )

    assert send_digests(as_of=TODAY) == 1
    digest = Notification.objects.get(dedupe_key__startswith="digest:")
    assert "Today (3):" in digest.body
    assert digest.body.count("Follow up") == 3


def test_the_digest_runs_once_a_day_however_often_the_job_runs(
    django_capture_on_commit_callbacks, csm_user
):
    account = AccountFactory(csm=csm_user, acm=None)
    with django_capture_on_commit_callbacks(execute=True):
        create_task(
            account=account,
            task_type=TaskType.MANUAL_NEXT_ACTION,
            title="Follow up",
            due_date=TODAY,
            created_by=csm_user,
        )

    assert send_digests(as_of=TODAY) == 1
    assert send_digests(as_of=TODAY) == 0


def test_health_alerts_stay_out_of_the_digest(django_capture_on_commit_callbacks, csm_user):
    """They have already been emailed on their own; repeating them is noise."""
    account = AccountFactory(csm=csm_user, acm=None)
    with django_capture_on_commit_callbacks(execute=True):
        create_task(
            account=account,
            task_type=TaskType.AUTO_HEALTH_ALERT,
            title="At Risk",
            due_date=TODAY,
        )

    assert send_digests(as_of=TODAY) == 0


def test_sending_marks_the_outbox_row_and_does_not_resend(
    django_capture_on_commit_callbacks, csm_user
):
    account = AccountFactory(csm=csm_user, acm=None)
    with django_capture_on_commit_callbacks(execute=True):
        create_task(
            account=account,
            task_type=TaskType.MANUAL_NEXT_ACTION,
            title="Follow up",
            due_date=TODAY,
            created_by=csm_user,
        )
    send_digests(as_of=TODAY)

    assert send_pending_emails() == 1
    assert len(mail.outbox) == 1
    assert send_pending_emails() == 0
    assert len(mail.outbox) == 1


# ---------------------------------------------------------------------------
# Reminders before a timed task
# ---------------------------------------------------------------------------
# 09:00 in Cairo is 06:00 UTC in June, when Egypt is on +03:00. Every instant
# below is written in UTC so the test says which moment it means, and the
# conversion is the application's own.


@pytest.fixture
def timed_task(db, csm_user):
    from csm.tasks.models import Task, TaskAssignment

    account = AccountFactory(csm=csm_user, acm=None)
    task = Task.objects.create(
        account=account,
        task_type=TaskType.MANUAL_NEXT_ACTION,
        title="Call the CISO",
        due_date=TODAY,
        due_time=dt.time(9, 0),
        created_by=csm_user,
    )
    TaskAssignment.objects.create(task=task, user=csm_user)
    return task


def reminders_at(moment: str) -> int:
    with time_machine.travel(moment, tick=False):
        return send_task_reminders()


def test_a_timed_task_reminds_an_hour_and_half_an_hour_before(timed_task, csm_user):
    """The whole point of recording a time.

    Due 09:00 Cairo = 06:00 UTC, so the reminders belong at 05:00 and 05:30 UTC.
    """
    assert reminders_at("2026-06-15 04:30:00+00:00") == 0, "too early to be useful"
    assert reminders_at("2026-06-15 05:00:00+00:00") == 1
    assert reminders_at("2026-06-15 05:10:00+00:00") == 0, "the hour one already went"
    assert reminders_at("2026-06-15 05:30:00+00:00") == 1
    assert reminders_at("2026-06-15 05:35:00+00:00") == 0

    titles = sorted(
        Notification.objects.filter(
            user=csm_user, channel=NotificationChannel.IN_APP
        ).values_list("title", flat=True)
    )
    assert titles == ["In 30 minutes: Call the CISO", "In 60 minutes: Call the CISO"]


def test_a_reminder_goes_out_by_email_as_well(timed_task, csm_user):
    """A reminder that waits for tomorrow's digest is not a reminder."""
    reminders_at("2026-06-15 05:00:00+00:00")
    with time_machine.travel("2026-06-15 05:00:30+00:00", tick=False):
        assert send_pending_emails() == 1
    assert "In 60 minutes" in mail.outbox[-1].subject


def test_a_reminder_too_late_to_be_true_is_dropped(timed_task):
    """If the worker was down over the hour mark, "in one hour" delivered at
    twenty past is worse than silence - and the half-hour one is still to come."""
    assert reminders_at("2026-06-15 05:20:00+00:00") == 0
    assert reminders_at("2026-06-15 05:30:00+00:00") == 1


def test_a_task_with_no_time_is_never_reminded(db, csm_user):
    from csm.tasks.models import Task, TaskAssignment

    account = AccountFactory(csm=csm_user, acm=None)
    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION,
        title="Some day this week", due_date=TODAY, created_by=csm_user,
    )
    TaskAssignment.objects.create(task=task, user=csm_user)

    for moment in ("2026-06-15 05:00:00+00:00", "2026-06-15 20:00:00+00:00"):
        assert reminders_at(moment) == 0

    assert not Notification.objects.filter(task=task).exists()


def test_a_finished_task_stops_reminding(timed_task, csm_user):
    from csm.core.enums import TaskStatus

    timed_task.status = TaskStatus.DONE
    timed_task.completed_at = dt.datetime.now(dt.UTC)
    timed_task.save(update_fields=["status", "completed_at"])

    assert reminders_at("2026-06-15 05:00:00+00:00") == 0


def test_a_reminder_reaches_only_the_people_the_task_routed_to(timed_task, csm_user):
    """Same rule as every other notification: recipients come from the
    assignments and are never widened."""
    stranger = UserFactory(csm=True, email="stranger-remind@dphish.com")

    reminders_at("2026-06-15 05:00:00+00:00")

    assert Notification.objects.filter(user=csm_user).exists()
    assert not Notification.objects.filter(user=stranger).exists()


def test_a_task_due_just_after_midnight_is_reminded_the_night_before(db, csm_user):
    """The hour before 00:15 is the previous day, so the query cannot look at
    today alone."""
    from csm.tasks.models import Task, TaskAssignment

    account = AccountFactory(csm=csm_user, acm=None)
    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION,
        title="Overnight window", due_date=TODAY + dt.timedelta(days=1),
        due_time=dt.time(0, 15), created_by=csm_user,
    )
    TaskAssignment.objects.create(task=task, user=csm_user)

    # 00:15 on the 16th, Cairo (+03:00) = 21:15 UTC on the 15th; an hour before
    # that is 20:15 UTC, still the 15th in both calendars.
    assert reminders_at("2026-06-15 20:15:00+00:00") == 1


# ---------------------------------------------------------------------------
# What the morning digest carries
# ---------------------------------------------------------------------------


def test_the_digest_looks_a_week_ahead_and_groups_what_it_finds(
    django_capture_on_commit_callbacks, csm_user
):
    """A renewal you first hear about on the morning it is due is an apology."""
    account = AccountFactory(csm=csm_user, acm=None)
    with django_capture_on_commit_callbacks(execute=True):
        for title, day in (
            ("Late one", TODAY - dt.timedelta(days=2)),
            ("Due today", TODAY),
            ("Later this week", TODAY + dt.timedelta(days=3)),
            ("Next month", TODAY + dt.timedelta(days=40)),
        ):
            create_task(
                account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title=title,
                due_date=day, category=TaskCategory.INTERNAL, created_by=csm_user,
            )

    assert send_digests(as_of=TODAY) == 1
    body = Notification.objects.get(
        user=csm_user, channel=NotificationChannel.EMAIL
    ).body

    assert "Overdue (1):" in body
    assert "Today (1):" in body
    assert "Next 7 days (1):" in body
    assert "Next month" not in body, "beyond the horizon, it waits its turn"
    assert body.index("Overdue") < body.index("Today") < body.index("Next 7 days")


def test_the_digest_also_reaches_the_bell(
    django_capture_on_commit_callbacks, csm_user
):
    """It is now the only thing that announces a routine task, so somebody who
    does not read email would otherwise never be told."""
    account = AccountFactory(csm=csm_user, acm=None)
    with django_capture_on_commit_callbacks(execute=True):
        create_task(
            account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Read me",
            due_date=TODAY, category=TaskCategory.INTERNAL, created_by=csm_user,
        )

    send_digests(as_of=TODAY)
    in_app = Notification.objects.get(user=csm_user, channel=NotificationChannel.IN_APP)
    assert "1 open" in in_app.title
    assert in_app.url, "and it goes somewhere"


def test_the_digest_still_sends_only_once_a_day(
    django_capture_on_commit_callbacks, csm_user
):
    account = AccountFactory(csm=csm_user, acm=None)
    with django_capture_on_commit_callbacks(execute=True):
        create_task(
            account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Once",
            due_date=TODAY, category=TaskCategory.INTERNAL, created_by=csm_user,
        )

    assert send_digests(as_of=TODAY) == 1
    assert send_digests(as_of=TODAY) == 0
    assert Notification.objects.filter(channel=NotificationChannel.EMAIL).count() == 1
