"""A tiny in-process domain event bus.

Services publish typed events; subscribers react. This is the seam that lets the
deferred Jira, Teams and Outlook work arrive as new subscribers rather than as
edits to the health or task engines.

Events are published inside the transaction that produced them and delivered
after it commits, so a subscriber never observes a state that was rolled back.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainEvent:
    """Base class. Subclasses are plain frozen dataclasses of scalars only."""


@dataclass(frozen=True)
class HealthRecalculated(DomainEvent):
    account_id: int
    previous_effective_status: str | None
    new_effective_status: str
    computed_status: str
    score: Decimal | None
    max_score: int | None
    trigger: str
    as_of: date


@dataclass(frozen=True)
class AccountStageChanged(DomainEvent):
    account_id: int
    previous_stage: str
    new_stage: str
    changed_by_id: int | None


@dataclass(frozen=True)
class ActivityLogged(DomainEvent):
    activity_id: int
    account_id: int
    contact_id: int | None
    activity_type: str
    occurred_on: date


@dataclass(frozen=True)
class TaskCreated(DomainEvent):
    task_id: int
    account_id: int
    task_type: str
    category: str
    due_date: date


@dataclass(frozen=True)
class TaskCompleted(DomainEvent):
    task_id: int
    account_id: int
    task_type: str
    completed_by_id: int | None


@dataclass(frozen=True)
class ImportApplied(DomainEvent):
    batch_id: int
    account_ids: tuple[int, ...]
    baseline_date: date


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------

Subscriber = Callable[[Any], None]

_subscribers: dict[type, list[Subscriber]] = defaultdict(list)


def subscribe(event_type: type, handler: Subscriber) -> None:
    """Register ``handler`` for ``event_type``. Idempotent per handler."""
    if handler not in _subscribers[event_type]:
        _subscribers[event_type].append(handler)


def unsubscribe(event_type: type, handler: Subscriber) -> None:
    if handler in _subscribers[event_type]:
        _subscribers[event_type].remove(handler)


def publish(event: DomainEvent, *, immediate: bool = False) -> None:
    """Deliver ``event`` to its subscribers after the current transaction commits.

    A failing subscriber is logged and skipped rather than allowed to break the
    others, because delivery happens after commit and cannot be rolled back.
    """
    if immediate:
        _dispatch(event)
    else:
        transaction.on_commit(lambda: _dispatch(event))


def _dispatch(event: DomainEvent) -> None:
    for handler in list(_subscribers[type(event)]):
        try:
            handler(event)
        except Exception:  # noqa: BLE001 - a subscriber must not break its peers
            logger.exception(
                "Subscriber %r failed handling %s", handler, type(event).__name__
            )


def clear_subscribers() -> None:
    """Test helper. Never call this from application code."""
    _subscribers.clear()
