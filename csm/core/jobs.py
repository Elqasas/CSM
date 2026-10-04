"""The scheduled jobs, as importable callables.

django-q stores a schedule as a dotted path to a function. Pointing it at
``call_command`` with the command name as a string argument would work, but the
argument is stored as text and evaluated at run time, so a typo becomes a
failure at three in the morning rather than an ImportError now. These wrappers
are checked when this module is imported.

Each one is the same command the operator can run by hand, so a schedule and a
manual run cannot drift apart.
"""

from __future__ import annotations

from django.core.management import call_command


def recalculate_health() -> None:
    """The daily half of the dual trigger."""
    call_command("recalculate_health")


def generate_tasks() -> None:
    """Renewal, quarterly and rollover reminders."""
    call_command("generate_tasks")


def send_due_notifications() -> None:
    """Morning digests, then drain the email outbox."""
    call_command("send_due_notifications")


def sync_jira() -> None:
    """Pull changed JSM issues into tickets. A no-op until Jira is configured."""
    call_command("sync_jira")


def send_task_reminders() -> None:
    """The hour-before and half-hour-before reminders for timed tasks."""
    call_command("send_task_reminders")
