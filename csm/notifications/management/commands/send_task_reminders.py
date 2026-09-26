"""Send the reminders that fall due for timed tasks, then drain the outbox.

Runs every few minutes rather than nightly: an hour before a 09:00 call is
08:00, which no daily job can hit. Safe to run repeatedly and safe to miss a
run - each reminder has a dedupe key, and one too late to be true is dropped
rather than delivered wrong.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from csm.notifications.dispatcher import send_pending_emails, send_task_reminders


class Command(BaseCommand):
    help = "Remind assignees before a task with a clock time falls due."

    def handle(self, *args, **options):
        reminders = send_task_reminders()
        sent = send_pending_emails()
        self.stdout.write(
            self.style.SUCCESS(f"Queued {reminders} reminders and sent {sent} emails.")
        )
