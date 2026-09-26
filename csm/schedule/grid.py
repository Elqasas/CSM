"""The shape of a month, as numbers.

Pure geometry, the same as ``dashboard/charts.py``: no database, no request, no
clock. A month is six rows of seven days starting on the configured first
weekday, which is the only way every month fits one grid without the page
jumping between five rows and six.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

#: Weeks in the grid. Six always, so February and a 31-day month starting on a
#: Sunday occupy the same space and the page does not resize as you page
#: through the year.
WEEKS = 6

#: Sunday, in Python's numbering. Egypt's working week runs Sunday to
#: Thursday, so a grid starting on Monday splits the working week across two
#: rows - which is the one thing a week view must not do. Each reader can
#: change it; this is what they get until they do.
FIRST_WEEKDAY = 6


@dataclass(frozen=True)
class Day:
    date: date
    in_month: bool


def month_start(day: date) -> date:
    return day.replace(day=1)


def grid_start(day: date, *, first_weekday: int = FIRST_WEEKDAY) -> date:
    """The first cell of the grid holding ``day``'s month."""
    first = month_start(day)
    back = (first.weekday() - first_weekday) % 7
    return first - timedelta(days=back)


def month_grid(day: date, *, first_weekday: int = FIRST_WEEKDAY) -> list[list[Day]]:
    """Six weeks of seven days, with the days outside the month marked."""
    start = grid_start(day, first_weekday=first_weekday)
    month = month_start(day).month
    return [
        [
            Day(date=start + timedelta(days=week * 7 + weekday),
                in_month=(start + timedelta(days=week * 7 + weekday)).month == month)
            for weekday in range(7)
        ]
        for week in range(WEEKS)
    ]


def week_of(day: date, *, first_weekday: int = FIRST_WEEKDAY) -> list[date]:
    start = day - timedelta(days=(day.weekday() - first_weekday) % 7)
    return [start + timedelta(days=offset) for offset in range(7)]


def weekday_headings(first_weekday: int = FIRST_WEEKDAY) -> list[str]:
    """Column headings in the reader's own order.

    Hard-coded "Mon Tue Wed..." in the template was wrong the moment the week
    could start anywhere, and a heading that disagrees with the column under it
    is worse than no heading.
    """
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return [names[(first_weekday + offset) % 7] for offset in range(7)]


def add_months(day: date, months: int) -> date:
    """Step a month for the previous/next links, always landing on the 1st."""
    from calendar import monthrange

    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))
