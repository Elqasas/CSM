"""Everything that touches one client, on one line.

A pure function over two lists: no database, no request, no clock. The page it
feeds is the answer to "what has been going on with this account", which today
takes three places to assemble - the activity log, the open tasks, and whatever
meetings somebody remembers arranging.

Interface only. Tasks and activities stay separate tables for the reason set out
in README.md: ``health/inputs.py`` reads ``Activity`` for three of the five
scoring factors, so a planned meeting living in that table would raise a health
score because somebody opened a calendar. This module merges them for reading
and nothing else - which is why it is a function over objects rather than a
query, and why it cannot accidentally become a place that writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

#: What an entry is. Two states, not three: "Planned" is an obligation nobody
#: has discharged, and "Happened" is everything that took place - a logged
#: activity, or a task somebody finished.
#:
#: Done was a third for a while and it double-counted. Completing a task writes
#: an Activity, so the page showed the task as Done *and* the activity it
#: produced as Happened: two lines, one meeting. The task is dropped in favour
#: of the record it wrote, and the two words collapse into one.
HAPPENED = "happened"
PLANNED = "planned"


@dataclass(frozen=True)
class Entry:
    """One line of the timeline."""

    when: date
    nature: str
    kind: str
    title: str
    detail: str
    who: str
    #: The underlying object, for the template to link to and gate on. Kept
    #: whole rather than flattened, so a template can ask it anything without
    #: this module having to predict the question.
    source: object
    voided: bool = False
    #: A field rather than a property derived from ``nature``: a finished task
    #: and a logged activity are both "happened", and only one of them has a
    #: Done button.
    is_task: bool = False


def build(activities, tasks) -> list[Entry]:
    """Interleave what happened with what was planned, newest first.

    Both streams are keyed on the date they are *about* - an activity on the day
    it occurred, a task on the day it is due - rather than on when the row was
    written. A task completed this morning but due last week belongs where the
    work belonged, or the timeline stops being a history of the account and
    becomes a history of somebody's data entry.

    Ties break by nature and then by title, so two entries on one day come back
    in the same order every time. Without that the page quietly reshuffles
    itself between visits and nobody can tell whether something changed.

    Tasks that produced a log entry are expected to have been excluded by the
    caller: the Activity is the record of what happened, and showing both is one
    meeting described twice.
    """
    entries = [_from_activity(activity) for activity in activities]
    entries += [_from_task(task) for task in tasks]
    return sorted(entries, key=lambda e: (e.when, e.nature, e.title), reverse=True)


def _from_activity(activity) -> Entry:
    return Entry(
        when=activity.occurred_on,
        nature=HAPPENED,
        kind=activity.get_activity_type_display(),
        title=activity.subject or activity.get_activity_type_display(),
        detail=activity.notes or "",
        who=_name(activity.contact),
        source=activity,
        voided=activity.is_voided,
    )


def _from_task(task) -> Entry:
    return Entry(
        when=task.due_date,
        nature=PLANNED if task.is_open else HAPPENED,
        kind=task.get_task_type_display(),
        title=task.title,
        detail=task.description or "",
        who=_name(task.contact),
        source=task,
        is_task=True,
    )


def _name(contact) -> str:
    return contact.full_name if contact else ""
