"""Completing a plan writes the record of it - and what that record counts as.

This is the seam where the interface can move the health score, so the mapping
is asserted rather than described. Every activity type except NOTE counts as
client contact, and a MEETING additionally drives Meeting Attendance, so the
category rule below is the difference between a score that reflects the
relationship and one that reflects how busy somebody looked.
"""

from __future__ import annotations

import datetime as dt

import pytest

from csm.activities.models import Activity
from csm.activities.subscribers import activity_type_for, log_completed_task
from csm.core.enums import (
    GENERAL_CONTACT_TYPES,
    ActivityType,
    TaskCategory,
    TaskProgress,
    TaskStatus,
    TaskType,
)
from csm.tasks.models import Task
from csm.tasks.services import complete_task
from tests.factories import AccountFactory, ContactFactory, UserFactory


@pytest.fixture
def account(db):
    return AccountFactory(client_code="LOG1", client_name="Log Co")


def make(account, **kwargs):
    defaults = {
        "task_type": TaskType.CALL,
        "title": "Ring them",
        "due_date": dt.date.today(),
        "category": TaskCategory.CLIENT_FACING,
        # An activity records who did the thing and the column is not nullable,
        # so every task here has an author the same way a real one does.
        "created_by": account.csm,
    }
    defaults.update(kwargs)
    return Task.objects.create(account=account, **defaults)


def test_a_task_with_nobody_behind_it_logs_nothing(account):
    """Unreachable through the application - ``complete_task`` always stamps
    the person - but a history entry attributed to nobody would be worse than
    none, so it is refused rather than guessed at."""
    orphan = Task.objects.create(
        account=account, task_type=TaskType.CALL, title="Nobody's",
        due_date=dt.date.today(), category=TaskCategory.CLIENT_FACING,
    )
    assert log_completed_task(orphan) is None


# ---------------------------------------------------------------------------
# What a completed item counts as
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "category", "expected"),
    [
        (TaskType.MEETING, TaskCategory.CLIENT_FACING, ActivityType.MEETING),
        (TaskType.CALL, TaskCategory.CLIENT_FACING, ActivityType.CALL),
        (TaskType.MANUAL_NEXT_ACTION, TaskCategory.CLIENT_FACING, ActivityType.OTHER),
        # Not contact with the client, whatever kind it was.
        (TaskType.MEETING, TaskCategory.INTERNAL, ActivityType.NOTE),
        (TaskType.CALL, TaskCategory.PARTNER_FACING, ActivityType.NOTE),
        (TaskType.MANUAL_NEXT_ACTION, TaskCategory.INTERNAL, ActivityType.NOTE),
    ],
)
def test_the_category_decides_what_it_counts_as(account, kind, category, expected):
    task = make(account, task_type=kind, category=category)
    assert activity_type_for(task) == expected


def test_internal_work_is_recorded_without_counting_as_contact(account):
    """The rule that protects the score.

    "Update the renewal spreadsheet" is real work and belongs in the history. It
    is not contact with the client, and a Note counts for neither contact
    factor - which is exactly what NOTE is for.
    """
    task = make(
        account, task_type=TaskType.MANUAL_NEXT_ACTION,
        title="Update the renewal spreadsheet", category=TaskCategory.INTERNAL,
    )
    entry = log_completed_task(task)

    assert entry.activity_type == ActivityType.NOTE
    assert ActivityType.NOTE not in GENERAL_CONTACT_TYPES, (
        "if a Note ever starts counting, this mapping stops being safe"
    )


def test_a_client_facing_call_counts_as_contact(account):
    entry = log_completed_task(make(account, task_type=TaskType.CALL))
    assert entry.activity_type in GENERAL_CONTACT_TYPES


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


def test_completing_a_task_writes_the_log_entry(account, django_capture_on_commit_callbacks):
    actor = UserFactory(csm=True, email="log-actor@dphish.com")
    contact = ContactFactory(account=account, full_name="Dana Farouk")
    task = make(account, contact=contact, description="Ask about the renewal")

    with django_capture_on_commit_callbacks(execute=True):
        complete_task(task, actor=actor)

    entry = Activity.objects.get(task=task)
    assert entry.account_id == account.pk
    assert entry.contact_id == contact.pk
    assert entry.subject == "Ring them"
    assert entry.notes == "Ask about the renewal"
    assert entry.activity_type == ActivityType.CALL


def test_the_entry_is_dated_the_day_it_was_done(account):
    """Not the day it was due. The log records what happened, and it happened
    today - a call finished a week late did not happen a week ago."""
    from csm.core.clock import today

    task = make(account, due_date=dt.date.today() - dt.timedelta(days=7))
    entry = log_completed_task(task)

    assert entry.occurred_on == today()


def test_completing_twice_writes_one_entry(account, django_capture_on_commit_callbacks):
    """Idempotent through the one-to-one column rather than a check-then-insert,
    so a replayed event or a retried job finds the row instead of racing."""
    actor = UserFactory(csm=True, email="log-twice@dphish.com")
    task = make(account)

    with django_capture_on_commit_callbacks(execute=True):
        complete_task(task, actor=actor)
    with django_capture_on_commit_callbacks(execute=True):
        complete_task(task, actor=actor)
    log_completed_task(task)

    assert Activity.objects.filter(task=task).count() == 1


def test_an_open_task_has_written_nothing(account):
    """The whole point of the split. A planned call is not contact until it
    happens, or the score would count intentions."""
    make(account, title="Will ring them")
    assert not Activity.objects.filter(subject="Will ring them").exists()


def test_the_entry_knows_which_plan_produced_it(account):
    task = make(account)
    entry = log_completed_task(task)

    assert entry.task_id == task.pk
    task.refresh_from_db()
    assert task.logged_activity.pk == entry.pk


def test_completing_moves_the_score(account, django_capture_on_commit_callbacks):
    """End to end: the plan closes, the record appears, the engine reads it.

    Nothing in the task code calls the scoring engine - the Activity's own
    post_save hook does, which is why this works at all.
    """
    from csm.health import services

    account.scoring_baseline_date = dt.date.today() - dt.timedelta(days=365)
    account.save(update_fields=["scoring_baseline_date"])
    services.recalculate(account, as_of=dt.date.today())
    account.refresh_from_db()
    before = account.computed_health_score

    actor = UserFactory(csm=True, email="log-score@dphish.com")
    with django_capture_on_commit_callbacks(execute=True):
        complete_task(make(account, task_type=TaskType.MEETING), actor=actor)

    account.refresh_from_db()
    assert account.computed_health_score >= before, (
        "contact that happened should never lower the score"
    )
    assert Activity.objects.filter(account=account).count() == 1


def test_an_internal_task_completing_does_not_raise_the_contact_factors(
    account, django_capture_on_commit_callbacks
):
    """The failure this design exists to prevent: a score that rises because
    somebody ticked off their own admin."""
    actor = UserFactory(csm=True, email="log-internal@dphish.com")
    task = make(account, category=TaskCategory.INTERNAL, title="Tidy the sheet")

    with django_capture_on_commit_callbacks(execute=True):
        complete_task(task, actor=actor)

    assert Activity.objects.counting_for_general_contact().count() == 0
    assert Activity.objects.filter(task=task).count() == 1, "still in the history"


def test_the_status_and_the_lifecycle_cannot_disagree(account, django_capture_on_commit_callbacks):
    actor = UserFactory(csm=True, email="log-status@dphish.com")
    task = make(account, progress=TaskProgress.IN_PROGRESS)

    with django_capture_on_commit_callbacks(execute=True):
        complete_task(task, actor=actor)

    task.refresh_from_db()
    assert task.status == TaskStatus.DONE
    assert task.progress == TaskProgress.COMPLETED
