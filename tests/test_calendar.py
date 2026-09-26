"""The calendar: a layout, and a forecast that writes nothing down.

The grid is geometry and the projection is arithmetic over a rule, so most of
this needs no database - the same reason the scoring engine's tests do not.

The rule it exists to respect: a repeating task keeps one open occurrence at a
time, and looking at a month must not change that. A calendar that materialised
what it drew would fill the queue with work nobody has to do yet.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from csm.core.enums import RepeatEnd, RepeatFrequency, TaskCategory, TaskType
from csm.schedule import grid, projection
from csm.tasks.models import Task, TaskSeries
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login

JUNE = dt.date(2026, 6, 15)


# ---------------------------------------------------------------------------
# The grid - no database
# ---------------------------------------------------------------------------


def test_a_month_is_always_six_weeks():
    """February and a 31-day month starting on a Sunday occupy the same space,
    so the page does not resize as you page through the year."""
    for month in range(1, 13):
        weeks = grid.month_grid(dt.date(2026, month, 1))
        assert len(weeks) == 6
        assert all(len(week) == 7 for week in weeks)


def test_the_grid_starts_on_the_working_weeks_first_day():
    """Sunday by default: Egypt works Sunday to Thursday, and a grid starting
    on Monday splits that week across two rows - the one thing a week view must
    not do."""
    weeks = grid.month_grid(JUNE)
    first = weeks[0][0].date

    assert first.weekday() == 6, "Sunday"
    days = [day for week in weeks for day in week]
    in_month = [day.date for day in days if day.in_month]
    assert in_month[0] == dt.date(2026, 6, 1)
    assert in_month[-1] == dt.date(2026, 6, 30)
    assert len(in_month) == 30


def test_days_outside_the_month_are_marked():
    """September 2026 starts on a Tuesday, so the grid opens with 31 August -
    a month that starts on a Monday would have nothing to show here."""
    weeks = grid.month_grid(dt.date(2026, 9, 15))
    outside = [day.date for week in weeks for day in week if not day.in_month]

    assert dt.date(2026, 8, 31) in outside
    assert dt.date(2026, 10, 1) in outside
    assert dt.date(2026, 9, 1) not in outside


def test_a_week_runs_sunday_to_saturday_by_default():
    week = grid.week_of(dt.date(2026, 6, 17))
    assert week[0] == dt.date(2026, 6, 14), "Sunday"
    assert week[-1] == dt.date(2026, 6, 20)


def test_a_reader_who_starts_on_monday_gets_monday():
    week = grid.week_of(dt.date(2026, 6, 17), first_weekday=0)
    assert week[0] == dt.date(2026, 6, 15)


def test_the_headings_follow_the_first_day():
    """A heading that disagrees with the column under it is worse than none."""
    assert grid.weekday_headings(6)[0] == "Sun"
    assert grid.weekday_headings(0)[0] == "Mon"
    assert len(set(grid.weekday_headings(6))) == 7


@pytest.mark.parametrize(
    ("start", "step", "expected"),
    [
        (dt.date(2026, 1, 31), 1, dt.date(2026, 2, 28)),
        (dt.date(2026, 6, 15), -1, dt.date(2026, 5, 15)),
        (dt.date(2026, 12, 15), 1, dt.date(2027, 1, 15)),
    ],
)
def test_paging_a_month_lands_where_it_should(start, step, expected):
    assert grid.add_months(start, step) == expected


# ---------------------------------------------------------------------------
# The forecast - no database either
# ---------------------------------------------------------------------------


class FakeSeries:
    is_active = True
    created_count = 1
    occurrences = None
    until = None
    priority = "MEDIUM"
    title = "Weekly sync"
    due_time = None

    def __init__(self, frequency=RepeatFrequency.WEEKLY, **kwargs):
        self.frequency = frequency
        self.ends = RepeatEnd.NEVER
        self.pk = 1
        for key, value in kwargs.items():
            setattr(self, key, value)

    def get_task_type_display(self):
        return "Meeting"


def test_a_weekly_series_fills_the_month():
    """The point of the whole thing: one live task, four visible weeks.

    A month showing one of four weekly meetings shows the wrong week.
    """
    out = projection.project(
        FakeSeries(),
        start=dt.date(2026, 6, 1),
        end=dt.date(2026, 6, 30),
        taken={JUNE},
        from_date=JUNE,
    )
    assert [p.when for p in out] == [
        dt.date(2026, 6, 22), dt.date(2026, 6, 29),
    ]


def test_a_day_that_already_has_a_task_is_not_projected_onto():
    """The real occurrence is drawn from the task; projecting it too would show
    the same meeting twice."""
    out = projection.project(
        FakeSeries(frequency=RepeatFrequency.DAILY),
        start=dt.date(2026, 6, 15),
        end=dt.date(2026, 6, 18),
        taken={dt.date(2026, 6, 16)},
        from_date=JUNE,
    )
    assert [p.when for p in out] == [dt.date(2026, 6, 17), dt.date(2026, 6, 18)]


def test_a_forecast_stops_at_the_end_of_the_window():
    """Bounded by the range rather than by a count, so a daily series looked at
    over a month is thirty steps and not an endless loop."""
    out = projection.project(
        FakeSeries(frequency=RepeatFrequency.DAILY),
        start=dt.date(2026, 6, 1),
        end=dt.date(2026, 6, 30),
        from_date=JUNE,
    )
    assert len(out) == 15
    assert out[-1].when == dt.date(2026, 6, 30)


def test_after_a_number_of_times_forecasts_only_what_is_left():
    series = FakeSeries(ends=RepeatEnd.AFTER, occurrences=3, created_count=1)
    out = projection.project(
        series, start=dt.date(2026, 6, 1), end=dt.date(2026, 12, 31), from_date=JUNE
    )
    assert len(out) == 2


def test_on_a_date_forecasts_no_further_than_that():
    series = FakeSeries(ends=RepeatEnd.ON, until=dt.date(2026, 6, 29))
    out = projection.project(
        series, start=dt.date(2026, 6, 1), end=dt.date(2026, 12, 31), from_date=JUNE
    )
    assert [p.when for p in out] == [dt.date(2026, 6, 22), dt.date(2026, 6, 29)]


def test_a_finished_series_forecasts_nothing():
    series = FakeSeries()
    series.is_active = False
    assert projection.project(
        series, start=dt.date(2026, 6, 1), end=dt.date(2026, 12, 31), from_date=JUNE
    ) == []


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


@pytest.fixture
def account(db):
    csm = UserFactory(csm=True, email="cal-csm@dphish.com", full_name="Casey Morgan")
    csm.set_password(PASSWORD)
    csm.save(update_fields=["password"])
    return AccountFactory(client_code="CAL1", client_name="Calendar Co", csm=csm, acm=None)


@pytest.fixture
def weekly(account):
    series = TaskSeries.objects.create(
        account=account, task_type=TaskType.MEETING, title="Weekly sync",
        category=TaskCategory.CLIENT_FACING, frequency=RepeatFrequency.WEEKLY,
        ends=RepeatEnd.NEVER, created_by=account.csm,
    )
    from csm.tasks import recurrence

    recurrence.start(series, first_due=dt.date.today())
    return series


def projected_days(response):
    return sorted(
        day for day, bucket in response.context["by_day"].items() if bucket["projected"]
    )


def test_the_repeat_shows_across_the_month_without_being_written_down(
    client, account, weekly
):
    """The requirement, and the rule it must not break."""
    import datetime

    before = Task.objects.count()
    login(client, account.csm)

    response = client.get(reverse("schedule:calendar"))
    days = projected_days(response)
    last_cell = max(response.context["by_day"], default=dt.date.today())

    # Every seventh day from today to the end of the grid, and not one of them
    # written down. Asserted as the cadence rather than as a count, because how
    # many fit depends on where in the month today happens to fall.
    expected = []
    cursor = dt.date.today() + datetime.timedelta(days=7)
    while cursor <= response.context["weeks"][-1][-1].date:
        expected.append(cursor)
        cursor += datetime.timedelta(days=7)

    assert days == expected, last_cell
    assert days, "a weekly repeat should show at least once more this month"
    assert Task.objects.count() == before, "and writes none of them down"


def test_the_real_occurrence_is_not_drawn_twice(client, account, weekly):
    login(client, account.csm)
    response = client.get(reverse("schedule:calendar"))

    today = dt.date.today()
    bucket = response.context["by_day"][today]
    assert len(bucket["tasks"]) == 1
    assert bucket["projected"] == [], "today has the real one already"


@pytest.mark.parametrize("mode", ["month", "week", "day"])
def test_every_range_renders(client, account, weekly, mode):
    login(client, account.csm)
    response = client.get(reverse("schedule:calendar"), {"view": mode})

    assert response.status_code == 200
    assert response.context["mode"] == mode


def test_an_unreadable_date_or_range_falls_back_to_today(client, account):
    login(client, account.csm)
    response = client.get(
        reverse("schedule:calendar"), {"view": "decade", "date": "not-a-date"}
    )

    assert response.context["mode"] == "month"
    assert response.context["focus"] == dt.date.today()


def test_a_colleagues_work_is_not_on_your_calendar(client, account):
    stranger = UserFactory(csm=True, email="cal-other@dphish.com")
    theirs = AccountFactory(client_code="CAL2", client_name="Not Yours", csm=stranger)
    Task.objects.create(
        account=theirs, task_type=TaskType.CALL, title="Their call",
        due_date=dt.date.today(), created_by=stranger,
    )

    login(client, account.csm)
    body = client.get(reverse("schedule:calendar")).content.decode()
    assert "Their call" not in body


def test_an_acm_has_no_health_alerts_on_their_calendar(client, account):
    """Read through ``visible_tasks`` like every other surface: an alert on the
    calendar would leak that one exists, which is itself a health signal."""
    acm = UserFactory(acm=True, email="cal-acm@dphish.com")
    acm.set_password(PASSWORD)
    acm.save(update_fields=["password"])
    account.acm = acm
    account.save(update_fields=["acm"])

    Task.objects.create(
        account=account, task_type=TaskType.AUTO_HEALTH_ALERT,
        title="At Risk: Calendar Co", due_date=dt.date.today(),
        auto_key="alert:cal:2026-09-23",
    )

    login(client, acm)
    body = client.get(reverse("schedule:calendar")).content.decode()
    assert "At Risk" not in body


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def test_a_reader_who_has_never_opened_the_settings_gets_the_working_week(
    client, account
):
    """Never None: a page that cannot render because somebody has no row is a
    worse failure than a preference nobody has chosen."""
    from csm.schedule.models import CalendarPreference

    preference = CalendarPreference.for_user(account.csm)

    assert preference.first_weekday == 6, "Sunday"
    assert preference.weekend == (4, 5), "Friday and Saturday"
    assert preference.default_view == "month"


def test_the_weekend_follows_the_first_day_rather_than_being_asked_for(client, account):
    """Making it a third question would let somebody save a combination that
    means nothing - a week starting Sunday whose weekend is Sunday."""
    from csm.schedule.models import CalendarPreference, Weekday

    preference = CalendarPreference.for_user(account.csm)
    preference.first_weekday = Weekday.MONDAY
    assert preference.weekend == (5, 6), "Saturday and Sunday"


def test_the_settings_page_changes_how_the_calendar_is_drawn(client, account):
    login(client, account.csm)

    response = client.post(
        reverse("schedule:settings"),
        {"first_weekday": "0", "default_view": "week", "show_weekends": "on"},
    )
    assert response.status_code == 302

    page = client.get(reverse("schedule:calendar"))
    assert page.context["mode"] == "week", "it opens where they asked"
    assert page.context["headings"][0] == "Mon"


def test_turning_the_forecast_off_leaves_only_what_was_raised(client, account, weekly):
    login(client, account.csm)

    with_forecast = client.get(reverse("schedule:calendar"))
    assert projected_days(with_forecast)

    client.post(
        reverse("schedule:settings"),
        {"first_weekday": "6", "default_view": "month", "show_weekends": "on"},
    )
    without = client.get(reverse("schedule:calendar"))

    assert projected_days(without) == []
    assert any(bucket["tasks"] for bucket in without.context["by_day"].values()), (
        "the real occurrence is still there"
    )


def test_hiding_the_weekend_drops_those_columns_from_a_week(client, account):
    login(client, account.csm)
    client.post(
        reverse("schedule:settings"),
        {"first_weekday": "6", "default_view": "week", "show_projected": "on",
         "show_happened": "on"},
    )

    days = client.get(reverse("schedule:calendar"), {"view": "week"}).context["days"]
    assert len(days) == 5
    assert all(day.weekday() not in (4, 5) for day in days)


def test_preferences_are_one_persons_own(client, account):
    """They change how a calendar is drawn, never what is on it - so they are
    not an access question and do not go near ``core/access``."""
    from csm.schedule.models import CalendarPreference

    other = UserFactory(csm=True, email="cal-pref@dphish.com")
    login(client, account.csm)
    client.post(
        reverse("schedule:settings"),
        {"first_weekday": "0", "default_view": "day", "show_weekends": "on"},
    )

    assert CalendarPreference.for_user(other).first_weekday == 6
    assert CalendarPreference.for_user(account.csm).first_weekday == 0


def test_the_settings_are_reachable_from_the_gear(client, account):
    """The gear opens the settings page; that page carries the link."""
    login(client, account.csm)

    home = client.get(reverse("dashboard:index")).content.decode()
    assert reverse("settings") in home

    page = client.get(reverse("settings")).content.decode()
    assert reverse("schedule:settings") in page


def test_the_calendar_is_in_the_navigation(client, account):
    login(client, account.csm)
    body = client.get(reverse("dashboard:index")).content.decode()

    assert reverse("schedule:calendar") in body
    assert ">Calendar</a>" in body
