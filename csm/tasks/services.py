"""Task creation, assignment and closure.

Every task in the system is created here, so assignment and notification happen
exactly once and cannot be forgotten by a caller.
"""

from __future__ import annotations

import logging
from datetime import date

from django.db import IntegrityError, transaction

from csm.accounts.models import Account
from csm.core import clock
from csm.core.enums import (
    AUTO_TASK_TYPES,
    DEFAULT_TASK_PRIORITY,
    TaskCategory,
    TaskCloseReason,
    TaskProgress,
    TaskStatus,
    TaskType,
)
from csm.core.events import TaskCompleted, TaskCreated, publish
from csm.notifications.routing import RoutingContext, resolve_recipients
from csm.tasks.models import Task, TaskAssignment

logger = logging.getLogger(__name__)


def routing_context(account: Account, *, task_type: str, creator_id: int | None) -> RoutingContext:
    return RoutingContext(
        task_type=task_type,
        lifecycle_stage=account.lifecycle_stage,
        csm_id=account.csm_id,
        acm_id=account.acm_id,
        creator_id=creator_id,
    )


@transaction.atomic
def create_task(
    *,
    account: Account,
    task_type: str,
    title: str,
    due_date: date,
    due_time=None,
    category: str = TaskCategory.INTERNAL,
    priority: str = DEFAULT_TASK_PRIORITY,
    progress: str = TaskProgress.NOT_STARTED,
    description: str = "",
    contact=None,
    created_by=None,
    series=None,
    auto_key: str | None = None,
) -> Task | None:
    """Create a task and route it.

    Returns None when ``auto_key`` already exists, which is the expected outcome
    of re-running a generator rather than an error.
    """
    try:
        with transaction.atomic():
            task = Task.objects.create(
                account=account,
                task_type=task_type,
                title=title,
                due_date=due_date,
                due_time=due_time,
                category=category,
                priority=priority,
                progress=progress,
                description=description,
                contact=contact,
                created_by=created_by,
                series=series,
                auto_key=auto_key,
            )
    except IntegrityError:
        # Either the auto_key exists or an open health alert is already there.
        # Both mean the obligation is already recorded.
        return None

    assign(task)
    publish(
        TaskCreated(
            task_id=task.pk,
            account_id=account.pk,
            task_type=str(task_type),
            category=str(category),
            due_date=due_date,
        )
    )
    return task


def assign(task: Task) -> tuple[TaskAssignment, ...]:
    """Route a task to its recipients and record the assignment."""
    ctx = routing_context(
        task.account, task_type=task.task_type, creator_id=task.created_by_id
    )
    recipients = resolve_recipients(ctx)

    if not recipients:
        # Never drop a task. An account imported with a blank CSM column must
        # not silently swallow a renewal reminder; it surfaces in the unassigned
        # bucket instead.
        logger.warning(
            "Task %s on account %s has no recipient and is unassigned",
            task.pk,
            task.account_id,
        )
        return ()

    created = []
    for recipient in recipients:
        assignment, _ = TaskAssignment.objects.get_or_create(
            task=task,
            user_id=recipient.user_id,
            defaults={"assigned_as": recipient.assigned_as},
        )
        created.append(assignment)
    return tuple(created)


@transaction.atomic
def complete_task(task: Task, *, actor, occurred_on=None) -> Task:
    if not task.is_open:
        return task
    task.status = TaskStatus.DONE
    # The two move together: a task that is done but still reads "In progress"
    # is the state that makes people stop trusting the column.
    task.progress = TaskProgress.COMPLETED
    task.close_reason = TaskCloseReason.COMPLETED
    task.completed_at = clock.now()
    task.completed_by = actor
    task.save(
        update_fields=[
            "status", "progress", "close_reason", "completed_at", "completed_by",
            "updated_at",
        ]
    )

    if task.task_type == TaskType.AUTO_HEALTH_ALERT:
        # Closing an alert by hand is final until the account leaves At Risk and
        # comes back, so the same red episode does not regenerate it.
        Account.objects.filter(pk=task.account_id).update(
            health_alert_suppressed_at=clock.now()
        )

    publish(
        TaskCompleted(
            task_id=task.pk,
            account_id=task.account_id,
            task_type=str(task.task_type),
            completed_by_id=getattr(actor, "pk", None),
            occurred_on=occurred_on,
        )
    )
    return task


@transaction.atomic
def cancel_task(task: Task, *, reason: str = TaskCloseReason.CANCELLED_BY_USER) -> Task:
    """Close a task without completing it.

    Generated tasks are cancelled rather than deleted. A hard delete would free
    the idempotency key and the task would reappear on the next run, which looks
    exactly like a bug.
    """
    if not task.is_open:
        return task
    task.status = TaskStatus.CANCELLED
    task.close_reason = reason
    task.save(update_fields=["status", "close_reason", "updated_at"])
    return task


def delete_task(task: Task) -> None:
    """Remove a task at a user's request.

    A manual task is genuinely deleted. A generated one is cancelled instead,
    for the reason given above.
    """
    if task.task_type in AUTO_TASK_TYPES or task.auto_key:
        cancel_task(task)
    else:
        task.delete()
