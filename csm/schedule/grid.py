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


# ---------------------------------------------------------------------------
# The hours of a day
# ---------------------------------------------------------------------------

MINUTES_PER_DAY = 24 * 60

#: How long a task with a clock time is drawn. A task has a due time and no
#: duration; half an hour is long enough to read and short enough not to claim
#: the afternoon.
TASK_MINUTES = 30

#: The shortest block drawn. A five-minute meeting is still a line somebody has
#: to be able to read, and two of them back to back must not overprint.
MIN_MINUTES = 20


@dataclass(frozen=True)
class Span:
    """Something with a start and an end on one day, in minutes from midnight."""

    start: int
    end: int
    kind: str
    item: object


@dataclass(frozen=True)
class Placed:
    """A span with its place in the column: which lane, out of how many.

    Positions are percentages of the day, so the template draws a block with
    nothing but ``top``/``height``/``left``/``width`` and the hour height can
    change in CSS without touching any of this.
    """

    span: Span
    lane: int
    lanes: int

    @property
    def kind(self) -> str:
        return self.span.kind

    @property
    def item(self):
        return self.span.item

    @property
    def top(self) -> float:
        return round(self.span.start / MINUTES_PER_DAY * 100, 4)

    @property
    def height(self) -> float:
        return round((_drawn_end(self.span) - self.span.start) / MINUTES_PER_DAY * 100, 4)

    @property
    def left(self) -> float:
        return round(self.lane / self.lanes * 100, 4)

    @property
    def width(self) -> float:
        return round(100 / self.lanes, 4)

    @property
    def is_short(self) -> bool:
        """Under three-quarters of an hour: one line, no second row of detail."""
        return self.span.end - self.span.start < 45


def _drawn_end(span: Span) -> int:
    return min(max(span.end, span.start + MIN_MINUTES), MINUTES_PER_DAY)


def minutes(moment) -> int:
    """A ``time`` (or datetime) as minutes past midnight."""
    return moment.hour * 60 + moment.minute


def lay_out(spans) -> list[Placed]:
    """Side by side where they overlap, full width where they do not.

    Spans are grouped into clusters that overlap one another, transitively; each
    span takes the first lane free at its start, and every span in a cluster is
    as wide as the cluster has lanes. Two spans sharing nothing in time never
    narrow each other, which is the usual calendar's behaviour and the one a
    reader expects. Overlap is judged on the *drawn* end, so a five-minute
    meeting followed at once by another does not overprint.
    """
    ordered = sorted(spans, key=lambda s: (s.start, -_drawn_end(s)))
    out: list[Placed] = []
    cluster: list[tuple[Span, int]] = []
    lane_ends: list[int] = []
    cluster_end = -1

    def flush():
        out.extend(Placed(span=span, lane=lane, lanes=len(lane_ends)) for span, lane in cluster)

    for span in ordered:
        if cluster and span.start >= cluster_end:
            flush()
            cluster, lane_ends = [], []
        end = _drawn_end(span)
        for lane, lane_end in enumerate(lane_ends):
            if lane_end <= span.start:
                lane_ends[lane] = end
                break
        else:
            lane = len(lane_ends)
            lane_ends.append(end)
        cluster.append((span, lane))
        cluster_end = max(cluster_end, end)
    if cluster:
        flush()
    return out
