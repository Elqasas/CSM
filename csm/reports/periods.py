"""The date range a report covers.

Pure: ``resolve`` takes today's business date as an argument rather than reading
the clock, so every preset is asserted in tests on both sides of a month, quarter
and year boundary. The view passes ``clock.today()`` - a range is a span of
Cairo calendar days, the same days every health factor counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

#: The longest range a report will build. Two years of daily health history
#: across the whole portfolio is the largest sheet anybody has a use for; past
#: that a download is a database dump, not a report.
MAX_DAYS = 366 * 2

CUSTOM = "custom"


class PeriodError(ValueError):
    """A range the reader asked for that cannot be built."""


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def slug(self) -> str:
        return f"{self.start.isoformat()}_to_{self.end.isoformat()}"


def _quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def _month_end(day: date) -> date:
    following = date(day.year + day.month // 12, day.month % 12 + 1, 1)
    return following - timedelta(days=1)


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _this_month(today):
    return today.replace(day=1), _month_end(today)


def _last_month(today):
    start = _add_months(today.replace(day=1), -1)
    return start, _month_end(start)


def _this_quarter(today):
    start = _quarter_start(today)
    return start, _month_end(_add_months(start, 2))


def _last_quarter(today):
    start = _add_months(_quarter_start(today), -3)
    return start, _month_end(_add_months(start, 2))


def _next_quarter(today):
    start = _add_months(_quarter_start(today), 3)
    return start, _month_end(_add_months(start, 2))


def _this_year(today):
    return date(today.year, 1, 1), date(today.year, 12, 31)


def _last_year(today):
    return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)


def _last_30(today):
    return today - timedelta(days=29), today


def _next_30(today):
    return today, today + timedelta(days=29)


def _next_90(today):
    return today, today + timedelta(days=89)


#: Every preset, in the order the menus offer them. Looking back first, then
#: forward: most reports describe what happened, and the forward ones (upcoming
#: tasks, renewals) say so by defaulting to a forward preset.
PRESETS: dict[str, tuple[str, object]] = {
    "this_month": ("This month", _this_month),
    "last_month": ("Last month", _last_month),
    "last_30": ("Last 30 days", _last_30),
    "this_quarter": ("This quarter", _this_quarter),
    "last_quarter": ("Last quarter", _last_quarter),
    "this_year": ("This year", _this_year),
    "last_year": ("Last year", _last_year),
    "next_30": ("Next 30 days", _next_30),
    "next_90": ("Next 90 days", _next_90),
    "next_quarter": ("Next quarter", _next_quarter),
}

#: What a menu shows: every preset, then the custom range.
CHOICES: list[tuple[str, str]] = [(key, label) for key, (label, _) in PRESETS.items()] + [
    (CUSTOM, "Custom range")
]


def preset(key: str, today: date) -> Period:
    label, compute = PRESETS[key]
    start, end = compute(today)
    return Period(start=start, end=end, label=label)


def _parse(text: str, name: str) -> date:
    try:
        return date.fromisoformat((text or "").strip())
    except ValueError:
        raise PeriodError(f"Choose a {name} date.") from None


def resolve(key: str, start_text: str, end_text: str, *, today: date) -> Period:
    """The range a submitted report form asks for.

    A preset ignores the two date boxes entirely, so a browser that still sends
    them after the reader switched back from a custom range cannot narrow a
    preset by accident. An unknown preset is an error rather than a silent "this
    month": the reader would get a sheet for a range they did not ask for.
    """
    if key in PRESETS:
        return preset(key, today)
    if key != CUSTOM:
        raise PeriodError("Choose a period.")

    start = _parse(start_text, "start")
    end = _parse(end_text, "end")
    if end < start:
        raise PeriodError("The end date is before the start date.")
    if (end - start).days + 1 > MAX_DAYS:
        raise PeriodError("A report covers at most two years. Choose a shorter range.")
    label = f"{start:%d %b %Y} to {end:%d %b %Y}"
    return Period(start=start, end=end, label=label)
