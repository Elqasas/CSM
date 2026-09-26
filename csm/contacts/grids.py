"""Arranging contacts into the nine-cell power/interest grid.

Pure functions over a list of contacts: no database, no request. The mapping
itself is not repeated here — each cell asks a throwaway ``Contact`` for its
quadrant, so the grid cannot drift from ``Contact.quadrant`` the way the earlier
two definitions drifted from each other.

Axis order follows the conventional stakeholder grid: influence increases
upward, interest increases rightward, so "Manage closely" sits top-right where
readers expect it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from csm.contacts.models import Contact
from csm.core.enums import Level, PowerInterestQuadrant

#: Top row first.
INFLUENCE_ROWS = (Level.HIGH, Level.MEDIUM, Level.LOW)
#: Leftmost column first.
INTEREST_COLUMNS = (Level.LOW, Level.MEDIUM, Level.HIGH)


@dataclass
class GridCell:
    influence: str
    interest: str
    quadrant: str
    contacts: list = field(default_factory=list)

    @property
    def quadrant_label(self) -> str:
        return PowerInterestQuadrant(self.quadrant).label

    @property
    def is_empty(self) -> bool:
        return not self.contacts


@dataclass
class GridRow:
    influence: str
    cells: list

    @property
    def influence_label(self) -> str:
        return Level(self.influence).label


def build_grid(contacts) -> list:
    """Nine cells, in reading order, each holding the contacts that fall in it.

    Unassessed contacts are not placed. They have no position by definition and
    belong in the triage tray instead.
    """
    buckets: dict[tuple[str, str], list] = {
        (influence, interest): []
        for influence in INFLUENCE_ROWS
        for interest in INTEREST_COLUMNS
    }
    for contact in contacts:
        if not contact.is_assessed:
            continue
        key = (contact.influence, contact.interest)
        if key in buckets:
            buckets[key].append(contact)

    rows = []
    for influence in INFLUENCE_ROWS:
        cells = []
        for interest in INTEREST_COLUMNS:
            probe = Contact(influence=influence, interest=interest)
            cells.append(
                GridCell(
                    influence=influence,
                    interest=interest,
                    quadrant=probe.quadrant,
                    contacts=buckets[(influence, interest)],
                )
            )
        rows.append(GridRow(influence=influence, cells=cells))
    return rows


def interest_labels() -> list:
    return [Level(value).label for value in INTEREST_COLUMNS]
