"""Keeping a repeating task going.

One rule, one open occurrence. The next is stamped out when the current one
closes, and the daily generator tops a series up if its occurrence was
cancelled - so a series cannot silently die, and an occurrence you have not done
sits there overdue rather than breeding copies of itself.

Idempotent the same way every generator here is: the occurrence carries an
``auto_key`` built from the series and the date, and the unique index turns a
second attempt into a no-op rather than a duplicate.
"""

from __future__ import annotations

import logging
from datetime import date

from csm.core.enums import OPEN_TASK_STATUSES
from csm.core.events import TaskCompleted, subscribe

logger = logging.getLogger(__name__)


def key_for(series_id: int, due_date: date) -> str:
    return f"series:{series_id}:{due_date.isoformat()}"


def extend(series, *, after: date):
    """Create the occurrence that follows ``after``, if the series has one left.

    Returns the new task, or None when the series has run out or already has an
    open occurrence - two states worth distinguishing in a log but not to the
    caller, which only wants to know whether there is more to do.
    """
    from csm.tasks.services import create_task

    if series.occurrences_made.filter(status__in=list(OPEN_TASK_STATUSES)).exists():
        return None

    due = series.next_due(after)
    if due is None:
        # The series has finished. Marked inactive so the daily pass stops
        # looking at it, rather than recomputing the same None every night.
        if series.is_active:
            series.is_active = False
            series.save(update_fields=["is_active"])
        return None

    task = create_task(
        account=series.account,
        task_type=series.task_type,
        title=series.title,
        description=series.description,
        due_date=due,
        due_time=series.due_time,
        category=series.category,
        priority=series.priority,
        contact=series.contact,
        created_by=series.created_by,
        series=series,
        auto_key=key_for(series.pk, due),
    )
    if task is not None:
        series.created_count += 1
        series.save(update_fields=["created_count"])
    return task


def start(series, *, first_due: date):
    """The first occurrence, on the date the person chose."""
    from csm.tasks.services import create_task

    task = create_task(
        account=series.account,
        task_type=series.task_type,
        title=series.title,
        description=series.description,
        due_date=first_due,
        due_time=series.due_time,
        category=series.category,
        priority=series.priority,
        contact=series.contact,
        created_by=series.created_by,
        series=series,
        auto_key=key_for(series.pk, first_due),
    )
    if task is not None:
        series.created_count += 1
        series.save(update_fields=["created_count"])
    return task


def top_up(as_of: date) -> int:
    """The daily pass: every active series gets an open occurrence back.

    Only reaches a series whose occurrence was cancelled or auto-closed without
    the completion event firing. A series with an open occurrence is left alone,
    however overdue it is - that one is somebody's to do, not the generator's to
    replace.
    """
    from csm.tasks.models import TaskSeries

    made = 0
    for series in TaskSeries.objects.filter(is_active=True).select_related(
        "account", "contact", "created_by"
    ):
        last = series.occurrences_made.order_by("-due_date").first()
        if extend(series, after=last.due_date if last else as_of) is not None:
            made += 1
    return made


def on_task_completed(event: TaskCompleted) -> None:
    """Finishing one occurrence creates the next."""
    from csm.tasks.models import Task

    task = (
        Task.objects.filter(pk=event.task_id)
        .select_related("series", "series__account", "series__contact")
        .first()
    )
    if task is None or task.series_id is None:
        return
    extend(task.series, after=task.due_date)


def register_subscribers() -> None:
    subscribe(TaskCompleted, on_task_completed)
