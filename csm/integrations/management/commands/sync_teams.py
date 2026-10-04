"""Read Outlook calendars into the platform calendar, and Teams attendance into activities.

Runs every fifteen minutes from the schedule. Safe to re-run. Exits quietly when
Microsoft 365 is not connected, so the schedule can be registered first.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from csm.integrations.teams import settings as teams
from csm.integrations.teams.client import GraphError
from csm.integrations.teams.sync import run


class Command(BaseCommand):
    help = "Sync Outlook calendars and Teams attendance from Microsoft 365. Safe to re-run."

    def handle(self, *args, **options):
        config = teams.current()
        if not config.is_configured:
            self.stdout.write(self.style.WARNING("Microsoft 365 is not connected; nothing to sync."))
            return

        try:
            report = run(teams.get_client(), count_attendance=config.count_attendance)
        except GraphError as exc:
            raise CommandError(str(exc)) from exc

        attendance = report["attendance"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Read {report['calendars_read']} calendars, {report['meetings']} meetings: "
                f"{report['created']} new, {report['meetings_deleted']} gone. "
                f"Attendance: {attendance['counted']} counted, {attendance['no_show']} no client."
            )
        )
        for email in report["no_mailbox"]:
            self.stdout.write(self.style.WARNING(f"No Microsoft 365 mailbox for {email}"))
        for email in report["forbidden"]:
            self.stdout.write(self.style.WARNING(f"Not allowed to read the calendar of {email}"))
