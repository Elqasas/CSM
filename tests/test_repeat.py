"""Repeating tasks: one rule, one live occurrence.

The stepping is pure arithmetic over dates, so most of this needs no database -
the same reason the scoring engine's tests do not. What does need one is the
part that matters: that a repeat produces exactly one open task at a time, and
that finishing one produces the next.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from csm.core.enums import (
    OPEN_TASK_STATUSES,
    RepeatEnd,
    RepeatFrequency,
    TaskCategory,
    TaskType,
)
from csm.tasks import recurrence
from csm.tasks.models import Task, TaskSeries
from csm.tasks.services import complete_task
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login


@pytest.fixture
def account(db):
    csm = UserFactory(csm=True, email="rep-csm@dphish.com", full_name="Casey Morgan")
    csm.set_password(PASSWORD)
    csm.save(update_fields=["password"])
    return AccountFactory(client_code="REP1", client_name="Repeat Co", csm=csm, acm=None)


def series_for(account, **kwargs):
    defaults = {
        "task_type": TaskType.MEETING,
        "title": "Weekly sync",
        "category": TaskCategory.CLIENT_FACING,
        "frequency": RepeatFrequency.WEEKLY,
        "ends": RepeatEnd.NEVER,
        "created_by": account.csm,
    }
    defaults.update(kwargs)
    return TaskSeries.objects.create(account=account, **defaults)


# ---------------------------------------------------------------------------
# Stepping - no database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frequency", "start", "expected"),
    [
        (RepeatFrequency.DAILY, dt.date(2026, 6, 15), dt.date(2026, 6, 16)),
        (RepeatFrequency.WEEKLY, dt.date(2026, 6, 15), dt.date(2026, 6, 22)),
        (RepeatFrequency.MONTHLY, dt.date(2026, 6, 15), dt.date(2026, 7, 15)),
        (RepeatFrequency.QUARTERLY, dt.date(2026, 6, 15), dt.date(2026, 9, 15)),
        (RepeatFrequency.YEARLY, dt.date(2026, 6, 15), dt.date(2027, 6, 15)),
    ],
)
def test_each_frequency_steps_by_its_own_unit(frequency, start, expected):
    from csm.tasks.models import _step

    assert _step(start, frequency) == expected


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (dt.date(2026, 1, 31), dt.date(2026, 2, 28)),
        (dt.date(2028, 1, 31), dt.date(2028, 2, 29)),
        (dt.date(2026, 3, 31), dt.date(2026, 4, 30)),
        (dt.date(2026, 12, 31), dt.date(2027, 1, 31)),
    ],
)
def test_a_monthly_repeat_clamps_to_the_end_of_a_short_month(start, expected):
    """31 January plus a month is 28 February, not 3 March.

    Adding thirty days instead would walk a month-end task a few days forward
    every February and never walk it back.
    """
    from csm.tasks.models import _step

    assert _step(start, RepeatFrequency.MONTHLY) == expected


# ---------------------------------------------------------------------------
# When it stops
# ---------------------------------------------------------------------------


def test_never_keeps_going(account):
    series = series_for(account, ends=RepeatEnd.NEVER)
    assert series.next_due(dt.date(2026, 6, 15)) == dt.date(2026, 6, 22)


def test_after_a_number_of_times_stops_at_that_number(account):
    series = series_for(account, ends=RepeatEnd.AFTER, occurrences=3, created_count=2)
    assert series.next_due(dt.date(2026, 6, 15)) == dt.date(2026, 6, 22)

    series.created_count = 3
    assert series.next_due(dt.date(2026, 6, 15)) is None


def test_on_a_date_stops_once_the_next_one_would_pass_it(account):
    series = series_for(
        account, ends=RepeatEnd.ON, until=dt.date(2026, 6, 25)
    )
    assert series.next_due(dt.date(2026, 6, 15)) == dt.date(2026, 6, 22)
    assert series.next_due(dt.date(2026, 6, 22)) is None


def test_a_finished_series_stops_being_looked_at(
    account, django_capture_on_commit_callbacks
):
    """Marked inactive rather than recomputing the same None every night."""
    series = series_for(account, ends=RepeatEnd.AFTER, occurrences=1)
    recurrence.start(series, first_due=dt.date.today())
    task = Task.objects.get(series=series)

    # The next occurrence is created on the event bus, which delivers on
    # commit - so a test transaction has to be told to run the callbacks.
    with django_capture_on_commit_callbacks(execute=True):
        complete_task(task, actor=account.csm)
    series.refresh_from_db()

    assert series.is_active is False
    assert Task.objects.filter(series=series).count() == 1


# ---------------------------------------------------------------------------
# One at a time
# ---------------------------------------------------------------------------


def test_finishing_one_creates_the_next(account, django_capture_on_commit_callbacks):
    series = series_for(account)
    first = recurrence.start(series, first_due=dt.date(2026, 6, 15))

    with django_capture_on_commit_callbacks(execute=True):
        complete_task(first, actor=account.csm)

    nxt = Task.objects.filter(series=series).exclude(pk=first.pk).get()
    assert nxt.due_date == dt.date(2026, 6, 22)
    assert nxt.title == series.title
    assert nxt.task_type == series.task_type


def test_only_one_occurrence_is_open_at_a_time(account):
    """A repeat you have not done sits there overdue rather than breeding
    copies of itself, which is what keeps the queue honest."""
    series = series_for(account, frequency=RepeatFrequency.DAILY)
    recurrence.start(series, first_due=dt.date.today() - dt.timedelta(days=10))

    for _ in range(5):
        recurrence.top_up(dt.date.today())

    open_ones = Task.objects.filter(series=series, status__in=list(OPEN_TASK_STATUSES))
    assert open_ones.count() == 1


def test_a_cancelled_occurrence_does_not_kill_the_series(account):
    """The daily pass exists for exactly this: the completion event never fired,
    so nothing else would ever create the next one."""
    from csm.tasks.services import delete_task

    series = series_for(account)
    first = recurrence.start(series, first_due=dt.date.today())
    delete_task(first)

    assert recurrence.top_up(dt.date.today()) == 1
    assert Task.objects.filter(
        series=series, status__in=list(OPEN_TASK_STATUSES)
    ).count() == 1


def test_extending_twice_creates_one_task(account):
    """Idempotent through the auto_key, like every other generator here."""
    series = series_for(account)
    first = recurrence.start(series, first_due=dt.date(2026, 6, 15))
    first.status = "DONE"
    first.completed_at = dt.datetime.now(dt.UTC)
    first.save(update_fields=["status", "completed_at"])

    recurrence.extend(series, after=dt.date(2026, 6, 15))
    recurrence.extend(series, after=dt.date(2026, 6, 15))

    assert Task.objects.filter(series=series).count() == 2


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------


def post_task(client, account, **extra):
    data = {
        "task_type": "MEETING", "title": "Weekly sync",
        "due_date": dt.date.today().isoformat(), "due_time": "",
        "category": "CLIENT_FACING", "priority": "MEDIUM",
        "progress": "NOT_STARTED", "description": "", "contact": "",
    }
    data.update(extra)
    return client.post(reverse("tasks:create", args=[account.pk]), data)


def test_creating_a_repeating_task_makes_a_series_and_one_occurrence(client, account):
    login(client, account.csm)
    post_task(
        client, account,
        repeat="on", repeat_frequency="WEEKLY", repeat_ends="NEVER",
    )

    series = TaskSeries.objects.get()
    assert series.frequency == "WEEKLY"
    assert series.ends == "NEVER"
    assert Task.objects.filter(series=series).count() == 1
    assert series.created_count == 1


def test_a_task_without_the_switch_makes_no_series(client, account):
    login(client, account.csm)
    post_task(client, account)

    assert not TaskSeries.objects.exists()
    assert Task.objects.get().series_id is None


def test_after_with_no_number_is_refused(client, account):
    """It would otherwise become "never", which is the one answer nobody can
    undo by waiting."""
    login(client, account.csm)
    response = post_task(
        client, account, repeat="on", repeat_frequency="DAILY", repeat_ends="AFTER"
    )

    assert response.status_code == 200
    assert "How many times?" in response.content.decode()
    assert not TaskSeries.objects.exists()


def test_on_with_no_date_is_refused(client, account):
    login(client, account.csm)
    response = post_task(
        client, account, repeat="on", repeat_frequency="DAILY", repeat_ends="ON"
    )

    assert response.status_code == 200
    assert "Give the date it stops on" in response.content.decode()
    assert not TaskSeries.objects.exists()


def test_an_end_date_before_the_first_one_is_refused(client, account):
    login(client, account.csm)
    response = post_task(
        client, account, repeat="on", repeat_frequency="WEEKLY", repeat_ends="ON",
        repeat_until=(dt.date.today() - dt.timedelta(days=1)).isoformat(),
    )

    assert response.status_code == 200
    assert "it would never repeat at all" in response.content.decode()


def test_the_form_offers_the_repeat_block(client, account):
    login(client, account.csm)
    body = client.get(reverse("tasks:create", args=[account.pk])).content.decode()

    assert 'name="repeat"' in body
    assert "Repeat type" in body
    for word in ("Never", "After", "On"):
        assert word in body, word
