"""Jira issues into ``Ticket`` rows.

The run, in order:

1. **Read everything first, then write.** No HTTP call happens inside the
   transaction, so a slow Jira holds no lock, and a failure halfway through the
   read writes nothing and leaves the cursor where it was.
2. **Write in one transaction.** Every ticket saves through the model, so the
   ``post_save`` hook in ``health/signals.py`` queues a rescore - and because it
   is one transaction, an account with forty changed issues rescores once.
3. **Sweep when due.** An incremental read only sees issues that changed. An
   issue deleted in Jira, or moved out of every organization, never changes
   again, so once a day (and on ``--full``) the open set is read whole and any
   open ticket missing from it is closed. Closed rather than deleted: it stops
   counting against the client, and the row still says what happened.

The cursor overlaps the previous run by ``OVERLAP`` and every write is an
upsert keyed on the issue and the account, so reading an issue twice is free.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter, defaultdict

from django.db import transaction

from csm.accounts.models import Account
from csm.core import clock
from csm.core.enums import IntegrationProvider, TicketKind, TicketSource, TicketStatus
from csm.engagement.models import Ticket
from csm.integrations.jira import settings as jira_settings
from csm.integrations.jira.client import JiraClient, JiraError, JiraIssue
from csm.integrations.models import SyncState

logger = logging.getLogger(__name__)

#: How far each incremental read reaches back past the previous one. Covers a
#: clock skew between this server and Atlassian's and an issue updated while
#: the previous run was reading.
OVERLAP = dt.timedelta(minutes=10)
SWEEP_EVERY = dt.timedelta(hours=24)

#: What a swept ticket shows as its Jira status, since Jira no longer has one.
GONE_STATUS = "Not open in Jira"

TITLE_MAX = Ticket._meta.get_field("title").max_length
STATUS_MAX = Ticket._meta.get_field("external_status").max_length


def normalise(name: str) -> str:
    """Case and spacing are presentation; the letters are the name."""
    return " ".join(name.casefold().split())


def account_index() -> dict[str, list[int]]:
    """Normalised client name -> the live accounts carrying it.

    Read from every live account, not from anybody's ``Ability``: the sync is
    a system job acting for the whole company, and a ticket must land on its
    client whoever happens to own the account.
    """
    index: dict[str, list[int]] = defaultdict(list)
    for pk, name in Account.objects.alive().values_list("pk", "client_name"):
        index[normalise(name)].append(pk)
    return dict(index)


def ticket_values(issue: JiraIssue, bug_issue_types=("Bug",)) -> dict:
    """The ticket columns an issue determines. Pure."""
    bug_types = {normalise(t) for t in bug_issue_types}
    done = issue.is_done
    return {
        "ticket_kind": TicketKind.BUG if normalise(issue.issue_type) in bug_types else TicketKind.GENERAL,
        "status": TicketStatus.CLOSED if done else TicketStatus.OPEN,
        "title": (issue.summary or issue.key)[:TITLE_MAX],
        "opened_on": clock.to_business_date(issue.created),
        # A Done issue with no resolution date is a workflow that never sets
        # one; the moment it last changed is the nearest honest answer.
        "closed_on": clock.to_business_date(issue.resolved or issue.updated) if done else None,
        "external_type": issue.issue_type[:STATUS_MAX],
        "external_status": issue.status[:STATUS_MAX],
    }


def _latest_per_key(issues) -> list[JiraIssue]:
    latest: dict[str, JiraIssue] = {}
    for issue in issues:
        held = latest.get(issue.key)
        if held is None or issue.updated >= held.updated:
            latest[issue.key] = issue
    return list(latest.values())


def apply_issues(issues, index: dict[str, list[int]], bug_issue_types=("Bug",)) -> dict:
    """Upsert one ticket per issue per matched account. Returns the counts."""
    created = updated = removed = 0
    unmatched: Counter[str] = Counter()
    ambiguous: Counter[str] = Counter()

    for issue in _latest_per_key(issues):
        account_ids: set[int] = set()
        for org in issue.organizations:
            matches = index.get(normalise(org), [])
            if len(matches) == 1:
                account_ids.add(matches[0])
            elif matches:
                ambiguous[org] += 1
            else:
                unmatched[org] += 1

        values = ticket_values(issue, bug_issue_types)
        existing = {
            t.account_id: t
            for t in Ticket.objects.filter(source=TicketSource.JIRA, external_key=issue.key)
        }

        for account_id in account_ids:
            ticket = existing.pop(account_id, None)
            if ticket is None:
                Ticket.objects.create(
                    account_id=account_id,
                    source=TicketSource.JIRA,
                    external_key=issue.key,
                    created_by=None,
                    **values,
                )
                created += 1
                continue
            changed = [name for name, value in values.items() if getattr(ticket, name) != value]
            if changed:
                for name in changed:
                    setattr(ticket, name, values[name])
                ticket.save(update_fields=[*changed, "updated_at"])
                updated += 1

        # Whatever is left belongs to an organization the issue is no longer
        # raised under. It was never that client's ticket, so it goes.
        for ticket in existing.values():
            ticket.delete()
            removed += 1

    return {
        "created": created,
        "updated": updated,
        "removed": removed,
        "unmatched_organizations": dict(sorted(unmatched.items())),
        "ambiguous_organizations": dict(sorted(ambiguous.items())),
    }


def close_missing(open_keys: set[str]) -> int:
    """Close every open Jira ticket whose issue is no longer open in Jira."""
    closed = 0
    stale = Ticket.objects.filter(source=TicketSource.JIRA, status=TicketStatus.OPEN).exclude(
        external_key__in=open_keys
    )
    for ticket in stale:
        # One save per row, never queryset.update(): an update fires no
        # post_save, and the account would keep counting the ticket until the
        # nightly recalculation.
        ticket.status = TicketStatus.CLOSED
        ticket.closed_on = clock.today()
        ticket.external_status = GONE_STATUS
        ticket.save(update_fields=["status", "closed_on", "external_status", "updated_at"])
        closed += 1
    return closed


def run(client: JiraClient, *, full: bool = False, config=None) -> dict:
    """One sync. Raises ``JiraError`` after recording it on the sync state."""
    config = config or jira_settings.current()
    state, _ = SyncState.objects.get_or_create(provider=IntegrationProvider.JIRA)
    started = clock.now()
    state.last_started_at = started
    state.save(update_fields=["last_started_at"])

    first_run = state.cursor is None
    sweep = (
        full
        or first_run
        or state.last_full_sweep_at is None
        or started - state.last_full_sweep_at >= SWEEP_EVERY
    )

    try:
        if first_run:
            since = started - dt.timedelta(days=config.backfill_days)
        else:
            since = state.cursor - OVERLAP
        issues = list(client.issues_updated_since(since))
        open_keys: set[str] | None = None
        if sweep:
            open_now = list(client.open_issues())
            open_keys = {issue.key for issue in open_now}
            issues.extend(open_now)
    except JiraError as exc:
        state.last_error = str(exc)
        state.save(update_fields=["last_error"])
        logger.warning("Jira sync failed: %s", exc)
        raise

    with transaction.atomic():
        report = apply_issues(issues, account_index(), config.bug_issue_types)
        report["closed_by_sweep"] = close_missing(open_keys) if open_keys is not None else None
        report["read"] = len(issues)
        report["swept"] = sweep
        if not sweep:
            # A quiet incremental run sees a handful of issues, so on its own
            # it would empty the unmatched list without anything having been
            # fixed. The list is rebuilt whole by each sweep and only added to
            # between sweeps.
            for key in ("unmatched_organizations", "ambiguous_organizations"):
                carried = dict((state.last_report or {}).get(key, {}))
                for name, count in report[key].items():
                    carried[name] = max(count, carried.get(name, 0))
                report[key] = dict(sorted(carried.items()))

        state.cursor = started
        if sweep:
            state.last_full_sweep_at = started
        state.last_success_at = clock.now()
        state.last_error = ""
        state.last_report = report
        state.save()

    return report
