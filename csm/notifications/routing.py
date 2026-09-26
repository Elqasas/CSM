"""Who receives a task.

This module is the only place in the codebase that decides notification
recipients, and it deliberately imports nothing from ``csm.core.access``.

That isolation is the whole of the specification's "access is separate from
notifications" rule, expressed as a structural property rather than a
convention. A senior CSM can view and act on every account, but there is no code
path by which seniority could reach this function and widen who gets notified:
``resolve_recipients`` reads five scalar fields and none of them describes
anyone's role or seniority. The senior-CSM reassignment rule is expressed as
"the creator is not this account's CSM or ACM", which needs no seniority flag
and is therefore impossible to get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from csm.core.enums import (
    MANUAL_TASK_TYPES,
    LifecycleStage,
    TaskAssignmentRole,
    TaskType,
)


@dataclass(frozen=True)
class RoutingContext:
    """Everything routing is permitted to know."""

    task_type: str
    lifecycle_stage: str
    csm_id: int | None
    acm_id: int | None
    creator_id: int | None = None


@dataclass(frozen=True)
class Recipient:
    user_id: int
    assigned_as: str


def resolve_recipients(ctx: RoutingContext) -> tuple[Recipient, ...]:
    """Return the recipients for a task, in strict priority order.

    Priority 1 is the POC exception, which beats every rule below it: while an
    account is in POC, the CSM and the ACM share every task regardless of type,
    because POC is a joint pre-sales and success effort.
    """
    both = _pair(ctx)

    # Priority 1: the POC exception, for any task type.
    if ctx.lifecycle_stage == LifecycleStage.POC:
        return both

    # Priority 2: by task type.
    if ctx.task_type == TaskType.AUTO_HEALTH_ALERT:
        return _csm_only(ctx)

    if ctx.task_type in (TaskType.AUTO_RENEWAL, TaskType.AUTO_QUARTERLY_MEETING):
        return both

    if ctx.task_type in MANUAL_TASK_TYPES:
        return _manual(ctx)

    return both


def _pair(ctx: RoutingContext) -> tuple[Recipient, ...]:
    out = []
    if ctx.csm_id:
        out.append(Recipient(ctx.csm_id, TaskAssignmentRole.CSM.value))
    if ctx.acm_id and ctx.acm_id != ctx.csm_id:
        out.append(Recipient(ctx.acm_id, TaskAssignmentRole.ACM.value))
    return tuple(out)


def _csm_only(ctx: RoutingContext) -> tuple[Recipient, ...]:
    if ctx.csm_id:
        return (Recipient(ctx.csm_id, TaskAssignmentRole.CSM.value),)
    return ()


def _manual(ctx: RoutingContext) -> tuple[Recipient, ...]:
    """A hand-made task - next action, meeting or call - goes to its creator.

    One exception.

    A senior CSM acting on an account that is not theirs is helping out, not
    taking the work over, so the task routes to the account's own CSM rather
    than to the person who typed it.
    """
    if ctx.creator_id and ctx.creator_id in (ctx.csm_id, ctx.acm_id):
        assigned_as = (
            TaskAssignmentRole.CSM.value
            if ctx.creator_id == ctx.csm_id
            else TaskAssignmentRole.ACM.value
        )
        return (Recipient(ctx.creator_id, assigned_as),)

    if ctx.csm_id:
        return (Recipient(ctx.csm_id, TaskAssignmentRole.CSM.value),)

    # No CSM on the account. Rather than drop the task, hand it back to whoever
    # created it so it is at least visible to someone.
    if ctx.creator_id:
        return (Recipient(ctx.creator_id, TaskAssignmentRole.CREATOR.value),)
    return ()
