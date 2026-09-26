"""What the activities pages can be narrowed by.

One module for both, because the two pages answer the same question at
different scopes and a filter that works on one and not the other is the sort
of difference nobody notices until they rely on it.

Every filter is applied to the *queries*, not to the merged list. Filtering
after the merge would narrow the slice that was already taken, so asking for
last month would return whatever part of the most recent sixty happened to fall
in it - which looks like an answer and is not one.
"""

from __future__ import annotations

import datetime as dt

from django.db.models import Q

#: How far back, in days. None is everything.
PERIODS = (
    ("30", "Last 30 days", 30),
    ("90", "Last 90 days", 90),
    ("365", "Last year", 365),
    ("all", "All time", None),
)
#: All time. A ninety-day window hid planned work further out - a renewal
#: three months away is exactly the thing you open this page to find - and a
#: default that silently omits the future is worse than a longer list.
DEFAULT_PERIOD = "all"

#: What kind of line to show. The first four are the labels the entries
#: themselves carry, so the filter and the badges speak one vocabulary. Overdue
#: is the fifth because it is the question people actually arrive with, and it
#: is planned work that has run out of time rather than a state of its own.
NATURES = (
    ("all", "Everything"),
    ("happened", "Happened"),
    ("planned", "Planned"),
    ("overdue", "Overdue"),
)


def cutoff(period: str, *, today: dt.date) -> dt.date | None:
    """The oldest date a period includes, or None for all time."""
    for value, _, days in PERIODS:
        if value == period and days is not None:
            return today - dt.timedelta(days=days)
    return None


def wants_activities(nature: str) -> bool:
    return nature in ("all", "happened")


def wants_tasks(nature: str) -> bool:
    return nature in ("all", "happened", "planned", "overdue")


def narrow_activities(qs, *, since, account_id, term, mine_for):
    if since is not None:
        qs = qs.filter(occurred_on__gte=since)
    if account_id:
        qs = qs.filter(account_id=account_id)
    if term:
        qs = qs.filter(Q(subject__icontains=term) | Q(notes__icontains=term))
    if mine_for is not None:
        # Who logged it. An activity has no assignee, so "mine" is authorship -
        # the nearest true thing, rather than pretending the two are the same.
        qs = qs.filter(created_by=mine_for)
    return qs


def narrow_tasks(
    qs, *, nature, since, account_id, term, mine_for, kind="",
    as_of=None, as_of_time=None,
):
    from csm.core.enums import OPEN_TASK_STATUSES

    # A task that produced a log entry is represented by that entry. Showing
    # both is one meeting described twice, which is what made "Done" and
    # "Happened" look like two different things.
    qs = qs.filter(logged_activity__isnull=True)

    if nature == "planned":
        qs = qs.filter(status__in=list(OPEN_TASK_STATUSES))
    elif nature == "happened":
        qs = qs.exclude(status__in=list(OPEN_TASK_STATUSES))
    elif nature == "overdue":
        # Through the queryset, so this counts what every other overdue number
        # in the application counts - including a task whose time passed today.
        qs = qs.overdue(as_of, as_of_time)
    if kind:
        qs = qs.filter(task_type=kind)
    if since is not None:
        qs = qs.filter(due_date__gte=since)
    if account_id:
        qs = qs.filter(account_id=account_id)
    if term:
        qs = qs.filter(Q(title__icontains=term) | Q(description__icontains=term))
    if mine_for is not None:
        qs = qs.filter(assignments__user=mine_for).distinct()
    return qs
