"""The scheduled half of the dual trigger.

Catches the decay that no event announces: the factors that measure days since
something happened get worse on their own, and nothing writes a row to say so.
"""

from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand

from csm.core import clock
from csm.core.enums import ScoreTrigger
from csm.health.services import recalculate_all


class Command(BaseCommand):
    help = "Recalculate the health score for every account in a scored lifecycle stage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--as-of",
            type=dt.date.fromisoformat,
            default=None,
            help="Score as at this date (YYYY-MM-DD) instead of today. For backfills.",
        )

    def handle(self, *args, **options):
        as_of = options["as_of"] or clock.today()
        count = recalculate_all(as_of=as_of, trigger=ScoreTrigger.DAILY)
        self.stdout.write(
            self.style.SUCCESS(f"Recalculated {count} accounts as at {as_of}.")
        )
