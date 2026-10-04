"""Register the recurring jobs with django-q.

Without this the daily half of the dual trigger never runs. The event hooks keep
a score current while somebody is using the platform, but an account that goes
quiet is exactly the one nothing fires an event for - so the day-count factors
would sit frozen at whatever they were when the account was last touched, which
is the opposite of what they are for.

Order is deliberate and the gaps between the jobs are the point:

1. **Health recalculation** just after midnight in the business timezone, so the
   day counts roll over on the right side of the boundary.
2. **Task generation** after it, so a health alert raised overnight is already
   in place when the renewal and meeting reminders are written.
3. **Notifications** in the morning, so the digest describes the day the reader
   is actually starting rather than the one that just ended.

Idempotent: running it twice updates the same three rows rather than doubling
them, which matters because it belongs in a deploy script.
"""

from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand

from csm.core import clock

#: name -> (dotted path, local hour, local minute). Daily, business time.
JOBS = (
    ("csm.recalculate_health", "csm.core.jobs.recalculate_health", 0, 30),
    ("csm.generate_tasks", "csm.core.jobs.generate_tasks", 1, 0),
    # 07:00: the digest is the start of the working day, and it is now the only
    # thing that announces a task at all, since creating one no longer notifies
    # anybody.
    ("csm.send_due_notifications", "csm.core.jobs.send_due_notifications", 7, 0),
)

#: name -> (dotted path, minutes between runs). An hour before a 09:00 call is
#: 08:00, which no daily job can hit, so this one runs on a short cycle and each
#: reminder decides for itself whether its moment has come.
MINUTE_JOBS = (
    ("csm.send_task_reminders", "csm.core.jobs.send_task_reminders", 5),
    # Registered whether or not Jira is configured: the command exits quietly
    # when it is not, so switching Jira on is a .env change and no redeploy.
    ("csm.sync_jira", "csm.core.jobs.sync_jira", 15),
)


def next_run_at(hour: int, minute: int, *, after=None):
    """The next occurrence of a business-local time, as a UTC instant.

    Built through ``core.clock`` rather than from a fixed offset. Egypt moves
    between +02:00 and +03:00, so a schedule pinned to a numeric offset drifts
    an hour twice a year and the midnight job starts running at eleven at night,
    on the wrong side of the day boundary it exists to respect.
    """
    after = after or clock.now()
    local_today = clock.to_business_date(after)
    candidate = dt.datetime.combine(
        local_today, dt.time(hour, minute), tzinfo=clock.business_tz()
    ).astimezone(dt.UTC)
    if candidate <= after:
        candidate = dt.datetime.combine(
            local_today + dt.timedelta(days=1),
            dt.time(hour, minute),
            tzinfo=clock.business_tz(),
        ).astimezone(dt.UTC)
    return candidate


class Command(BaseCommand):
    help = "Create or update the daily django-q schedules. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="Show the schedules without changing anything.",
        )

    def handle(self, *args, **options):
        from django_q.models import Schedule

        if options["list"]:
            for schedule in Schedule.objects.filter(name__startswith="csm."):
                self.stdout.write(
                    f"{schedule.name:32} {schedule.func:40} next {schedule.next_run}"
                )
            if not Schedule.objects.filter(name__startswith="csm.").exists():
                self.stdout.write(self.style.WARNING("No schedules registered."))
            return

        for name, func, hour, minute in JOBS:
            schedule, created = Schedule.objects.update_or_create(
                name=name,
                defaults={
                    "func": func,
                    "schedule_type": Schedule.DAILY,
                    "repeats": -1,
                    "next_run": next_run_at(hour, minute),
                },
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(
                f"{verb} {name} -> {func} at {hour:02d}:{minute:02d} "
                f"{clock.business_tz()} (next {schedule.next_run:%Y-%m-%d %H:%M} UTC)"
            )

        for name, func, minutes in MINUTE_JOBS:
            schedule, created = Schedule.objects.update_or_create(
                name=name,
                defaults={
                    "func": func,
                    "schedule_type": Schedule.MINUTES,
                    "minutes": minutes,
                    "repeats": -1,
                    "next_run": clock.now(),
                },
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(f"{verb} {name} -> {func} every {minutes} minutes")

        self.stdout.write(
            self.style.SUCCESS(
                "Schedules registered. They only run while `manage.py qcluster` "
                "is up; without it nothing recalculates overnight."
            )
        )
