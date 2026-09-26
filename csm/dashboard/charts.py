"""SVG geometry for the dashboard charts.

Pure functions over plain numbers: no database, no request, no clock. Charts are
rendered server-side as inline SVG because the platform has no JavaScript build
step, and because a chart that is markup can be asserted on in a test rather than
screenshotted.

Colour is not decided here. These functions produce geometry and take a CSS
custom-property name for the fill, so the palette lives in one stylesheet and a
chart cannot invent a colour of its own.

On the palette: the categorical slots used for two-series charts were validated
against this application's own surfaces (white in light mode, Bootstrap's
``#212529`` in dark) and pass every gate. The health-status donut is the
exception and it is deliberate. The specification asks for traffic lights, and
traffic-light red against green is the one pair a deuteranopic reader cannot
separate: measured, they sit 4.1 apart where 8 is the floor. Status colour there
is therefore never alone - every segment carries a direct label and a count, and
the card ships a table of the same numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Radius of the rounded data-end on a bar, in user units.
BAR_END_RADIUS = 4.0


# ---------------------------------------------------------------------------
# Donut
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DonutSegment:
    label: str
    value: int
    percent: float
    path: str
    token: str
    #: An opaque identifier the caller can hang a link off. Geometry knows
    #: nothing about what it means; it is carried, never interpreted.
    key: str = ""

    @property
    def percent_display(self) -> str:
        return f"{self.percent:.0f}%"


@dataclass(frozen=True)
class Donut:
    segments: tuple
    total: int
    size: float
    centre: float
    inner_radius: float
    #: What the total counts. Most donuts here count accounts; the seat split
    #: counts users, and a centre reading "accounts" over a seat count would be
    #: a caption that contradicts its own chart.
    unit: str = "accounts"

    @property
    def is_empty(self) -> bool:
        return self.total == 0

    @property
    def total_display(self) -> str:
        return f"{self.total:,}"


def _polar(centre: float, radius: float, degrees: float) -> tuple:
    radians = math.radians(degrees - 90.0)  # 0 degrees at twelve o'clock
    return centre + radius * math.cos(radians), centre + radius * math.sin(radians)


def _annulus_path(
    centre: float, outer: float, inner: float, start: float, end: float
) -> str:
    """One donut segment, as an SVG path.

    A segment covering the whole circle cannot be drawn as a single arc - start
    and end land on the same point and the renderer draws nothing - so it is
    split in half. That case is not hypothetical: a portfolio where every
    account is Healthy is the one every CSM is aiming for.
    """
    if end - start >= 360.0:
        half = start + 180.0
        first = _annulus_path(centre, outer, inner, start, half)
        second = _annulus_path(centre, outer, inner, half, start + 359.999)
        return f"{first} {second}"

    large_arc = 1 if (end - start) > 180.0 else 0
    ox0, oy0 = _polar(centre, outer, start)
    ox1, oy1 = _polar(centre, outer, end)
    ix1, iy1 = _polar(centre, inner, end)
    ix0, iy0 = _polar(centre, inner, start)
    return (
        f"M {ox0:.2f} {oy0:.2f} "
        f"A {outer:.2f} {outer:.2f} 0 {large_arc} 1 {ox1:.2f} {oy1:.2f} "
        f"L {ix1:.2f} {iy1:.2f} "
        f"A {inner:.2f} {inner:.2f} 0 {large_arc} 0 {ix0:.2f} {iy0:.2f} Z"
    )


def donut(
    slices, *, size: float = 160.0, thickness: float = 26.0, unit: str = "accounts"
) -> Donut:
    """Build a donut from ``(label, value, token)`` triples.

    A fourth element, if given, is an opaque key carried through to the segment
    - what the caller uses to turn a slice into a link. This module stays pure
    geometry: it neither builds the URL nor knows there is one.

    Zero-valued slices are dropped rather than drawn as invisible slivers that
    still claim a legend row and a tooltip target.
    """
    kept = [
        (row[0], int(row[1]), row[2], row[3] if len(row) > 3 else "")
        for row in slices
        if row[1]
    ]
    total = sum(value for _, value, _, _ in kept)

    centre = size / 2.0
    outer = centre
    inner = centre - thickness

    segments = []
    angle = 0.0
    for label, value, token, key in kept:
        sweep = 360.0 * value / total if total else 0.0
        segments.append(
            DonutSegment(
                label=label,
                value=value,
                percent=100.0 * value / total if total else 0.0,
                path=_annulus_path(centre, outer, inner, angle, angle + sweep),
                token=token,
                key=key,
            )
        )
        angle += sweep

    return Donut(
        segments=tuple(segments),
        total=total,
        size=size,
        centre=centre,
        inner_radius=inner,
        unit=unit,
    )


# ---------------------------------------------------------------------------
# Horizontal bars
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    label: str
    value: int
    path: str
    label_y: float
    value_x: float
    value_y: float
    key: str = ""


@dataclass(frozen=True)
class BarChart:
    bars: tuple
    width: float
    height: float
    label_width: float
    max_value: int

    @property
    def is_empty(self) -> bool:
        return not self.bars


def _bar_path(x0: float, y: float, length: float, height: float) -> str:
    """A bar with its data-end rounded and its baseline end square.

    Rounding only the far end is what keeps the baseline reading as a baseline;
    a pill-shaped bar detaches from the axis it is measured against.
    """
    radius = min(BAR_END_RADIUS, height / 2.0, max(length, 0.0))
    if length <= 0.5:
        return ""
    if length <= radius:
        return f"M {x0:.2f} {y:.2f} h {length:.2f} v {height:.2f} h {-length:.2f} Z"
    end = x0 + length
    return (
        f"M {x0:.2f} {y:.2f} "
        f"H {end - radius:.2f} "
        f"A {radius:.2f} {radius:.2f} 0 0 1 {end:.2f} {y + radius:.2f} "
        f"V {y + height - radius:.2f} "
        f"A {radius:.2f} {radius:.2f} 0 0 1 {end - radius:.2f} {y + height:.2f} "
        f"H {x0:.2f} Z"
    )


#: Rough advance width of the label font (0.72rem) per character. Used only to
#: size the label gutter; being a little generous costs plot width, being mean
#: clips the label, so it errs high.
LABEL_CHAR_WIDTH = 6.1


def bars(
    rows,
    *,
    width: float = 420.0,
    bar_height: float = 18.0,
    gap: float = 10.0,
    label_width: float | None = None,
) -> BarChart:
    """Horizontal bars from ``(label, value)`` pairs, in the order given.

    A third element, if given, is an opaque key carried through to the bar, the
    same as for donut slices: the caller turns it into a link, this module does
    not know it is one.

    Order is the caller's. These charts show ordered categories - Class runs
    VIP to D, Lifecycle Stage runs POC to Churned - and re-sorting them by count
    would destroy the sequence the reader is there to follow.

    The label gutter sizes itself to the longest label unless one is given. A
    fixed gutter silently clips: "United Arab Emirates" is twenty characters and
    ran off the left edge of the country chart at the fixed width this used to
    have, with nothing to show that it had happened.
    """
    rows = [
        (row[0], int(row[1]), row[2] if len(row) > 2 else "")
        for row in rows
    ]

    if label_width is None:
        longest = max((len(str(label)) for label, _, _ in rows), default=0)
        label_width = min(max(58.0, longest * LABEL_CHAR_WIDTH + 10.0), width * 0.45)
    max_value = max((value for _, value, _ in rows), default=0)
    plot_width = width - label_width - 46.0  # room for the value at the end

    built = []
    y = 0.0
    for label, value, key in rows:
        length = (plot_width * value / max_value) if max_value else 0.0
        built.append(
            Bar(
                label=label,
                value=value,
                path=_bar_path(label_width, y, length, bar_height),
                label_y=y + bar_height / 2.0,
                value_x=label_width + max(length, 0.0) + 6.0,
                value_y=y + bar_height / 2.0,
                key=key,
            )
        )
        y += bar_height + gap

    return BarChart(
        bars=tuple(built),
        width=width,
        height=max(y - gap, 0.0),
        label_width=label_width,
        max_value=max_value,
    )


# ---------------------------------------------------------------------------
# Line chart
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinePoint:
    x: float
    y: float
    value: int
    label: str


@dataclass(frozen=True)
class LineSeries:
    name: str
    token: str
    path: str
    points: tuple


@dataclass(frozen=True)
class GridLine:
    y: float
    value: int


@dataclass(frozen=True)
class LineChart:
    series: tuple
    x_labels: tuple
    grid: tuple
    width: float
    height: float
    plot_top: float
    plot_bottom: float
    plot_left: float
    max_value: int
    raw_max: int

    @property
    def is_empty(self) -> bool:
        """Emptiness is decided on the real data, not the rounded axis.

        ``_nice_ceiling`` floors at 1 so an axis always has a top, which means
        ``max_value`` is never 0 and cannot answer this question. A chart of
        twelve zeroes would otherwise draw a flat line along the bottom and
        claim to be showing something.
        """
        return not self.series or self.raw_max == 0


def _nice_ceiling(value: int) -> int:
    """Round a maximum up to something an axis label can say out loud."""
    if value <= 4:
        return max(value, 1)
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if value <= step:
            return step
    return int(math.ceil(value / 1000.0) * 1000)


def lines(
    x_labels,
    series,
    *,
    width: float = 640.0,
    height: float = 220.0,
    plot_left: float = 34.0,
    plot_top: float = 12.0,
    plot_bottom: float = 26.0,
) -> LineChart:
    """A multi-series line chart from ``(name, token, values)`` triples.

    Every series shares one y-scale. That is not a stylistic preference: two
    scales on one plot let the author choose which line looks higher, and the
    reader cannot tell it happened.
    """
    x_labels = list(x_labels)
    series = [(name, token, list(values)) for name, token, values in series]

    raw_max = max((max(values, default=0) for _, _, values in series), default=0)
    max_value = _nice_ceiling(raw_max)

    plot_w = width - plot_left - 8.0
    plot_h = height - plot_top - plot_bottom
    steps = max(len(x_labels) - 1, 1)

    def x_at(index: int) -> float:
        return plot_left + (plot_w * index / steps)

    def y_at(value: int) -> float:
        return plot_top + plot_h * (1.0 - (value / max_value if max_value else 0.0))

    built = []
    for name, token, values in series:
        points = tuple(
            LinePoint(
                x=x_at(index),
                y=y_at(value),
                value=value,
                label=x_labels[index] if index < len(x_labels) else "",
            )
            for index, value in enumerate(values)
        )
        path = " ".join(
            f"{'M' if index == 0 else 'L'} {p.x:.2f} {p.y:.2f}"
            for index, p in enumerate(points)
        )
        built.append(LineSeries(name=name, token=token, path=path, points=points))

    divisions = 4
    grid = tuple(
        GridLine(y=y_at(max_value * step // divisions), value=max_value * step // divisions)
        for step in range(divisions + 1)
    )

    return LineChart(
        series=tuple(built),
        x_labels=tuple(x_labels),
        grid=grid,
        width=width,
        height=height,
        plot_top=plot_top,
        plot_bottom=height - plot_bottom,
        plot_left=plot_left,
        max_value=max_value,
        raw_max=raw_max,
    )
