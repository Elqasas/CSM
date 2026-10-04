"""The shape the sync reads, and the interface any Graph source must offer.

``GraphEvent`` is an Outlook event once the HTTP layer has unpacked it: plain
values, so the sync is tested by constructing events directly.
``FakeGraphClient`` is the in-memory Microsoft 365 the tests use.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol


class GraphError(Exception):
    """Graph could not be reached, refused the credentials, or answered nonsense."""


class GraphNotFound(GraphError):
    """404: no such mailbox, user or meeting. Expected, and handled per item."""


class GraphForbidden(GraphError):
    """403: the app may not read this one - an access policy excludes it."""


@dataclass(frozen=True)
class Attendee:
    email: str
    name: str = ""
    response: str = ""


@dataclass(frozen=True)
class GraphEvent:
    event_id: str
    ical_uid: str
    subject: str
    starts_at: dt.datetime
    ends_at: dt.datetime
    is_all_day: bool = False
    is_cancelled: bool = False
    is_private: bool = False
    is_teams: bool = False
    join_url: str = ""
    web_link: str = ""
    location: str = ""
    organizer_email: str = ""
    organizer_name: str = ""
    response: str = ""
    attendees: tuple[Attendee, ...] = field(default_factory=tuple)


class GraphClient(Protocol):
    def calendar_view(self, mailbox: str, start: dt.datetime, end: dt.datetime) -> list[GraphEvent]:
        """Every occurrence in ``mailbox``'s calendar overlapping ``start``..``end``.

        Raises ``GraphNotFound`` when the address has no mailbox.
        """

    def attendance(self, organizer: str, join_url: str) -> list[str] | None:
        """Email addresses that actually joined this Teams meeting.

        ``None`` when there is no attendance report (yet) - which is not the
        same answer as an empty list, where a report exists and nobody with an
        address joined.
        """


class FakeGraphClient:
    """An in-memory Microsoft 365: calendars by mailbox, reports by join URL."""

    def __init__(
        self,
        calendars: dict[str, Iterable[GraphEvent]] | None = None,
        reports: dict[str, list[str]] | None = None,
    ):
        self.calendars = {k.lower(): list(v) for k, v in (calendars or {}).items()}
        self.reports = dict(reports or {})
        self.error: GraphError | None = None
        self.forbidden: set[str] = set()
        self.attendance_calls: list[str] = []

    def calendar_view(self, mailbox, start, end):
        if self.error is not None:
            raise self.error
        mailbox = mailbox.lower()
        if mailbox in self.forbidden:
            raise GraphForbidden(f"not allowed to read {mailbox}")
        if mailbox not in self.calendars:
            raise GraphNotFound(f"no mailbox {mailbox}")
        return [e for e in self.calendars[mailbox] if e.starts_at < end and e.ends_at > start]

    def attendance(self, organizer, join_url):
        if self.error is not None:
            raise self.error
        self.attendance_calls.append(join_url)
        return self.reports.get(join_url)
