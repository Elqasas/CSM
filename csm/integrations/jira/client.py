"""The shape the sync reads, and the interface any Jira source must offer.

``JiraIssue`` is what survives of an issue once the HTTP layer has unpacked it:
plain values, no nested JSON, so the sync can be tested by constructing issues
directly. ``FakeJiraClient`` is the in-memory source the tests use, and what a
developer without Jira credentials can point the sync at.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

#: Jira's fixed status categories. Workflows name their statuses freely, but
#: every status belongs to one of these three, which is why the sync reads the
#: category and never the status name.
CATEGORY_TODO = "new"
CATEGORY_IN_PROGRESS = "indeterminate"
CATEGORY_DONE = "done"


class JiraError(Exception):
    """Jira could not be reached, refused the credentials, or answered nonsense."""


@dataclass(frozen=True)
class JiraIssue:
    key: str
    issue_type: str
    status: str
    status_category: str
    summary: str
    created: dt.datetime
    updated: dt.datetime
    resolved: dt.datetime | None = None
    organizations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_done(self) -> bool:
        return self.status_category == CATEGORY_DONE


class JiraClient(Protocol):
    def issues_updated_since(self, since: dt.datetime) -> Iterator[JiraIssue]:
        """Every issue under an organization that changed at or after ``since``."""

    def open_issues(self) -> Iterator[JiraIssue]:
        """Every issue under an organization whose status is not Done."""


class FakeJiraClient:
    """An in-memory Jira, answering the same two questions."""

    def __init__(self, issues: Iterable[JiraIssue] = ()):
        self.issues: list[JiraIssue] = list(issues)
        self.error: JiraError | None = None

    def _all(self) -> list[JiraIssue]:
        if self.error is not None:
            raise self.error
        return [issue for issue in self.issues if issue.organizations]

    def issues_updated_since(self, since: dt.datetime) -> Iterator[JiraIssue]:
        return iter([issue for issue in self._all() if issue.updated >= since])

    def open_issues(self) -> Iterator[JiraIssue]:
        return iter([issue for issue in self._all() if not issue.is_done])
