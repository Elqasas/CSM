"""Query-string helpers for filters that are driven by the page itself.

A chart segment is a filter control: clicking "Cloud" should narrow the page to
cloud accounts, and clicking it again should put the page back. That is a toggle
over one query parameter with every other parameter left alone, which is the
part that gets written wrong by hand - a link built from ``?deployment=CLOUD``
silently drops the country and the quarter the reader had already chosen.
"""

from __future__ import annotations

from django import template

register = template.Library()

#: Never carried across a filter change. A page number from the previous,
#: wider result set points into a list that no longer has that many pages.
DROPPED = ("page",)


@register.simple_tag(takes_context=True)
def toggle_filter(context, param: str, value) -> str:
    """The current URL with ``param`` set to ``value``, or cleared if already set.

    Clearing on a second click is what makes a chart a control rather than a
    one-way trip: the segment the reader just selected is still on screen, so it
    is also the way back out, and they never have to find the Clear link to undo
    a click they made by accident.
    """
    value = "" if value is None else str(value)
    # A segment with no key is not a control. Returning "" rather than a URL
    # lets the template ask unconditionally and render an inert element, which
    # is what keeps one copy of the markup instead of two.
    if not param or not value:
        return ""

    request = context["request"]
    params = request.GET.copy()

    if params.get(param, "") == value:
        params.pop(param, None)
    else:
        params[param] = value

    for key in DROPPED:
        params.pop(key, None)

    query = params.urlencode()
    return f"{request.path}?{query}" if query else request.path


@register.simple_tag(takes_context=True)
def set_filter(context, param: str, value) -> str:
    """This page with ``param`` set to ``value``, everything else kept.

    Not ``toggle_filter``: a segmented control always has one option chosen, so
    clicking the one already selected must leave it selected rather than
    clearing it and showing the reader an unfiltered page they did not ask for.
    """
    request = context["request"]
    params = request.GET.copy()
    params[param] = "" if value is None else str(value)
    params.pop("page", None)
    return f"{request.path}?{params.urlencode()}"


@register.simple_tag(takes_context=True)
def sort_link(context, key: str) -> str:
    """This page, sorted by ``key``, keeping every filter already applied.

    A bare ``?sort=name`` - which is what the headers used to be - throws away
    the search, the country and the quarter the reader chose, so sorting a
    filtered list silently unfiltered it.

    Clicking the column already sorted reverses it, which is what a second click
    on a column header means everywhere.
    """
    request = context["request"]
    params = request.GET.copy()
    current = params.get("sort", "")

    params["sort"] = f"-{key}" if current == key else key
    params.pop("page", None)
    return f"{request.path}?{params.urlencode()}"


@register.simple_tag(takes_context=True)
def sort_state(context, key: str) -> str:
    """"ascending", "descending" or "" - for ``aria-sort`` on the header.

    The marker is the attribute rather than a glyph: the column that is sorted
    is announced to a screen reader without putting an arrow beside every
    heading, which is what was asked for.
    """
    current = context["request"].GET.get("sort", "")
    if current == key:
        return "ascending"
    if current == f"-{key}":
        return "descending"
    return ""


@register.simple_tag(takes_context=True)
def is_filtered_on(context, param: str, value) -> bool:
    """Whether this segment is the one currently selected.

    Used to mark the selection visually. Without it a filtered chart is a chart
    with one segment and no explanation of why.
    """
    value = "" if value is None else str(value)
    if not param or not value:
        return False
    return context["request"].GET.get(param, "") == value
