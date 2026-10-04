"""Completing a plan writes the record of it.

A task, a meeting or a call is an obligation. Finishing one means the thing
happened, and what happened is an ``Activity`` - the table the scoring engine
reads. This module is the seam between the two, and it runs on the event bus
rather than inside ``complete_task`` so that tasks stay ignorant of activities:
the same reason notification routing lives outside ``core/access``.

**The category decides what it counts as, and this is the part to be careful
with.** Every activity type except ``NOTE`` counts as client contact, and a
``MEETING`` additionally drives Meeting Attendance. So a completed internal task
- "update the renewal spreadsheet" - recorded as contact would raise an
account's health score as though somebody had spoken to the client. Internal and
partner-facing work is therefore logged as a Note: present in the history, and
counting towards neither contact factor, which is exactly what ``NOTE`` is for.
"""

from __future__ import annotations

import logging

from csm.core import clock
from csm.core.enums import ActivitySource, ActivityType, TaskCategory, TaskType
from csm.core.events import TaskCompleted, subscribe

logger = logging.getLogger(__name__)

#: What a completed item of each kind is a record of, when it faced the client.
CONTACT_TYPE_FOR_KIND = {
    TaskType.MEETING.value: ActivityType.MEETING,
    TaskType.CALL.value: ActivityType.CALL,
}

#: Work that did not face the client. A Note counts for neither contact factor.
NOT_CLIENT_FACING = ActivityType.NOTE


def activity_type_for(task) -> str:
    """What kind of record a completed task becomes.

    Client-facing: the kind decides - a meeting is a meeting, a call is a call,
    and anything else is Other, which counts the same as an email would for
    Weekly Contact. Only Meeting is special to the scoring engine.

    Anything else: a Note. Internal work is real work and belongs in the
    history; it is not contact with the client and must not be counted as any.
    """
    if task.category != TaskCategory.CLIENT_FACING:
        return NOT_CLIENT_FACING
    return CONTACT_TYPE_FOR_KIND.get(str(task.task_type), ActivityType.OTHER)


def log_completed_task(task, *, actor=None, occurred_on=None):
    """Write the log entry for a completed task, once.

    Idempotent through the one-to-one column rather than a check-then-insert: a
    second completion, a replayed event or a retried job finds the row already
    there instead of racing to write a duplicate.

    ``occurred_on`` is given only when the task was entered after the fact with
    the date it happened on. It is never later than today: a log entry cannot
    record something that has not happened yet.
    """
    from csm.activities.models import Activity

    if getattr(task, "logged_activity", None) is not None:
        return None

    # An activity records who did the thing, and the column is not nullable.
    # ``complete_task`` always stamps ``completed_by``, so this is unreachable
    # through the application - but a replayed event or a shell session could
    # arrive without one, and a history entry attributed to nobody would be
    # worse than no entry.
    author = actor or task.completed_by or task.created_by
    if author is None:
        logger.warning("Task %s completed with no author; nothing logged", task.pk)
        return None

    return Activity.objects.create(
        account_id=task.account_id,
        task=task,
        contact=task.contact,
        activity_type=activity_type_for(task),
        # The day it was done, not the day it was due. The log is a record of
        # what happened: today, unless it was entered after the fact with the
        # date it actually took place.
        occurred_on=min(occurred_on or clock.today(), clock.today()),
        subject=task.title,
        notes=task.description or "",
        source=ActivitySource.MANUAL,
        created_by=author,
    )


def on_task_completed(event: TaskCompleted) -> None:
    from csm.tasks.models import Task

    task = Task.objects.filter(pk=event.task_id).select_related("contact").first()
    if task is None:
        return
    log_completed_task(task, occurred_on=event.occurred_on)


def register_subscribers() -> None:
    subscribe(TaskCompleted, on_task_completed)
