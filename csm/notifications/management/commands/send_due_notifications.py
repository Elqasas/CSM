"""Build the morning digests and drain the email outbox.

Safe to run repeatedly: the dedupe key on each notification means a second run
in the same day sends nothing further.
"""

from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand

from csm.core import clock
from csm.notifications.dispatcher import send_digests, send_pending_emails


class Command(BaseCommand):
    help = "Send task digests and any queued notification emails."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", type=dt.date.fromisoformat, default=None)

    def handle(self, *args, **options):
        as_of = options["as_of"] or clock.today()
        digests = send_digests(as_of=as_of)
        sent = send_pending_emails()
        self.stdout.write(
            self.style.SUCCESS(f"Queued {digests} digests and sent {sent} emails.")
        )
