"""Where a repeating task *will* fall, without writing it down.

A series keeps one open occurrence at a time - see ``TaskSeries`` for why - so a
weekly meeting is one row today and nothing at all next Tuesday. That is the
right behaviour for a queue and useless for a calendar: the whole point of
looking at a month is to see the load, and a month showing one of four weekly
meetings shows the wrong week.

So the calendar projects. Pure arithmetic over the rule, no database and no
writes: the projected days are drawn as outlines, the real occurrence is the
solid one, and nothing about how a repeat behaves changes because somebody
looked at a month.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from csm.core.enums import RepeatEnd


@dataclass(frozen=True)
class Projected:
    """A day a series will land on, and the rule that put it there."""

    when: date
    series: object

    @property
    def title(self) -> str:
        return self.series.title

    @property
    def kind(self) -> str:
        return self.series.get_task_type_display()

    @property
    def priority(self) -> str:
        return self.series.priority


def project(series, *, start: date, end: date, taken=(), from_date=None) -> list[Projected]:
    """Every day this series falls on between ``start`` and ``end``.

    ``taken`` are days that already have a real occurrence: those are drawn from
    the task itself, so projecting them too would show the same meeting twice.

    ``from_date`` is where to start stepping - normally the last occurrence
    already created. Walking from the rule's own beginning would re-project the
    past, and a calendar that invents history is worse than one that stops
    short.

    Bounded by ``end`` rather than by a count, so a daily series looked at over
    a month is thirty steps and not an infinite loop.
    """
    from csm.tasks.models import _step

    if not series.is_active or from_date is None:
        return []

    remaining = None
    if series.ends == RepeatEnd.AFTER:
        remaining = max((series.occurrences or 0) - series.created_count, 0)

    out: list[Projected] = []
    cursor = from_date
    while True:
        if remaining is not None and remaining <= 0:
            break
        cursor = _step(cursor, series.frequency)
        if cursor > end:
            break
        if series.ends == RepeatEnd.ON and series.until and cursor > series.until:
            break
        if remaining is not None:
            remaining -= 1
        if cursor >= start and cursor not in taken:
            out.append(Projected(when=cursor, series=series))
    return out


def project_all(series_list, *, start: date, end: date, taken_by_series=None) -> list[Projected]:
    """Every active series, projected across one window."""
    taken_by_series = taken_by_series or {}
    out: list[Projected] = []
    for series in series_list:
        dates = taken_by_series.get(series.pk, set())
        out += project(
            series,
            start=start,
            end=end,
            taken=dates,
            from_date=max(dates) if dates else None,
        )
    return out
