"""The calendar by the hour, Outlook meetings on it, and the source filter.

The layout is pure geometry and is asserted without a database. The page tests
hold the rules that matter: a meeting is seen only by whoever has it in their
own calendar, the filter separates what Bousla holds from what Microsoft does,
and a counted Teams meeting is not drawn twice.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from csm.core import clock
from csm.core.enums import ActivitySource, ActivityType, TaskCategory, TaskType
from csm.meetings.models import Attendance, CalendarEntry, Meeting, activity_external_id
from csm.schedule import grid
from csm.tasks.models import Task
from tests.factories import AccountFactory, ActivityFactory, ContactFactory, UserFactory
from tests.test_views import PASSWORD, login

# ---------------------------------------------------------------------------
# Layout - no database
# ---------------------------------------------------------------------------


def span(start, end, name="x"):
    return grid.Span(start, end, "task", name)


def test_spans_that_do_not_overlap_are_full_width():
    placed = grid.lay_out([span(540, 600, "a"), span(600, 660, "b")])
    assert [(p.item, p.lane, p.lanes) for p in placed] == [("a", 0, 1), ("b", 0, 1)]
    assert placed[0].width == 100


def test_overlapping_spans_sit_side_by_side():
    placed = {p.item: p for p in grid.lay_out([span(540, 600, "a"), span(570, 630, "b")])}
    assert (placed["a"].lane, placed["b"].lane) == (0, 1)
    assert placed["a"].lanes == placed["b"].lanes == 2
    assert placed["b"].left == 50


def test_a_freed_lane_is_reused_inside_one_cluster():
    """a overlaps b, b overlaps c, a and c do not: c takes a's lane, and the
    whole cluster is two wide rather than three."""
    placed = {p.item: p for p in grid.lay_out([
        span(540, 600, "a"), span(570, 690, "b"), span(600, 660, "c"),
    ])}
    assert placed["c"].lane == 0
    assert {p.lanes for p in placed.values()} == {2}


def test_a_later_cluster_is_not_narrowed_by_an_earlier_one():
    placed = {p.item: p for p in grid.lay_out([
        span(540, 600, "a"), span(540, 600, "b"), span(720, 780, "c"),
    ])}
    assert placed["c"].lanes == 1


def test_a_very_short_block_is_still_drawn_readable_and_does_not_overprint():
    placed = {p.item: p for p in grid.lay_out([span(540, 545, "a"), span(550, 560, "b")])}
    assert placed["a"].height == pytest.approx(grid.MIN_MINUTES / grid.MINUTES_PER_DAY * 100, abs=1e-3)
    assert placed["a"].lanes == 2, "drawn ends overlap, so they share the row"


def test_positions_are_fractions_of_the_day():
    [only] = grid.lay_out([span(720, 780)])
    assert only.top == 50
    assert only.height == pytest.approx(60 / 1440 * 100, abs=1e-3)


# ---------------------------------------------------------------------------
# Which days a meeting is on
# ---------------------------------------------------------------------------


def meeting_at(day, start, end, **kw):
    return Meeting(
        external_id=kw.pop("external_id", f"m-{day}-{start}"),
        starts_at=clock.business_instant(day, start),
        ends_at=clock.business_instant(day if end > start else day + dt.timedelta(days=1), end),
        **kw,
    )


def test_a_meeting_ending_at_midnight_does_not_spill_into_tomorrow():
    day = dt.date(2026, 10, 5)
    assert meeting_at(day, dt.time(22), dt.time(0)).days() == [day]


def test_a_meeting_across_midnight_is_on_both_days():
    day = dt.date(2026, 10, 5)
    assert meeting_at(day, dt.time(23), dt.time(1)).days() == [day, day + dt.timedelta(days=1)]


def test_an_all_day_event_keeps_its_own_dates():
    """Outlook stores it midnight to midnight UTC; read in Cairo it would start
    the evening before."""
    meeting = Meeting(
        starts_at=dt.datetime(2026, 10, 6, tzinfo=dt.UTC),
        ends_at=dt.datetime(2026, 10, 8, tzinfo=dt.UTC),
        is_all_day=True,
    )
    assert meeting.days() == [dt.date(2026, 10, 6), dt.date(2026, 10, 7)]


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


@pytest.fixture
def account(db):
    account = AccountFactory(client_name="Acme")
    account.csm.set_password(PASSWORD)
    account.csm.save(update_fields=["password"])
    return account


def own_meeting(user, *, start=dt.time(10), end=dt.time(11), **kw):
    meeting = meeting_at(clock.today(), start, end, subject=kw.pop("subject", "Client sync"), **kw)
    meeting.save()
    CalendarEntry.objects.create(meeting=meeting, user=user, event_id=f"evt-{meeting.pk}")
    return meeting


def timed_task(account, at=dt.time(14)):
    return Task.objects.create(
        account=account,
        title="Send the renewal quote",
        task_type=TaskType.MANUAL_NEXT_ACTION,
        category=TaskCategory.CLIENT_FACING,
        due_date=clock.today(),
        due_time=at,
        created_by=account.csm,
    )


def placed_kinds(response):
    today = clock.today()
    column = next(c for c in response.context["columns"] if c["date"] == today)
    return sorted(p.kind for p in column["placed"]), column


def test_the_week_puts_timed_work_and_meetings_in_the_hours(client, account):
    own_meeting(account.csm)
    timed_task(account)
    Task.objects.create(
        account=account, title="Untimed", task_type=TaskType.MANUAL_NEXT_ACTION,
        category=TaskCategory.CLIENT_FACING, due_date=clock.today(), created_by=account.csm,
    )
    login(client, account.csm)

    response = client.get(reverse("schedule:calendar"), {"view": "week"})

    kinds, column = placed_kinds(response)
    assert kinds == ["meeting", "task"]
    assert [t.title for t in column["all_day"]["tasks"]] == ["Untimed"]
    body = response.content.decode()
    assert "cal-daycol" in body and "Client sync" in body


@pytest.mark.parametrize("mode", ["week", "day"])
def test_week_and_day_render_the_hours(client, account, mode):
    own_meeting(account.csm)
    login(client, account.csm)
    response = client.get(reverse("schedule:calendar"), {"view": mode})
    assert response.status_code == 200
    assert response.content.decode().count('class="cal-hour-label"') == 24


def test_a_colleagues_meeting_is_not_on_my_calendar(client, account):
    colleague = UserFactory(csm=True, email="colleague-cal@dphish.com")
    own_meeting(colleague, subject="Colleague's one-to-one")
    login(client, account.csm)

    body = client.get(reverse("schedule:calendar"), {"view": "week"}).content.decode()
    assert "Colleague&#x27;s one-to-one" not in body and "Colleague's one-to-one" not in body


def test_the_source_filter_separates_platform_from_microsoft(client, account):
    own_meeting(account.csm)
    timed_task(account)
    login(client, account.csm)
    url = reverse("schedule:calendar")

    assert placed_kinds(client.get(url, {"view": "day", "source": "platform"}))[0] == ["task"]
    assert placed_kinds(client.get(url, {"view": "day", "source": "microsoft"}))[0] == ["meeting"]
    assert placed_kinds(client.get(url, {"view": "day", "source": "all"}))[0] == ["meeting", "task"]


def test_an_unreadable_source_shows_everything(client, account):
    own_meeting(account.csm)
    login(client, account.csm)
    response = client.get(reverse("schedule:calendar"), {"view": "day", "source": "nonsense"})
    assert response.context["source"] == "all"


def test_the_filter_survives_paging(client, account):
    own_meeting(account.csm)
    login(client, account.csm)
    body = client.get(
        reverse("schedule:calendar"), {"view": "week", "source": "microsoft"}
    ).content.decode()
    assert "source=microsoft" in body.split('aria-label="Previous"')[0].rsplit("href=", 1)[1]


def test_a_counted_meeting_is_not_drawn_twice(client, account):
    contact = ContactFactory(account=account, email="rana@acme.com")
    meeting = own_meeting(account.csm, attendance=Attendance.COUNTED)
    meeting.contacts.add(contact)
    ActivityFactory(
        account=account, contact=contact, activity_type=ActivityType.MEETING,
        occurred_on=clock.today(), source=ActivitySource.TEAMS,
        external_id=activity_external_id(meeting.pk, account.pk), created_by=account.csm,
        subject="Client sync",
    )
    login(client, account.csm)

    response = client.get(reverse("schedule:calendar"), {"view": "day"})
    _, column = placed_kinds(response)
    assert column["all_day"]["activities"] == []
    assert "Client joined" in response.content.decode()

    platform = client.get(reverse("schedule:calendar"), {"view": "day", "source": "platform"})
    _, column = placed_kinds(platform)
    assert len(column["all_day"]["activities"]) == 1, "without the meeting, the log line shows"


def test_a_meeting_names_only_clients_the_reader_may_see(client, account):
    hidden = AccountFactory(client_name="Someone Else's Client")
    meeting = own_meeting(account.csm)
    meeting.contacts.add(
        ContactFactory(account=account, email="a@acme.com"),
        ContactFactory(account=hidden, email="b@other.com"),
    )
    login(client, account.csm)

    response = client.get(reverse("schedule:calendar"), {"view": "day"})
    _, column = placed_kinds(response)
    [placed] = [p for p in column["placed"] if p.kind == "meeting"]
    assert placed.item.clients == ["Acme"]


def test_the_month_lists_meetings_too(client, account):
    own_meeting(account.csm)
    login(client, account.csm)
    response = client.get(reverse("schedule:calendar"), {"view": "month"})
    assert len(response.context["by_day"][clock.today()]["meetings"]) == 1


# ---------------------------------------------------------------------------
# The connection page
# ---------------------------------------------------------------------------

GUID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
SECRET = "Qx8Q~very-secret-value"


@pytest.fixture
def admin(db):
    user = UserFactory(admin=True, email="teams-admin@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    return user


def save(client, **overrides):
    data = {
        "tenant_id": GUID, "client_id": GUID, "client_secret": SECRET,
        "count_attendance": "on", "enabled": "on", "action": "save",
    }
    data.update(overrides)
    data = {k: v for k, v in data.items() if v is not None}
    return client.post(reverse("integrations:teams"), data)


def test_the_secret_is_stored_encrypted_and_never_shown(client, admin):
    from csm.integrations.models import TeamsConnection
    from csm.integrations.teams import settings as teams

    login(client, admin)
    assert save(client).status_code == 302

    row = TeamsConnection.objects.get()
    assert SECRET not in row.client_secret_encrypted
    assert row.client_secret == SECRET
    assert teams.is_configured()
    assert SECRET not in client.get(reverse("integrations:teams")).content.decode()


def test_a_blank_secret_keeps_the_stored_one(client, admin):
    from csm.integrations.models import TeamsConnection

    login(client, admin)
    save(client)
    save(client, client_secret="")
    assert TeamsConnection.objects.get().client_secret == SECRET


def test_it_cannot_be_switched_on_half_configured(client, admin):
    from csm.integrations.models import TeamsConnection

    login(client, admin)
    response = save(client, client_secret="")
    assert response.status_code == 200
    assert not TeamsConnection.objects.exists()


def test_ids_are_held_to_microsofts_format(client, admin):
    login(client, admin)
    response = save(client, client_id="not-a-guid")
    assert "client ID looks like" in response.content.decode()


def test_only_an_administrator_reaches_the_page(client, account):
    login(client, account.csm)
    assert client.get(reverse("integrations:teams")).status_code in (403, 404)


def test_test_tries_the_form_without_saving(client, admin, monkeypatch):
    from csm.integrations.models import TeamsConnection
    from csm.integrations.teams import settings as teams

    seen = {}

    class Probe:
        def check(self, mailbox):
            seen["mailbox"] = mailbox
            return 3

    monkeypatch.setattr(teams, "client_for", lambda config: seen.setdefault("config", config) and Probe())
    login(client, admin)
    response = save(client, action="test")

    assert response.status_code == 200
    assert seen["mailbox"] == admin.email
    assert seen["config"].client_secret == SECRET
    assert not TeamsConnection.objects.exists()
