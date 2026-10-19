"""Every report the Reports page offers, and who may run each.

A report declares the capabilities it needs and ``visible_reports()`` drops the
ones the reader lacks - the same shape as the dashboard card registry. Nothing
here, and nothing in a builder, asks what role somebody holds: an ACM sees only
the accounts and contacts sheets because those are the only two that need no
more than ``export.run``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from csm.core.access import capabilities as cap
from csm.reports import builders


@dataclass(frozen=True)
class Option:
    """A choice a report offers beyond its period, from a closed list."""

    name: str
    label: str
    choices: tuple[tuple[str, str], ...]
    default: str

    def clean(self, value: str | None) -> str:
        """The submitted value, or the default when none was sent.

        An unknown value raises rather than falling back: a file built for a
        choice the reader did not make looks right and is not.
        """
        if not value:
            return self.default
        if value not in dict(self.choices):
            raise ValueError(f"Choose what to show: {self.label.lower()}.")
        return value


@dataclass(frozen=True)
class Report:
    key: str
    title: str
    description: str
    icon: str
    capabilities: tuple[str, ...]
    #: The preset the form opens on. None means the report is a snapshot of
    #: what exists now and takes no period.
    default_period: str | None
    build: Callable | None = None
    #: The sheets in the file, in order, as the page describes them.
    contents: tuple[str, ...] = ()
    options: tuple[Option, ...] = ()
    #: Reports sharing a tab sit on one page of the tabs. Defaults to the key.
    tab: str = ""

    @property
    def tab_key(self) -> str:
        return self.tab or self.key


def _accounts_sheet(ctx):
    from csm.importer import export
    from csm.importer.parsing import active_custom_fields

    return export.build_accounts_xlsx(
        ctx.ability.accounts(),
        include_health=ctx.shows_health,
        custom_fields=active_custom_fields(),
    )


def _contacts_sheet(ctx):
    from csm.contacts.models import Contact
    from csm.importer import export

    return export.build_contacts_xlsx(Contact.objects.filter(account__in=ctx.ability.accounts()))


REPORTS: tuple[Report, ...] = (
    Report(
        key="work",
        title="Tasks & activities",
        description=(
            "What was done and what is coming, in one file. Completed rows are "
            "the activity record - meetings, calls, Teams attendance - with the "
            "task each one closed and how late it was. Upcoming rows are open "
            "and overdue tasks, plus the renewal and quarterly reminders that "
            "will be created on the way, marked Projected."
        ),
        icon="bi-list-check",
        capabilities=(cap.REPORT_VIEW,),
        default_period="this_month",
        build=builders.work,
        contents=(
            "Summary by CSM - completed, late, meetings and client contacts; "
            "overdue, open and projected",
            "Tasks and activities - one row per item, with its status",
            "About - the period, who ran it and when",
        ),
        options=(
            Option(
                name="show",
                label="Show",
                choices=builders.SHOW_CHOICES,
                default=builders.SHOW_ALL,
            ),
        ),
    ),
    Report(
        key="health",
        title="Health scores",
        description=(
            "How each account's health moved over the period. The starting point "
            "is the last score before the period began, so a recovery on the "
            "2nd of the month shows as a change."
        ),
        icon="bi-heart-pulse",
        capabilities=(cap.REPORT_VIEW, cap.HEALTH_VIEW),
        default_period="last_month",
        build=builders.health,
        contents=(
            "Summary by account - status and score at the start and the end, "
            "lowest, highest, number of changes, and the record behind the score at the end",
            "Status changes - each move between Healthy, Watch and At Risk",
            "Every calculation - the record each factor was scored on (open tickets, last adoption event, CSAT average, last contact, meetings held), then its colour and points",
            "Overrides - set or cleared in the period, with the reason",
            "About",
        ),
    ),
    Report(
        key="renewals",
        title="Renewals",
        description=(
            "Contracts ending in the period, soonest first, with their owners "
            "and any renewal reminder still open."
        ),
        icon="bi-arrow-repeat",
        capabilities=(cap.REPORT_VIEW,),
        default_period="next_90",
        build=builders.renewals,
        contents=(
            "Renewals - contract end, days left, class, stage, CSM and ACM, "
            "open reminders, health",
            "About",
        ),
    ),
    Report(
        key="portfolio",
        title="Portfolio by CSM",
        description=(
            "The analytics page as numbers: one line per CSM with the accounts "
            "they carry and the work done in the period."
        ),
        icon="bi-people",
        capabilities=(cap.REPORT_VIEW,),
        default_period="this_month",
        build=builders.portfolio,
        contents=(
            "By CSM - accounts, health today, tasks done and late, overdue now, "
            "client contacts, meetings, renewals in the period",
            "About",
        ),
    ),
    Report(
        key="accounts",
        title="Accounts",
        description=(
            "Every account you can see, as it stands today, in the import "
            "sheet's columns - so an edited copy imports straight back."
        ),
        icon="bi-building",
        capabilities=(cap.EXPORT_RUN,),
        default_period=None,
        build=_accounts_sheet,
        tab="data",
    ),
    Report(
        key="contacts",
        title="Contacts",
        description=(
            "Every contact on those accounts, as they stand today, in the "
            "contacts import sheet's columns."
        ),
        icon="bi-person-lines-fill",
        capabilities=(cap.EXPORT_RUN,),
        default_period=None,
        build=_contacts_sheet,
        tab="data",
    ),
)

#: How a tab holding several reports is named in the tab strip.
TAB_TITLES = {"data": ("Accounts & contacts", "bi-database")}

_BY_KEY = {report.key: report for report in REPORTS}


def visible_reports(ability) -> list[Report]:
    return [report for report in REPORTS if ability.can_all(report.capabilities)]


def get(key: str) -> Report | None:
    return _BY_KEY.get(key)
