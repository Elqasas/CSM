"""Automatic task generation.

Every generator is idempotent. The keys contain no timestamp, so running the
daily job five times produces one task; they do contain the business date, so
moving a contract or meeting date is correctly treated as a new obligation.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.db import transaction

from csm.accounts.models import Account
from csm.core import clock
from csm.core.enums import (
    NO_REMINDER_STAGES,
    HealthStatus,
    TaskCategory,
    TaskCloseReason,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from csm.core.events import HealthRecalculated, subscribe
from csm.health.models import HealthScoreHistory
from csm.tasks.models import Task
from csm.tasks.services import cancel_task, create_task

logger = logging.getLogger(__name__)

#: Reminders at ninety, sixty and thirty days before the contract ends. Three
#: separate tasks rather than one sliding task, so "reminded at ninety days and
#: nothing happened" stays visible instead of being overwritten.
RENEWAL_OFFSETS = (90, 60, 30)

#: How far back a generator reaches to create a checkpoint it missed. Without
#: this, an account imported three weeks before its renewal would spawn all
#: three reminders at once, every one of them already overdue.
RENEWAL_CATCHUP_DAYS = 7

QUARTERLY_LEAD_DAYS = 7


# ---------------------------------------------------------------------------
# Idempotency keys
# ---------------------------------------------------------------------------


def renewal_key(account_id: int, end_date: date, offset: int) -> str:
    return f"renewal:{account_id}:{end_date.isoformat()}:{offset}"


def quarterly_key(account_id: int, meeting_date: date) -> str:
    return f"qmeeting:{account_id}:{meeting_date.isoformat()}"


def quarterly_rollover_key(account_id: int, stale_date: date) -> str:
    return f"qmeeting_rollover:{account_id}:{stale_date.isoformat()}"


# ---------------------------------------------------------------------------
# Renewal
# ---------------------------------------------------------------------------


def generate_renewal_tasks(*, as_of: date | None = None) -> int:
    """Create the renewal checkpoints that fall due today.

    Embedding the contract end date in the key gives a useful property free:
    when a contract is renewed and the end date moves forward, a fresh cycle of
    ninety, sixty and thirty day reminders generates for the new date without
    any reset logic.
    """
    as_of = as_of or clock.today()
    created = 0
    accounts = (
        Account.objects.alive()
        .exclude(lifecycle_stage__in=list(NO_REMINDER_STAGES))
        .exclude(contract_end_date__isnull=True)
        .filter(contract_end_date__gte=as_of)
    )
    for account in accounts:
        for offset in RENEWAL_OFFSETS:
            trigger_date = account.contract_end_date - timedelta(days=offset)
            earliest = as_of - timedelta(days=RENEWAL_CATCHUP_DAYS)
            if not (earliest <= trigger_date <= as_of):
                continue
            task = create_task(
                account=account,
                task_type=TaskType.AUTO_RENEWAL,
                title=f"Renewal in {offset} days: {account.client_name}",
                description=(
                    f"The contract ends on {account.contract_end_date:%d %B %Y}. "
                    f"This is the {offset} day checkpoint."
                ),
                due_date=trigger_date,
                category=TaskCategory.CLIENT_FACING,
                auto_key=renewal_key(account.pk, account.contract_end_date, offset),
            )
            if task:
                created += 1
    return created


def generate_renewal_catchup(account: Account, *, as_of: date | None = None) -> Task | None:
    """One reminder for an account imported already inside the renewal window.

    Creates only the most urgent checkpoint already passed, so an account twenty
    days from renewal gets the thirty day task alone rather than all three.
    """
    as_of = as_of or clock.today()
    if not account.contract_end_date or account.contract_end_date < as_of:
        return None
    if account.lifecycle_stage in NO_REMINDER_STAGES:
        return None

    days_left = (account.contract_end_date - as_of).days
    passed = [o for o in RENEWAL_OFFSETS if days_left <= o]
    if not passed:
        return None
    offset = min(passed)

    return create_task(
        account=account,
        task_type=TaskType.AUTO_RENEWAL,
        title=f"Renewal in {days_left} days: {account.client_name}",
        description=(
            f"The contract ends on {account.contract_end_date:%d %B %Y}. "
            f"Imported inside the {offset} day window."
        ),
        due_date=as_of,
        category=TaskCategory.CLIENT_FACING,
        auto_key=renewal_key(account.pk, account.contract_end_date, offset),
    )


def cancel_stale_renewal_tasks(account: Account, previous_end_date: date | None) -> int:
    """Close reminders that referred to a contract end date that has since moved."""
    if previous_end_date is None:
        return 0
    stale_keys = [
        renewal_key(account.pk, previous_end_date, offset) for offset in RENEWAL_OFFSETS
    ]
    cancelled = 0
    for task in Task.objects.open().filter(auto_key__in=stale_keys):
        cancel_task(task, reason=TaskCloseReason.CONTRACT_DATE_CHANGED)
        cancelled += 1
    return cancelled


# ---------------------------------------------------------------------------
# Quarterly meeting
# ---------------------------------------------------------------------------


def generate_quarterly_tasks(*, as_of: date | None = None) -> int:
    as_of = as_of or clock.today()
    created = 0
    accounts = (
        Account.objects.alive()
        .exclude(lifecycle_stage__in=list(NO_REMINDER_STAGES))
        .exclude(quarterly_meeting_date__isnull=True)
    )
    for account in accounts:
        meeting_date = account.quarterly_meeting_date
        lead_date = meeting_date - timedelta(days=QUARTERLY_LEAD_DAYS)

        if lead_date <= as_of <= meeting_date:
            task = create_task(
                account=account,
                task_type=TaskType.AUTO_QUARTERLY_MEETING,
                title=f"Quarterly meeting: {account.client_name}",
                description=f"Scheduled for {meeting_date:%d %B %Y}.",
                due_date=lead_date,
                category=TaskCategory.CLIENT_FACING,
                auto_key=quarterly_key(account.pk, meeting_date),
            )
            if task:
                created += 1
            continue

        # The date has gone by and no new one was set. Without this nudge, a
        # manually maintained field goes stale and the account quietly stops
        # receiving quarterly reminders for good.
        if meeting_date < as_of:
            has_open = (
                Task.objects.open()
                .filter(account=account, task_type=TaskType.AUTO_QUARTERLY_MEETING)
                .exists()
            )
            if not has_open:
                task = create_task(
                    account=account,
                    task_type=TaskType.AUTO_QUARTERLY_MEETING,
                    title=f"Schedule the next quarterly meeting: {account.client_name}",
                    description=(
                        f"The last recorded date was {meeting_date:%d %B %Y}, which has "
                        "passed. Set the next one so the reminders continue."
                    ),
                    due_date=as_of,
                    category=TaskCategory.INTERNAL,
                    auto_key=quarterly_rollover_key(account.pk, meeting_date),
                )
                if task:
                    created += 1
    return created


def cancel_stale_quarterly_tasks(account: Account, previous_date: date | None) -> int:
    if previous_date is None:
        return 0
    cancelled = 0
    for task in Task.objects.open().filter(auto_key=quarterly_key(account.pk, previous_date)):
        cancel_task(task, reason=TaskCloseReason.MEETING_DATE_CHANGED)
        cancelled += 1
    return cancelled


# ---------------------------------------------------------------------------
# Health alert
# ---------------------------------------------------------------------------


def open_health_alerts(account_id: int):
    return Task.objects.filter(
        account_id=account_id,
        task_type=TaskType.AUTO_HEALTH_ALERT,
        status=TaskStatus.PENDING,
    )


@transaction.atomic
def on_health_recalculated(event: HealthRecalculated) -> None:
    """Open or close the health alert for an account.

    The rule is state-based rather than transition-based, so it stays correct
    even when a recalculation is missed, replayed, or runs twice at once.
    """
    account = Account.objects.filter(pk=event.account_id).first()
    if account is None:
        return

    if event.new_effective_status == HealthStatus.AT_RISK:
        _open_alert(account, event)
    else:
        _close_alerts(account)


def _open_alert(account: Account, event: HealthRecalculated) -> Task | None:
    if account.health_alert_suppressed_at is not None:
        # Someone closed this alert by hand while the account was still red.
        # That decision stands until the account recovers and relapses.
        return None

    # Duplicate suppression is the partial unique index on the table, not a
    # check here. create_task returns None when the insert collides, which is
    # the correct outcome rather than an error.
    return create_task(
        account=account,
        task_type=TaskType.AUTO_HEALTH_ALERT,
        title=f"At Risk: {account.client_name}",
        description=_alert_description(account, event),
        due_date=event.as_of,
        category=TaskCategory.INTERNAL,
        # The one task that means stop what you are doing - and the only one
        # still notified the moment it is raised, rather than in the morning.
        priority=TaskPriority.HIGH,
    )


def _close_alerts(account: Account) -> int:
    """Close any open alert once the account is no longer At Risk.

    Watch counts as leaving red, as does the account moving to a stage where
    health is not calculated, and an override to a non-red status.
    """
    closed = 0
    for task in open_health_alerts(account.pk):
        task.status = TaskStatus.AUTO_CLOSED
        task.close_reason = TaskCloseReason.HEALTH_RECOVERED
        task.save(update_fields=["status", "close_reason", "updated_at"])
        closed += 1
    return closed


def _alert_description(account: Account, event: HealthRecalculated) -> str:
    """Name the weakest factors, so the alert is an instruction and not a colour."""
    score_text = str(event.score) if event.score is not None else "not available"
    lines = [f"This account is At Risk with a score of {score_text}."]

    row = (
        HealthScoreHistory.objects.filter(account=account).order_by("-calculated_at").first()
    )
    if row and row.breakdown:
        factors = row.breakdown.get("factors") or {}
        weak = [
            (fid, data) for fid, data in factors.items() if data.get("color") in ("RED", "YELLOW")
        ]
        weak.sort(key=lambda pair: pair[1].get("points") or 0)
        if weak:
            named = ", ".join(
                "{} ({})".format(fid.replace("_", " ").title(), data["color"].lower())
                for fid, data in weak[:3]
            )
            lines.append(f"Weakest factors: {named}.")
    return " ".join(lines)


def register_subscribers() -> None:
    """Wire the health engine to the task engine."""
    subscribe(HealthRecalculated, on_health_recalculated)
