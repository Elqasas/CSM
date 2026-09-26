"""Create the reminders that fall due today.

Safe to run repeatedly: every generator is keyed so a second run creates nothing.
"""

from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand

from csm.core import clock
from csm.tasks import recurrence
from csm.tasks.generators import generate_quarterly_tasks, generate_renewal_tasks


class Command(BaseCommand):
    help = "Generate renewal and quarterly meeting reminders."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", type=dt.date.fromisoformat, default=None)

    def handle(self, *args, **options):
        as_of = options["as_of"] or clock.today()
        renewals = generate_renewal_tasks(as_of=as_of)
        quarterly = generate_quarterly_tasks(as_of=as_of)
        # Repeating tasks normally extend themselves the moment an occurrence is
        # completed. This pass is for the series whose occurrence was cancelled
        # instead, so a repeat cannot silently die.
        repeats = recurrence.top_up(as_of)
        self.stdout.write(
            self.style.SUCCESS(
                f"Created {renewals} renewal, {quarterly} quarterly and {repeats} "
                f"repeating tasks as at {as_of}."
            )
        )
