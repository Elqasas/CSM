"""Pull Jira Service Management issues into tickets.

Runs every fifteen minutes from the schedule. Safe to re-run: every write is an
upsert keyed on the issue and the account. Exits quietly when Jira is not
configured, so the schedule can be registered before the credentials exist.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from csm.integrations.jira import settings as jira
from csm.integrations.jira.client import JiraError
from csm.integrations.jira.sync import run


class Command(BaseCommand):
    help = "Sync tickets from Jira Service Management. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument(
            "--full",
            action="store_true",
            help="Also re-read every open issue and close tickets Jira no longer has open.",
        )

    def handle(self, *args, **options):
        if not jira.is_configured():
            self.stdout.write(self.style.WARNING("Jira is not configured; nothing to sync."))
            return

        try:
            report = run(jira.get_client(), full=options["full"])
        except JiraError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Read {report['read']} issues: {report['created']} created, "
                f"{report['updated']} updated, {report['removed']} removed"
                + (f", {report['closed_by_sweep']} closed by sweep" if report["swept"] else "")
                + "."
            )
        )
        for label, key in (("No account named", "unmatched_organizations"),
                           ("More than one account named", "ambiguous_organizations")):
            for name, count in report[key].items():
                self.stdout.write(self.style.WARNING(f"{label} {name!r} ({count} issues)"))
