"""Two small helpers the calendar grid needs.

Django templates cannot look a dictionary up by a variable key, and a calendar
is a dictionary looked up by forty-two different dates. The alternative is
building the grid as nested lists in the view and losing the ability to ask a
day anything - so: one filter, kept deliberately dumb.
"""

from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def dictkey(mapping, key):
    """``mapping[key]``, or None. Never raises: a day with nothing on it is the
    common case, not an error."""
    if not hasattr(mapping, "get"):
        return None
    return mapping.get(key)


@register.filter
def entry_overflow(bucket, limit: int) -> int:
    """How many entries a cell had to leave out.

    Counted per section rather than in total, because each section is sliced to
    the same limit: three tasks and three activities both show three.
    """
    if not bucket:
        return 0
    return sum(
        max(len(bucket.get(part, [])) - int(limit), 0)
        for part in ("activities", "meetings", "tasks", "projected")
    )
