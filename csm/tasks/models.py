"""Tasks and their assignment.

Two database-level invariants carry business rules that an application check
could not hold: ``auto_key`` makes every generator safely re-runnable, and the
partial unique index on open health alerts makes "one alert per account"
unfalsifiable even when the daily job and an event-driven recalculation race.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from django.conf import settings
from django.db import models

from csm.accounts.models import Account
from csm.contacts.models import Contact
from csm.core.enums import (
    DEFAULT_TASK_PRIORITY,
    OPEN_TASK_STATUSES,
    RepeatEnd,
    RepeatFrequency,
    TaskAssignmentRole,
    TaskCategory,
    TaskCloseReason,
    TaskPriority,
    TaskProgress,
    TaskStatus,
    TaskType,
)


class TaskQuerySet(models.QuerySet):
    def open(self) -> TaskQuerySet:
        return self.filter(status__in=list(OPEN_TASK_STATUSES))

    def overdue(self, as_of, as_of_time) -> TaskQuerySet:
        """Past its date, or past its time on the day it is due.

        The time is required rather than optional. It used to be absent
        entirely, which made every counter on every page date-only: a task set
        for 09:00 still read as "due today" at six in the evening, and the
        person chasing it had no way to see that it had slipped. An optional
        argument would have left that behaviour one forgotten call site away.

        This is the SQL half of ``Task.is_overdue``. The two are expressible
        separately and so can drift; ``tests/test_task_routing.py`` asserts they
        agree across the boundary cases rather than trusting that they do.
        """
        return self.open().filter(
            models.Q(due_date__lt=as_of)
            | models.Q(due_date=as_of, due_time__isnull=False, due_time__lt=as_of_time)
        )

    def for_user(self, user) -> TaskQuerySet:
        return self.filter(assignments__user=user).distinct()

    def unassigned(self) -> TaskQuerySet:
        return self.open().filter(assignments__isnull=True)


class TaskSeries(models.Model):
    """The rule behind a repeating task, and the template it stamps out.

    A repeat is not a hundred rows written in advance. "Never" has no end to
    write up to, a year of a daily task is three hundred and sixty-five entries
    nobody asked to see, and every one of them would need rewriting the moment
    somebody edited the wording.

    So: one rule, and **one open occurrence at a time**. Finishing the current
    one creates the next; the daily generator tops a series up if its occurrence
    was cancelled, so a series cannot silently die. A repeating task you have
    not done therefore sits there overdue rather than breeding copies of itself,
    which is the behaviour people expect and the one that keeps the queue
    honest.
    """

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="task_series")
    contact = models.ForeignKey(
        Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="task_series"
    )

    # The template. Deliberately a copy rather than a pointer at the first task:
    # editing one occurrence should not silently rewrite every future one.
    task_type = models.CharField(max_length=24, choices=TaskType.choices)
    category = models.CharField(
        max_length=16, choices=TaskCategory.choices, default=TaskCategory.CLIENT_FACING
    )
    priority = models.CharField(
        max_length=8, choices=TaskPriority.choices, default=DEFAULT_TASK_PRIORITY
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    due_time = models.TimeField(null=True, blank=True)

    frequency = models.CharField(max_length=12, choices=RepeatFrequency.choices)
    ends = models.CharField(
        max_length=8, choices=RepeatEnd.choices, default=RepeatEnd.NEVER
    )
    #: How many occurrences in total, when it ends after a number of times.
    occurrences = models.PositiveSmallIntegerField(null=True, blank=True)
    #: The last date an occurrence may fall on, when it ends on a date.
    until = models.DateField(null=True, blank=True)

    #: How many have been stamped out so far. Counted rather than derived, so
    #: deleting an occurrence does not quietly buy the series another one.
    created_count = models.PositiveSmallIntegerField(default=0)

    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="task_series",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "task_series"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~models.Q(ends=RepeatEnd.AFTER) | models.Q(occurrences__gte=1)
                ),
                name="task_series_after_needs_a_count",
            ),
            models.CheckConstraint(
                condition=~models.Q(ends=RepeatEnd.ON) | models.Q(until__isnull=False),
                name="task_series_on_needs_a_date",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.get_frequency_display().lower()})"

    def next_due(self, after: date):
        """The date after this one, or None when the series has run out.

        Months and years step by calendar rather than by a fixed number of days,
        so a monthly task on the 31st lands on the last day of a short month
        instead of wandering into the next one.
        """
        if not self.is_active:
            return None
        if self.ends == RepeatEnd.AFTER and self.created_count >= (self.occurrences or 0):
            return None

        nxt = _step(after, self.frequency)
        if self.ends == RepeatEnd.ON and self.until and nxt > self.until:
            return None
        return nxt

    @property
    def summary(self) -> str:
        """One line, for a badge beside an occurrence."""
        every = self.get_frequency_display()
        if self.ends == RepeatEnd.AFTER:
            return f"{every}, {self.occurrences} times"
        if self.ends == RepeatEnd.ON and self.until:
            return f"{every} until {self.until:%d %b %Y}"
        return every


def _step(day: date, frequency: str) -> date:
    """One step forward. Calendar months, not thirty-day months."""
    if frequency == RepeatFrequency.DAILY:
        return day + timedelta(days=1)
    if frequency == RepeatFrequency.WEEKLY:
        return day + timedelta(weeks=1)
    if frequency == RepeatFrequency.MONTHLY:
        return _add_months(day, 1)
    if frequency == RepeatFrequency.QUARTERLY:
        return _add_months(day, 3)
    return _add_months(day, 12)


def _add_months(day: date, months: int) -> date:
    """Add whole months, clamped to the end of a short one.

    31 January plus a month is 28 February, not 3 March: a monthly task should
    stay at the end of the month rather than drift a few days forward every
    February.
    """
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


class Task(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="tasks")
    series = models.ForeignKey(
        "tasks.TaskSeries",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="occurrences_made",
        help_text="The repeat rule this occurrence came from, if any.",
    )
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
        help_text="Optional, but shown prominently in the form to encourage use.",
    )

    task_type = models.CharField(max_length=24, choices=TaskType.choices)
    category = models.CharField(
        max_length=16, choices=TaskCategory.choices, default=TaskCategory.INTERNAL
    )

    progress = models.CharField(
        max_length=16,
        choices=TaskProgress.choices,
        default=TaskProgress.NOT_STARTED,
        db_index=True,
        help_text="Where the work has got to. Set by hand; completing the task sets it.",
    )

    priority = models.CharField(
        max_length=8,
        choices=TaskPriority.choices,
        default=DEFAULT_TASK_PRIORITY,
        db_index=True,
        help_text="How much this matters, as distinct from when it is due.",
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    due_date = models.DateField(db_index=True)
    #: Optional clock time on the due date, for a task that has to happen at a
    #: moment rather than on a day. Kept beside the date rather than folded into
    #: a DateTimeField: the generators identify their tasks by a business date,
    #: overdue is a day-count question, and the digest groups by day - making
    #: the column a timestamp would drag a timezone into all three for the sake
    #: of the minority of tasks that need a time at all.
    due_time = models.TimeField(
        null=True,
        blank=True,
        help_text="Optional. Leave empty for a task due any time that day.",
    )

    status = models.CharField(
        max_length=12, choices=TaskStatus.choices, default=TaskStatus.PENDING
    )
    close_reason = models.CharField(
        max_length=32, choices=TaskCloseReason.choices, null=True, blank=True
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    jira_ticket_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Reserved and empty. Only Internal-category tasks become Jira tickets later.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_tasks",
        help_text="Null means the task was generated by the system.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    auto_key = models.CharField(
        max_length=160,
        null=True,
        blank=True,
        unique=True,
        help_text=(
            "Idempotency key for generated tasks. Contains no timestamp, so "
            "re-running a job creates nothing; contains the business date, so "
            "moving a contract date correctly creates a new obligation."
        ),
    )

    objects = TaskQuerySet.as_manager()

    class Meta:
        db_table = "tasks"
        ordering = ["due_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(status=TaskStatus.DONE, completed_at__isnull=False)
                    | ~models.Q(status=TaskStatus.DONE) & models.Q(completed_at__isnull=True)
                ),
                name="tasks_completed_at_matches_status",
            ),
            # One open health alert per account. A select-then-insert check would
            # leave a window in which two callers both find nothing and both
            # insert; this cannot.
            models.UniqueConstraint(
                fields=["account"],
                condition=models.Q(
                    task_type=TaskType.AUTO_HEALTH_ALERT, status=TaskStatus.PENDING
                ),
                name="tasks_one_open_health_alert_per_account",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "due_date"]),
            models.Index(fields=["account", "status"]),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_TASK_STATUSES

    @property
    def is_system_generated(self) -> bool:
        return self.auto_key is not None

    @property
    def due_display(self) -> str:
        """The due date, with the time only when there is one.

        The day is interpolated rather than formatted: ``%-d`` strips the
        leading zero on Linux and raises ValueError on Windows, and this project
        is developed on one and deployed on the other.
        """
        day = f"{self.due_date.day} {self.due_date:%b %Y}"
        if self.due_time:
            return f"{day} at {self.due_time:%H:%M}"
        return day

    def get_edit_url(self) -> str:
        """Where a link to this task goes. One definition, so a calendar cell
        and a list row cannot disagree about where a task lives."""
        from django.urls import reverse

        return reverse("tasks:edit", args=[self.pk])

    def is_overdue(self, as_of_date, as_of_time=None) -> bool:
        """Past its date, or past its time on the day it is due.

        A task set for 09:00 is overdue at 09:01, not at midnight. One with no
        time is not overdue until the day is over, which is what "due today"
        has always meant here.
        """
        if not self.is_open:
            return False
        if self.due_date < as_of_date:
            return True
        if self.due_date == as_of_date and self.due_time and as_of_time:
            return as_of_time > self.due_time
        return False

    @property
    def is_overdue_now(self) -> bool:
        """``is_overdue`` against the business clock, for templates.

        A template cannot pass arguments to a method, and the alternative -
        comparing ``task.due_date < today`` in the markup - is how five pages
        ended up each carrying their own half-right definition of overdue.
        """
        from csm.core import clock

        return self.is_overdue(clock.today(), clock.local_time())


class TaskAssignment(models.Model):
    """Who receives a task.

    Many-to-many rather than a single owner column, because renewal and
    quarterly tasks route to the CSM and the ACM together, as does every task on
    a POC-stage account.
    """

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="assignments")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="task_assignments"
    )
    assigned_as = models.CharField(max_length=8, choices=TaskAssignmentRole.choices)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "task_assignments"
        constraints = [
            models.UniqueConstraint(fields=["task", "user"], name="task_assignment_unique")
        ]
        indexes = [models.Index(fields=["user", "task"])]

    def __str__(self) -> str:
        return f"{self.user_id} on task {self.task_id}"
