"""Reading a contacts file.

A separate upload from accounts. The specification fills contact details in from
the account profile over time, but a client arriving with forty stakeholders
already recorded somewhere else should not have to be retyped.

The account is matched by client code and must already exist. Creating an
account implicitly from a contacts file would produce one with a name and
nothing else, which then reads as a real client in every list and every count on
the dashboard.

Value coercion is shared with the accounts parser rather than reimplemented, so
"Yes", a date format or a channel name means the same thing in both files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from csm.core.enums import CommunicationChannel, Level
from csm.importer.parsing import (
    TRUE_WORDS,
    _choice,
    _text,
    normalise_header,
    read_table,
)

CONTACT_COLUMNS: dict[str, tuple] = {
    "client_code": ("client code", "code", "account code", "client id"),
    "full_name": ("full name", "name", "contact name", "contact"),
    "title": ("title", "role", "title role", "job title", "position"),
    "email": ("email", "email address", "e mail"),
    "phone": ("phone", "phone number", "mobile", "telephone"),
    "influence": ("influence", "influence level", "power"),
    "interest": ("interest", "interest level"),
    "channels": ("channels", "communication channels", "contact channels"),
    "preferred_channel": ("preferred channel", "preferred contact method"),
    "focal_rank": ("focal rank", "focal point level", "focal level", "focal point"),
    "notes": ("notes", "comment", "comments"),
}

CONTACT_REQUIRED_COLUMNS = ("client_code", "full_name")

#: Separators people actually use inside one cell for a multi-select.
CHANNEL_SPLIT = re.compile(r"[,;/|]+")


@dataclass
class ParsedContact:
    row_number: int
    client_code: str = ""
    full_name: str = ""
    cleaned: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def error(self, column: str, message: str) -> None:
        self.errors.append({"column": column, "message": message})

    def warn(self, column: str, message: str) -> None:
        self.warnings.append({"column": column, "message": message})


def _channels(value, row: ParsedContact) -> list:
    """A multi-select in one cell, however the author separated it."""
    text = _text(value)
    if not text:
        return []

    chosen: list = []
    for part in CHANNEL_SPLIT.split(text):
        part = part.strip()
        if not part:
            continue
        folded = normalise_header(part)
        for stored, label in CommunicationChannel.choices:
            if folded in (normalise_header(stored), normalise_header(label)):
                if stored not in chosen:
                    chosen.append(stored)
                break
        else:
            allowed = ", ".join(label for _, label in CommunicationChannel.choices)
            row.error("Channels", f"{part!r} is not one of: {allowed}.")
    return chosen


def parse_contact_row(row_number: int, values: dict, *, known_codes: set) -> ParsedContact:
    """One contact row: cleaned values plus errors.

    Influence and interest stay optional. A blank cell means nobody has assessed
    this person - a real state in this schema rather than a stand-in for Medium -
    so a blank imports as unassessed and the contact appears in the triage tray.
    """
    row = ParsedContact(row_number=row_number)
    row.client_code = _text(values.get("client_code"))
    row.full_name = _text(values.get("full_name"))

    if not row.client_code:
        row.error("Client code", "This column is required.")
    elif row.client_code.lower() not in known_codes:
        row.error(
            "Client code",
            f"No account has the code {row.client_code!r}. Import the accounts "
            "file first; a contacts file never creates an account.",
        )
    if not row.full_name:
        row.error("Full name", "This column is required.")

    cleaned = {
        "full_name": row.full_name[:150],
        "title": _text(values.get("title"))[:150],
        "email": _text(values.get("email"))[:254],
        "phone": _text(values.get("phone"))[:32],
        "notes": _text(values.get("notes")),
    }

    if cleaned["email"] and "@" not in cleaned["email"]:
        row.error("Email", f"{cleaned['email']!r} is not an email address.")

    for key, column in (("influence", "Influence"), ("interest", "Interest")):
        if _text(values.get(key)):
            cleaned[key] = _choice(values.get(key), Level.choices, row, column)
        else:
            cleaned[key] = None

    if cleaned["influence"] is None and cleaned["interest"] is None:
        row.warn(
            "Influence",
            "Not assessed, so this contact has no grid position and appears in "
            "the triage tray.",
        )
    elif None in (cleaned["influence"], cleaned["interest"]):
        # Half an assessment is still unassessed as far as the grid is
        # concerned. Saying so beats letting somebody believe they have placed
        # this person and then not finding them on it.
        row.warn(
            "Influence",
            "Only one of influence and interest is set, so this contact still "
            "has no position on the grid.",
        )

    channels = _channels(values.get("channels"), row)
    cleaned["channels"] = channels

    if _text(values.get("preferred_channel")):
        preferred = _choice(
            values.get("preferred_channel"),
            CommunicationChannel.choices, row, "Preferred channel",
        )
        cleaned["preferred_channel"] = preferred or ""
        if preferred and channels and preferred not in channels:
            row.error(
                "Preferred channel",
                "The preferred channel must be one of the selected channels.",
            )
    else:
        cleaned["preferred_channel"] = ""

    cleaned["focal_rank"] = _focal_rank(values.get("focal_rank"), row)
    cleaned["is_focal_point"] = cleaned["focal_rank"] is not None

    row.cleaned = cleaned
    return row


def _focal_rank(value, row: ParsedContact):
    """Focal point L1 or L2, or not a focal point at all.

    A bare "Yes" is rejected rather than guessed at. The database keeps one
    contact per rank per account, so an unranked focal point has nowhere to go.
    """
    text = _text(value)
    if not text:
        return None

    # Digits first. ``TRUE_WORDS`` contains "1", so checking for a bare "yes"
    # before this would reject the most ordinary value the column can hold.
    digits = re.sub(r"[^0-9]", "", text)
    if digits in ("1", "2"):
        return int(digits)

    if text.lower() in TRUE_WORDS:
        row.error(
            "Focal rank",
            "A focal point needs a rank of 1 or 2, not just 'yes', so the two "
            "are distinguishable.",
        )
        return None

    row.error("Focal rank", f"{text!r} is not 1 or 2.")
    return None


def parse_contacts(file_obj, *, known_codes: set, filename: str = "") -> tuple:
    """Parse a contacts file into ``(ParsedContact list, unknown column list)``."""
    header_map, body, unknown = read_table(
        file_obj,
        filename=filename,
        columns=CONTACT_COLUMNS,
        required=CONTACT_REQUIRED_COLUMNS,
    )

    folded_codes = {code.lower() for code in known_codes}
    parsed = []
    seen_ranks: dict = {}

    for row_number, values in body:
        picked = {
            name: (values[index] if index < len(values) else None)
            for name, index in header_map.items()
        }
        row = parse_contact_row(row_number, picked, known_codes=folded_codes)

        # One contact per focal rank per account is a database constraint, so
        # two rows claiming rank 1 for the same client would otherwise fail the
        # whole import at write time instead of here, where the row can be named.
        rank = row.cleaned.get("focal_rank")
        if rank and row.client_code:
            key = (row.client_code.lower(), rank)
            first = seen_ranks.get(key)
            if first is not None:
                row.error(
                    "Focal rank",
                    f"Row {first} already claims focal rank {rank} for this client.",
                )
            else:
                seen_ranks[key] = row_number

        parsed.append(row)

    return parsed, unknown
