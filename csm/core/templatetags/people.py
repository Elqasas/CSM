"""A person's mark: initials in a circle, in a tone derived from their name.

A circle because account marks are squares - on a page showing both, the shape
alone says which is the company and which is the person. The tone is a stable
hash of the name, not Python's ``hash()``, which is salted per process and
would recolour everybody on every restart.
"""

from __future__ import annotations

import zlib

from django import template

register = template.Library()

#: How many tones ``.person-tone-N`` defines in app.css.
TONES = 6


@register.filter
def initials(name) -> str:
    """First letter of the first and last words: "Abdulaziz J. Aljabr" is AA."""
    words = [word for word in str(name or "").replace(".", " ").split() if word[0].isalpha()]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[-1][0]).upper()


@register.filter
def person_tone(name) -> int:
    return zlib.crc32(str(name or "").strip().lower().encode("utf-8")) % TONES
