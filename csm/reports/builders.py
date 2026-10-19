"""What each report contains.

A builder receives a ``ReportContext`` - the reader's ``Ability``, the period and
the business date - and returns sheets. It never starts a query from
``Account.objects``, ``Task.objects`` or ``Activity.objects`` unscoped: accounts
come from ``ability.accounts()``, tasks from ``ability.visible_tasks()`` and
activities through ``ability.visible_activities()``. Health columns appear only
for a reader who holds ``health.view``, whatever the report.

Every range is a span of business (Cairo) calendar days, inclusive at both ends.
A stored timestamp is compared against the UTC instants at which those days
begin, through ``core/clock`` - never against a bare date, which would count a
task finished at 01:00 Cairo time on the 1st as the previous month's.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import Count, OuterRef, Q, Subquery

from csm.core import clock
from csm.core.access import capabilities as cap
from csm.core.enums import (
    ActivityType,
    FactorId,
    HealthStatus,
    OverrideClearedReason,
    ScoreTrigger,
    TaskCategory,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from csm.reports.periods import Period
from csm.reports.workbook import Sheet

NO_CSM = "(No CSM)"


@dataclass(frozen=True)
class ReportContext:
    ability: object
    period: Period | None
    today: date
    generated_at: datetime
    #: The report's own choices beyond the period, already checked against the
    #: registry's list - e.g. ``{"show": "completed"}``.
    options: dict = field(default_factory=dict)

    @property
    def shows_health(self) -> bool:
        return self.ability.can(cap.HEALTH_VIEW)

    @property
    def shows_notes(self) -> bool:
        return self.ability.can(cap.NOTE_VIEW)

    def instants(self) -> tuple[datetime, datetime]:
        """The UTC half-open interval covering the period's business days."""
        return (
            clock.start_of_business_day(self.period.start),
            clock.start_of_business_day(self.period.end + timedelta(days=1)),
        )


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def _name(user) -> str:
    if user is None:
        return ""
    return user.full_name or user.email


def _label(choices, value) -> str:
    if not value:
        return ""
    try:
        return choices(value).label
    except ValueError:
        return str(value)


def _yes(flag: bool) -> str:
    return "Yes" if flag else "No"


def _number(value):
    """A stored score or points value as something Excel sums."""
    if value is None or value == "":
        return None
    return float(Decimal(str(value)))


def _assignees(task) -> str:
    return ", ".join(sorted(_name(a.user) for a in task.assignments.all()))


def _is_automatic(task_type: str) -> bool:
    return task_type in (
        TaskType.AUTO_RENEWAL,
        TaskType.AUTO_QUARTERLY_MEETING,
        TaskType.AUTO_HEALTH_ALERT,
    )


# ---------------------------------------------------------------------------
# Tasks and activities
# ---------------------------------------------------------------------------

#: What the merged report may show. Completed is what happened - the activity
#: log - and upcoming is what is planned: open tasks and the reminders the
#: generators will create.
SHOW_ALL = "all"
SHOW_COMPLETED = "completed"
SHOW_UPCOMING = "upcoming"
SHOW_CHOICES = (
    (SHOW_ALL, "Completed and upcoming"),
    (SHOW_COMPLETED, "Completed only"),
    (SHOW_UPCOMING, "Upcoming only"),
)

COMPLETED = "Completed"
OPEN = "Open"
OVERDUE = "Overdue"
PROJECTED = "Projected"


def work(ctx: ReportContext) -> list[Sheet]:
    """Tasks, meetings and calls in the period - done, planned, or both.

    One row per thing. A completed task already wrote its activity record, so a
    completed row *is* that record, carrying the task it closed (title, due date,
    how late). Listing the task and its record separately would count every
    finished meeting twice. A record with no task - a Teams meeting the sync
    logged - is a row of its own, and so is a task marked done whose record is
    missing, which predates completion writing one. A voided record is left out:
    voiding says it did not happen.
    """
    show = ctx.options.get("show", SHOW_ALL)
    entries: list[tuple] = []
    if show in (SHOW_ALL, SHOW_COMPLETED):
        entries += _completed(ctx)
    if show in (SHOW_ALL, SHOW_UPCOMING):
        entries += _upcoming(ctx)
    entries.sort(key=lambda entry: (entry[0], entry[1]))

    headers = [
        "Date",
        "Status",
        "Client code",
        "Account",
        "Title",
        "Kind",
        "Client contact",
        "Category",
        "Priority",
        "Contact",
        "Due date",
        "Days late",
        "Source",
        "Done by",
        "Assigned to",
        "CSM",
        "Automatic",
    ]
    if ctx.shows_notes:
        headers.append("Notes")
    rows = [row if ctx.shows_notes else row[:-1] for _, _, row in entries]

    return [_work_summary(rows, headers, show), Sheet("Tasks and activities", headers, rows)]


def _row(**values) -> list:
    order = (
        "date", "status", "code", "account", "title", "kind", "contact_flag",
        "category", "priority", "contact", "due", "late", "source", "done_by",
        "assigned", "csm", "automatic", "notes",
    )
    return [values.get(key) for key in order]


def _completed(ctx: ReportContext) -> list[tuple]:
    from csm.activities.models import Activity

    period = ctx.period
    entries = []
    logged = ctx.ability.visible_activities(
        Activity.objects.live().filter(
            account__in=ctx.ability.accounts(),
            occurred_on__gte=period.start,
            occurred_on__lte=period.end,
        )
    ).select_related("account", "account__csm", "contact", "created_by", "task")

    for activity in logged:
        account, task = activity.account, activity.task
        late = max(0, (activity.occurred_on - task.due_date).days) if task else None
        entries.append(
            (
                activity.occurred_on,
                account.client_name,
                _row(
                    date=activity.occurred_on,
                    status=COMPLETED,
                    code=account.client_code,
                    account=account.client_name,
                    title=(task.title if task else "") or activity.subject,
                    kind=activity.get_activity_type_display(),
                    # A note is internal: it counts for neither contact factor.
                    contact_flag=_yes(activity.activity_type != ActivityType.NOTE),
                    category=_label(TaskCategory, task.category) if task else "",
                    priority=_label(TaskPriority, task.priority) if task else "",
                    contact=activity.contact.full_name if activity.contact else "",
                    due=task.due_date if task else None,
                    late=late,
                    source=activity.get_source_display(),
                    done_by=_name(activity.created_by),
                    csm=_name(account.csm) or NO_CSM,
                    automatic=_yes(bool(task) and _is_automatic(task.task_type)),
                    notes=activity.notes,
                ),
            )
        )

    start, end = ctx.instants()
    unrecorded = (
        ctx.ability.visible_tasks()
        .filter(
            status=TaskStatus.DONE,
            completed_at__gte=start,
            completed_at__lt=end,
            logged_activity__isnull=True,
        )
        .select_related("account", "account__csm", "contact", "completed_by")
    )
    for task in unrecorded:
        account = task.account
        completed_on = clock.to_business_date(task.completed_at)
        entries.append(
            (
                completed_on,
                account.client_name,
                _row(
                    date=completed_on,
                    status=COMPLETED,
                    code=account.client_code,
                    account=account.client_name,
                    title=task.title,
                    kind=_label(TaskType, task.task_type),
                    contact_flag="",
                    category=_label(TaskCategory, task.category),
                    priority=_label(TaskPriority, task.priority),
                    contact=task.contact.full_name if task.contact else "",
                    due=task.due_date,
                    late=max(0, (completed_on - task.due_date).days),
                    source="Platform",
                    done_by=_name(task.completed_by),
                    csm=_name(account.csm) or NO_CSM,
                    automatic=_yes(_is_automatic(task.task_type)),
                    notes="",
                ),
            )
        )
    return entries


def _upcoming(ctx: ReportContext) -> list[tuple]:
    """Open tasks due in the period, plus the automatic ones not created yet.

    The generators create a renewal or quarterly task only on the day it falls
    due, so the projection is what lets a forward range show next quarter's
    reminders at all. A period in the past shows what is still open from it -
    which is everything overdue.
    """
    from csm.tasks.generators import project_auto_tasks

    period = ctx.period
    now_time = clock.local_time()
    entries = []
    tasks = (
        ctx.ability.visible_tasks()
        .open()
        .filter(due_date__gte=period.start, due_date__lte=period.end)
        .select_related("account", "account__csm", "contact")
        .prefetch_related("assignments__user")
    )
    for task in tasks:
        account = task.account
        entries.append(
            (
                task.due_date,
                account.client_name,
                _row(
                    date=task.due_date,
                    status=OVERDUE if task.is_overdue(ctx.today, now_time) else OPEN,
                    code=account.client_code,
                    account=account.client_name,
                    title=task.title,
                    kind=_label(TaskType, task.task_type),
                    contact_flag="",
                    category=_label(TaskCategory, task.category),
                    priority=_label(TaskPriority, task.priority),
                    contact=task.contact.full_name if task.contact else "",
                    due=task.due_date,
                    source="Platform",
                    assigned=_assignees(task),
                    csm=_name(account.csm) or NO_CSM,
                    automatic=_yes(_is_automatic(task.task_type)),
                    notes=task.description,
                ),
            )
        )

    accounts = ctx.ability.accounts().select_related("csm", "acm")
    for projected in project_auto_tasks(accounts, period.start, period.end, as_of=ctx.today):
        account = projected.account
        # Renewal and quarterly tasks route to the account's CSM and ACM.
        people = ", ".join(sorted(n for n in (_name(account.csm), _name(account.acm)) if n))
        entries.append(
            (
                projected.due_date,
                account.client_name,
                _row(
                    date=projected.due_date,
                    status=PROJECTED,
                    code=account.client_code,
                    account=account.client_name,
                    title=projected.title,
                    kind=_label(TaskType, projected.task_type),
                    contact_flag="",
                    category=_label(TaskCategory, TaskCategory.CLIENT_FACING),
                    due=projected.due_date,
                    source="Projected",
                    assigned=people,
                    csm=_name(account.csm) or NO_CSM,
                    automatic="Yes",
                    notes="",
                ),
            )
        )
    return entries


def _work_summary(rows: list, headers: list, show: str) -> Sheet:
    """One line per CSM, with only the columns the chosen view can fill."""
    at = {name: headers.index(name) for name in ("Status", "Kind", "Client contact", "Days late", "CSM")}
    tally: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        counts = tally[row[at["CSM"]]]
        counts[row[at["Status"]]] += 1
        if row[at["Status"]] == COMPLETED:
            if row[at["Days late"]]:
                counts["late"] += 1
            if row[at["Kind"]] == ActivityType.MEETING.label:
                counts["meetings"] += 1
            if row[at["Client contact"]] == "Yes":
                counts["contact"] += 1

    columns = ["CSM"]
    if show in (SHOW_ALL, SHOW_COMPLETED):
        columns += ["Completed", "Completed late", "Meetings held", "Client contacts"]
    if show in (SHOW_ALL, SHOW_UPCOMING):
        columns += ["Overdue", "Open", "Projected"]
    keys = {
        "Completed": COMPLETED,
        "Completed late": "late",
        "Meetings held": "meetings",
        "Client contacts": "contact",
        "Overdue": OVERDUE,
        "Open": OPEN,
        "Projected": PROJECTED,
    }
    summary = [
        [csm] + [tally[csm][keys[column]] for column in columns[1:]] for csm in sorted(tally)
    ]
    return Sheet("Summary by CSM", columns, summary)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def _as_date(value):
    return date.fromisoformat(value) if value else None


#: The record each factor was scored on, as the engine stored it in the history
#: row's ``breakdown``: (detail key, column heading, conversion). Points alone
#: say 100, 50 or 0; these say why - three open bugs, last contact 12 days ago,
#: a CSAT of 3.40 from two people. Read from the stored row, never recomputed,
#: so the file shows exactly what that calculation saw.
FACTOR_RECORD: dict[str, tuple[tuple[str, str, object | None], ...]] = {
    FactorId.OPEN_TICKETS.value: (
        ("bugs", "Open bug tickets", None),
        ("general", "Open general tickets", None),
        ("weighted", "Weighted open tickets", None),
    ),
    FactorId.MONTHLY_ADOPTION.value: (
        ("last_event_on", "Last adoption event", _as_date),
        ("days_since", "Days since adoption event", None),
    ),
    FactorId.CSAT.value: (
        ("average", "CSAT average", _number),
        ("quarter", "CSAT quarter", str),
        ("contacts", "CSAT contacts", None),
        ("ratings", "CSAT ratings", None),
        ("recorded_on", "CSAT last rated", _as_date),
    ),
    FactorId.GENERAL_CONTACT.value: (
        ("last_contact_on", "Last client contact", _as_date),
        ("days_since", "Days since client contact", None),
    ),
    FactorId.WEEKLY_CONTACT.value: (
        ("last_contact_on", "Last non-meeting contact", _as_date),
        ("days_since", "Days since non-meeting contact", None),
    ),
    FactorId.MEETING_ATTENDANCE.value: (
        ("meetings_in_window", "Meetings this month", None),
        ("required_per_month", "Meetings required a month", None),
        ("compliance_pct", "Meeting compliance %", _number),
    ),
}

#: Why a factor was scored the way it was when the record alone does not say.
FACTOR_NOTES = {
    "csat_missing": "No rating yet - scored 50",
    "csat_stale": "Latest rating too old - scored 50",
    "required_meeting_frequency_not_set": "Not scored - no meeting frequency set",
}


def _factor_headers() -> list[str]:
    headers = []
    for factor in FactorId:
        headers += [heading for _, heading, _ in FACTOR_RECORD.get(factor.value, ())]
        headers += [f"{factor.label} colour", f"{factor.label} points", f"{factor.label} note"]
    return headers


def _factor_cells(breakdown) -> list:
    """One calculation's factors, in ``_factor_headers()`` order.

    A factor the account's factor set does not include (General Contact on a
    managed account, Weekly Contact on any other) is left blank throughout
    rather than shown as zero - zero open tickets is a record, an absent factor
    is not.
    """
    factors = (breakdown or {}).get("factors") or {}
    cells = []
    for factor in FactorId:
        data = factors.get(factor.value)
        record = FACTOR_RECORD.get(factor.value, ())
        if not data:
            cells += [None] * (len(record) + 3)
            continue
        detail = data.get("detail") or {}
        for key, _, convert in record:
            value = detail.get(key)
            cells.append(convert(value) if convert and value is not None else value)
        scored = data.get("outcome") == "SCORED"
        cells.append((data.get("color") or "").title() if scored else "Not scored")
        cells.append(_number(data.get("points")) if scored else None)
        cells.append(FACTOR_NOTES.get(data.get("reason") or "", data.get("reason") or ""))
    return cells


def health(ctx: ReportContext) -> list[Sheet]:
    """How each account's health moved over the period.

    The opening position is the last calculation *before* the period began, so
    an account that was At Risk on the 31st and recovered on the 2nd shows the
    change rather than starting the month already green.
    """
    from csm.health.models import HealthScoreHistory, HealthStatusOverride

    period = ctx.period
    accounts = ctx.ability.accounts()

    before = HealthScoreHistory.objects.filter(
        account=OuterRef("pk"), calculated_on__lt=period.start
    ).order_by("-calculated_at", "-pk")
    opening = {
        row["pk"]: row
        for row in accounts.annotate(
            opening_status=Subquery(before.values("effective_status")[:1]),
            opening_score=Subquery(before.values("score")[:1]),
        ).values("pk", "opening_status", "opening_score")
    }

    history = (
        HealthScoreHistory.objects.filter(
            account__in=accounts,
            calculated_on__gte=period.start,
            calculated_on__lte=period.end,
        )
        .select_related("account", "account__csm")
        .order_by("account__client_name", "account_id", "calculated_at", "pk")
    )

    calculations = []
    changes = []
    per_account: dict[int, dict] = {}

    for row in history:
        account = row.account
        state = per_account.get(account.pk)
        if state is None:
            first = opening.get(account.pk, {})
            state = per_account[account.pk] = {
                "account": account,
                "opening_status": first.get("opening_status"),
                "opening_score": first.get("opening_score"),
                "previous": first.get("opening_status"),
                "scores": [],
                "changes": 0,
            }
        if row.score is not None:
            state["scores"].append(row.score)
        state["closing_status"] = row.effective_status
        state["closing_score"] = row.score

        if state["previous"] is not None and row.effective_status != state["previous"]:
            state["changes"] += 1
            changes.append(
                [
                    row.calculated_on,
                    account.client_code,
                    account.client_name,
                    _label(HealthStatus, state["previous"]),
                    _label(HealthStatus, row.effective_status),
                    _number(row.score),
                    row.max_score,
                    _label(ScoreTrigger, row.trigger),
                    _yes(row.override_active),
                    _name(account.csm) or NO_CSM,
                ]
            )
        state["previous"] = row.effective_status

        factor_cells = _factor_cells(row.breakdown)
        state["closing_factors"] = factor_cells

        calculations.append(
            [
                row.calculated_on,
                row.calculated_at,
                account.client_code,
                account.client_name,
                _label(ScoreTrigger, row.trigger),
                _number(row.score),
                row.max_score,
                _label(HealthStatus, row.computed_status),
                _label(HealthStatus, row.effective_status),
                _yes(row.override_active),
                row.engine_version,
            ]
            + factor_cells
        )

    summary = []
    for state in sorted(per_account.values(), key=lambda s: s["account"].client_name):
        account = state["account"]
        opening_score = state["opening_score"]
        closing_score = state["closing_score"]
        change = (
            _number(closing_score - opening_score)
            if opening_score is not None and closing_score is not None
            else None
        )
        summary.append(
            [
                account.client_code,
                account.client_name,
                _name(account.csm) or NO_CSM,
                _label(HealthStatus, state["opening_status"]),
                _number(opening_score),
                _label(HealthStatus, state["closing_status"]),
                _number(closing_score),
                change,
                _number(min(state["scores"])) if state["scores"] else None,
                _number(max(state["scores"])) if state["scores"] else None,
                state["changes"],
            ]
            + state["closing_factors"]
        )

    start, end = ctx.instants()
    overrides = (
        HealthStatusOverride.objects.filter(account__in=accounts)
        .filter(
            Q(created_at__gte=start, created_at__lt=end)
            | Q(cleared_at__gte=start, cleared_at__lt=end)
        )
        .select_related("account", "created_by", "cleared_by")
        .order_by("created_at", "pk")
    )
    override_rows = [
        [
            o.created_at,
            o.account.client_code,
            o.account.client_name,
            _label(HealthStatus, o.status),
            _number(o.score_at_override),
            o.max_at_override,
            o.reason,
            _name(o.created_by),
            o.cleared_at,
            _label(OverrideClearedReason, o.cleared_reason),
            _name(o.cleared_by),
        ]
        for o in overrides
    ]

    factor_headers = _factor_headers()

    return [
        Sheet(
            "Summary by account",
            [
                "Client code",
                "Account",
                "CSM",
                "Status at start",
                "Score at start",
                "Status at end",
                "Score at end",
                "Change",
                "Lowest score",
                "Highest score",
                "Status changes",
            ]
            # The record behind the score at the end of the period.
            + factor_headers,
            summary,
        ),
        Sheet(
            "Status changes",
            [
                "Date",
                "Client code",
                "Account",
                "From",
                "To",
                "Score",
                "Out of",
                "Trigger",
                "Override",
                "CSM",
            ],
            changes,
        ),
        Sheet(
            "Every calculation",
            [
                "Date",
                "Calculated at",
                "Client code",
                "Account",
                "Trigger",
                "Score",
                "Out of",
                "Computed status",
                "Effective status",
                "Override",
                "Engine version",
            ]
            + factor_headers,
            calculations,
        ),
        Sheet(
            "Overrides",
            [
                "Set at",
                "Client code",
                "Account",
                "Status set",
                "Score at override",
                "Out of",
                "Reason",
                "Set by",
                "Cleared at",
                "Cleared because",
                "Cleared by",
            ],
            override_rows,
        ),
    ]


# ---------------------------------------------------------------------------
# Renewals
# ---------------------------------------------------------------------------


def renewals(ctx: ReportContext) -> list[Sheet]:
    """Contracts ending in the period, soonest first."""
    period = ctx.period
    accounts = (
        ctx.ability.accounts()
        .filter(contract_end_date__gte=period.start, contract_end_date__lte=period.end)
        .select_related("csm", "acm")
        .annotate(
            open_reminders=Count(
                "tasks",
                filter=Q(tasks__task_type=TaskType.AUTO_RENEWAL, tasks__status=TaskStatus.PENDING),
            )
        )
        .order_by("contract_end_date", "client_name")
    )

    headers = [
        "Contract end",
        "Days left",
        "Client code",
        "Account",
        "Class",
        "Lifecycle stage",
        "License",
        "CSM",
        "ACM",
        "Open renewal reminders",
    ]
    if ctx.shows_health:
        headers += ["Health status", "Health score"]

    rows = []
    for account in accounts:
        row = [
            account.contract_end_date,
            (account.contract_end_date - ctx.today).days,
            account.client_code,
            account.client_name,
            account.get_account_class_display(),
            account.get_lifecycle_stage_display(),
            account.get_license_type_display(),
            _name(account.csm) or NO_CSM,
            _name(account.acm),
            account.open_reminders,
        ]
        if ctx.shows_health:
            row += [
                _label(HealthStatus, account.effective_health_status),
                _number(account.computed_health_score),
            ]
        rows.append(row)

    return [Sheet("Renewals", headers, rows)]


# ---------------------------------------------------------------------------
# Portfolio by CSM
# ---------------------------------------------------------------------------


def portfolio(ctx: ReportContext) -> list[Sheet]:
    """One line per CSM: the book they carry and the work done in the period.

    The analytics page as numbers. Health counts are the status today, not at
    the end of the period - the health report is where the history lives.
    """
    from csm.activities.models import Activity

    period = ctx.period
    start, end = ctx.instants()
    accounts = ctx.ability.accounts().select_related("csm")

    book: dict[str, Counter] = defaultdict(Counter)
    for account in accounts:
        tally = book[_name(account.csm) or NO_CSM]
        tally["accounts"] += 1
        tally[account.effective_health_status or ""] += 1
        if account.contract_end_date and period.start <= account.contract_end_date <= period.end:
            tally["renewals"] += 1

    tasks = ctx.ability.visible_tasks().select_related("account__csm")
    for task in tasks.filter(
        status=TaskStatus.DONE, completed_at__gte=start, completed_at__lt=end
    ):
        tally = book[_name(task.account.csm) or NO_CSM]
        tally["done"] += 1
        if clock.to_business_date(task.completed_at) > task.due_date:
            tally["late"] += 1
    for task in tasks.overdue(ctx.today, clock.local_time()):
        book[_name(task.account.csm) or NO_CSM]["overdue"] += 1

    logged = ctx.ability.visible_activities(
        Activity.objects.live().filter(
            account__in=accounts,
            occurred_on__gte=period.start,
            occurred_on__lte=period.end,
        )
    ).select_related("account__csm")
    for activity in logged:
        tally = book[_name(activity.account.csm) or NO_CSM]
        if activity.activity_type != ActivityType.NOTE:
            tally["contact"] += 1
        if activity.activity_type == ActivityType.MEETING:
            tally["meetings"] += 1

    headers = ["CSM", "Accounts"]
    if ctx.shows_health:
        headers += ["Healthy", "Watch", "At Risk", "Not scored"]
    headers += [
        "Tasks completed",
        "Completed late",
        "Overdue now",
        "Client contacts logged",
        "Meetings held",
        "Renewals in period",
    ]

    rows = []
    for csm in sorted(book):
        tally = book[csm]
        row = [csm, tally["accounts"]]
        if ctx.shows_health:
            row += [
                tally[HealthStatus.HEALTHY.value],
                tally[HealthStatus.WATCH.value],
                tally[HealthStatus.AT_RISK.value],
                tally[HealthStatus.NOT_APPLICABLE.value] + tally[""],
            ]
        row += [
            tally["done"],
            tally["late"],
            tally["overdue"],
            tally["contact"],
            tally["meetings"],
            tally["renewals"],
        ]
        rows.append(row)

    return [Sheet("By CSM", headers, rows)]
