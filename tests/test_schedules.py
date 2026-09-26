"""The recurring jobs.

This is the piece whose absence is silent. The event hooks keep a score current
while somebody is working an account; the daily run is what catches the accounts
nobody has touched, which are precisely the ones the score exists to surface. If
no schedule is registered, nothing fails - the numbers simply stop moving.
"""

from __future__ import annotations

import datetime as dt
from io import StringIO

import pytest
import time_machine
from django.core.management import call_command

from csm.core.management.commands.setup_schedules import JOBS, MINUTE_JOBS, next_run_at


def run(*args) -> str:
    out = StringIO()
    call_command("setup_schedules", *args, stdout=out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# The job callables
# ---------------------------------------------------------------------------


def test_every_scheduled_path_actually_imports():
    """A dotted path is stored as text and resolved at run time.

    Getting one wrong fails at half past midnight rather than here, so the
    schedule points at real functions in ``core/jobs.py`` and this walks them.
    """
    from importlib import import_module

    for _, path, _, _ in JOBS:
        module_path, _, attribute = path.rpartition(".")
        module = import_module(module_path)
        assert callable(getattr(module, attribute)), path


def test_each_job_wraps_a_command_that_exists():
    from django.core.management import get_commands

    commands = get_commands()
    for name, _, _, _ in JOBS:
        assert name.removeprefix("csm.") in commands


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


@time_machine.travel("2026-09-21 09:00:00+00:00")
def test_next_run_is_computed_in_the_business_timezone():
    """Cairo is UTC+3 in September, so 00:30 local is 21:30 UTC the day before."""
    when = next_run_at(0, 30)
    assert when.tzinfo == dt.UTC
    assert (when.hour, when.minute) == (21, 30)
    assert when.date() == dt.date(2026, 9, 21)


@time_machine.travel("2026-01-15 09:00:00+00:00")
def test_the_offset_follows_daylight_saving_rather_than_a_fixed_number():
    """Egypt moves between +02:00 and +03:00.

    A schedule pinned to a numeric offset drifts an hour twice a year and the
    midnight job starts running at eleven at night, on the wrong side of the day
    boundary it exists to respect.
    """
    winter = next_run_at(0, 30)
    assert (winter.hour, winter.minute) == (22, 30)  # +02:00 in January

    with time_machine.travel("2026-07-15 09:00:00+00:00"):
        summer = next_run_at(0, 30)
    assert (summer.hour, summer.minute) == (21, 30)  # +03:00 in July


@time_machine.travel("2026-09-21 23:00:00+00:00")
def test_a_time_already_past_today_schedules_for_tomorrow():
    when = next_run_at(0, 30)
    assert when > dt.datetime(2026, 9, 21, 23, 0, tzinfo=dt.UTC)


def test_the_jobs_run_in_dependency_order():
    """Recalculate, then generate, then notify.

    A digest written before the generators have run describes yesterday, and
    tasks generated before the score has rolled over miss an account that went
    red overnight.
    """
    minutes = [hour * 60 + minute for _, _, hour, minute in JOBS]
    assert minutes == sorted(minutes)
    assert [name for name, _, _, _ in JOBS] == [
        "csm.recalculate_health",
        "csm.generate_tasks",
        "csm.send_due_notifications",
    ]


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_registering_creates_one_schedule_per_job():
    from django_q.models import Schedule

    output = run()

    assert Schedule.objects.filter(name__startswith="csm.").count() == len(JOBS) + len(MINUTE_JOBS)
    for name, path, _, _ in JOBS:
        schedule = Schedule.objects.get(name=name)
        assert schedule.func == path
        assert schedule.schedule_type == Schedule.DAILY
        assert schedule.repeats == -1
        assert name in output


@pytest.mark.django_db
def test_running_it_twice_updates_rather_than_doubling():
    """It belongs in a deploy script, so a second run must not duplicate."""
    from django_q.models import Schedule

    run()
    first = set(Schedule.objects.values_list("id", flat=True))

    output = run()

    assert Schedule.objects.filter(name__startswith="csm.").count() == len(JOBS) + len(MINUTE_JOBS)
    assert set(Schedule.objects.values_list("id", flat=True)) == first
    assert "Updated" in output


@pytest.mark.django_db
def test_a_drifted_schedule_is_corrected_rather_than_left(monkeypatch):
    from django_q.models import Schedule

    run()
    schedule = Schedule.objects.get(name="csm.recalculate_health")
    schedule.func = "csm.core.jobs.something_removed"
    schedule.save(update_fields=["func"])

    run()

    schedule.refresh_from_db()
    assert schedule.func == "csm.core.jobs.recalculate_health"


@pytest.mark.django_db
def test_listing_says_so_when_nothing_is_registered():
    output = run("--list")
    assert "No schedules registered" in output


@pytest.mark.django_db
def test_listing_shows_what_is_registered_without_changing_it():
    from django_q.models import Schedule

    run()
    before = list(Schedule.objects.values_list("id", "next_run"))

    output = run("--list")

    assert list(Schedule.objects.values_list("id", "next_run")) == before
    for name, _, _, _ in JOBS:
        assert name in output
