"""A notification body as lines, so the page can lay a digest out.

The body is stored as plain text because the same text is the email. On the
page, though, run together it reads as one paragraph: "Overdue (1): - test 3 -
Arabia Telecom (due ...) Today (1): - call ...". This splits it back into what
the digest builder wrote - a greeting, group headings, one line per task -
without the page and the email needing two builders that could drift.
"""

from __future__ import annotations

import re

from django import template

register = template.Library()

#: A group heading exactly as ``dispatcher._digest_body`` writes one.
HEADING = re.compile(r"^(?P<label>.+?) \((?P<count>\d+)\):$")

#: The heading's tone, so Overdue reads as the one to act on first.
TONES = {"Overdue": "danger", "Today": "primary"}


@register.filter
def body_lines(body) -> list[dict]:
    lines = []
    for raw in str(body or "").splitlines():
        text = raw.strip()
        if not text:
            continue
        heading = HEADING.match(text)
        if heading:
            label = heading.group("label")
            lines.append(
                {
                    "kind": "heading",
                    "text": label,
                    "count": heading.group("count"),
                    "tone": TONES.get(label, "secondary"),
                }
            )
        elif text.startswith("- "):
            lines.append({"kind": "item", "text": text[2:]})
        else:
            lines.append({"kind": "text", "text": text})
    return lines
