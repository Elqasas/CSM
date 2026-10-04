"""Outlook calendars into meetings; Teams attendance into activities.

What matters: one meeting per occurrence however many calendars hold it; a
person sees only what is in their own calendar; a calendar that could not be
read is left alone rather than emptied; and - the rule the health score
depends on - an invitation is not attendance. Only a contact whose stored email
appears in the Teams attendance report makes a meeting count, and it counts
once.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse

import pytest

from csm.activities.models import Activity
from csm.core.enums import ActivitySource, ActivityType, IntegrationProvider
from csm.integrations.models import SyncState
from csm.integrations.teams.client import (
    Attendee,
    FakeGraphClient,
    GraphError,
    GraphEvent,
)
from csm.integrations.teams.http import HttpGraphClient, parse_graph_time
from csm.integrations.teams.sync import REPORT_GIVE_UP, run
from csm.meetings.models import Attendance, CalendarEntry, Meeting
from tests.factories import AccountFactory, ContactFactory, UserFactory

NOW = dt.datetime(2026, 10, 5, 10, 0, tzinfo=dt.UTC)
JOIN = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0"


def event(uid="uid-1", *, start=None, minutes=60, attendees=(), teams=True, **kw) -> GraphEvent:
    start = start or NOW - dt.timedelta(hours=3)
    return GraphEvent(
        event_id=kw.pop("event_id", f"evt-{uid}"),
        ical_uid=uid,
        subject=kw.pop("subject", "Quarterly review"),
        starts_at=start,
        ends_at=start + dt.timedelta(minutes=minutes),
        is_teams=teams,
        join_url=JOIN if teams else "",
        organizer_email=kw.pop("organizer_email", "csm@dphish.com"),
        attendees=tuple(Attendee(email=a) for a in attendees),
        **kw,
    )


@pytest.fixture
def csm(db):
    return UserFactory(csm=True, email="csm@dphish.com")


@pytest.fixture
def account(csm):
    return AccountFactory(csm=csm, client_name="Acme")


@pytest.fixture
def contact(account):
    return ContactFactory(account=account, full_name="Rana Client", email="Rana@Acme.com")


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------


def test_one_meeting_however_many_calendars_hold_it(csm, contact):
    colleague = UserFactory(csm=True, email="colleague@dphish.com")
    shared = event(attendees=["rana@acme.com", "colleague@dphish.com"])
    client = FakeGraphClient(calendars={
        "csm@dphish.com": [shared],
        "colleague@dphish.com": [shared],
    })
    UserFactory(acm=True, email="nobody@dphish.com")  # no mailbox

    report = run(client, now=NOW)

    assert Meeting.objects.count() == 1
    meeting = Meeting.objects.get()
    assert set(meeting.entries.values_list("user__email", flat=True)) == {
        "csm@dphish.com", "colleague@dphish.com",
    }
    assert list(Meeting.objects.for_user(colleague)) == [meeting]
    assert "nobody@dphish.com" in report["no_mailbox"]
    assert report["calendars_read"] == 2


def test_a_meeting_is_linked_to_contacts_by_their_stored_email(contact):
    """Case is presentation: Rana@Acme.com on the contact matches rana@acme.com."""
    run(FakeGraphClient(calendars={"csm@dphish.com": [event(attendees=["rana@acme.com"])]}), now=NOW)

    meeting = Meeting.objects.get()
    assert list(meeting.contacts.all()) == [contact]
    assert meeting.attendance == Attendance.PENDING


def test_only_a_teams_meeting_with_a_client_invited_waits_for_attendance(contact):
    client = FakeGraphClient(calendars={"csm@dphish.com": [
        event("internal", attendees=["colleague@dphish.com"]),
        event("in-person", attendees=["rana@acme.com"], teams=False),
    ]})
    run(client, now=NOW)

    assert set(Meeting.objects.values_list("attendance", flat=True)) == {Attendance.NOT_APPLICABLE}
    assert client.attendance_calls == []


def test_a_meeting_gone_from_a_calendar_leaves_it(csm):
    client = FakeGraphClient(calendars={"csm@dphish.com": [event("a"), event("b")]})
    run(client, now=NOW)
    assert Meeting.objects.count() == 2

    client.calendars["csm@dphish.com"] = [event("a")]
    report = run(client, now=NOW)

    assert list(Meeting.objects.values_list("external_id", flat=True)) == ["a"]
    assert report["meetings_deleted"] == 1


def test_a_cancelled_meeting_is_not_shown(csm):
    run(FakeGraphClient(calendars={"csm@dphish.com": [event(is_cancelled=True)]}), now=NOW)
    assert not Meeting.objects.exists()


def test_a_calendar_that_could_not_be_read_is_left_alone(csm):
    """A 403 or a missing mailbox says nothing about the meetings - emptying the
    calendar would make a policy change look like a week of cancellations."""
    client = FakeGraphClient(calendars={"csm@dphish.com": [event()]})
    run(client, now=NOW)

    client.forbidden.add("csm@dphish.com")
    report = run(client, now=NOW)

    assert report["forbidden"] == ["csm@dphish.com"]
    assert CalendarEntry.objects.filter(user=csm).count() == 1


def test_a_private_meeting_keeps_its_subject_to_its_owner(csm):
    run(FakeGraphClient(calendars={"csm@dphish.com": [
        event(subject="", is_private=True, location="")
    ]}), now=NOW)
    meeting = Meeting.objects.get()
    assert meeting.title == "Busy"
    assert meeting.subject == ""


def test_running_twice_changes_nothing(contact):
    client = FakeGraphClient(calendars={"csm@dphish.com": [event(attendees=["rana@acme.com"])]})
    run(client, now=NOW)
    run(client, now=NOW)
    assert Meeting.objects.count() == 1
    assert CalendarEntry.objects.count() == 1


def test_a_failure_is_recorded_and_writes_nothing(csm):
    client = FakeGraphClient(calendars={"csm@dphish.com": [event()]})
    client.error = GraphError("token refused")

    with pytest.raises(GraphError):
        run(client, now=NOW)

    state = SyncState.objects.get(provider=IntegrationProvider.TEAMS)
    assert state.last_error == "token refused"
    assert not Meeting.objects.exists()


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


def test_a_client_who_joined_makes_the_meeting_count(csm, account, contact):
    client = FakeGraphClient(
        calendars={"csm@dphish.com": [event(attendees=["rana@acme.com"])]},
        reports={JOIN: ["csm@dphish.com", "rana@acme.com"]},
    )
    report = run(client, now=NOW)

    meeting = Meeting.objects.get()
    assert meeting.attendance == Attendance.COUNTED
    activity = Activity.objects.get()
    assert activity.account == account
    assert activity.contact == contact
    assert activity.activity_type == ActivityType.MEETING
    assert activity.source == ActivitySource.TEAMS
    assert activity.occurred_on == dt.date(2026, 10, 5)
    assert activity.created_by == csm, "recorded as the organizer"
    assert Activity.objects.meetings().filter(account=account).count() == 1, "the engine reads it"
    assert report["attendance"]["counted"] == 1


def test_invited_but_absent_does_not_count(contact):
    """The user's rule: a client who did not attend is not contact."""
    client = FakeGraphClient(
        calendars={"csm@dphish.com": [event(attendees=["rana@acme.com"])]},
        reports={JOIN: ["csm@dphish.com"]},
    )
    run(client, now=NOW)

    assert Meeting.objects.get().attendance == Attendance.NO_SHOW
    assert not Activity.objects.exists()


def test_someone_else_from_the_client_does_not_count(contact):
    """Matched by the email stored on a contact, never by domain."""
    client = FakeGraphClient(
        calendars={"csm@dphish.com": [event(attendees=["rana@acme.com"])]},
        reports={JOIN: ["intern@acme.com"]},
    )
    run(client, now=NOW)
    assert Meeting.objects.get().attendance == Attendance.NO_SHOW
    assert not Activity.objects.exists()


def test_a_counted_meeting_is_logged_once(contact):
    client = FakeGraphClient(
        calendars={"csm@dphish.com": [event(attendees=["rana@acme.com"])]},
        reports={JOIN: ["rana@acme.com"]},
    )
    run(client, now=NOW)
    run(client, now=NOW + dt.timedelta(minutes=15))

    assert Activity.objects.count() == 1
    assert client.attendance_calls == [JOIN], "a settled meeting is not asked about again"


def test_one_activity_per_account_whose_contact_joined(contact):
    other = ContactFactory(account=AccountFactory(client_name="Globex"), email="li@globex.com")
    client = FakeGraphClient(
        calendars={"csm@dphish.com": [event(attendees=["rana@acme.com", "li@globex.com"])]},
        reports={JOIN: ["rana@acme.com", "li@globex.com"]},
    )
    run(client, now=NOW)

    assert set(Activity.objects.values_list("account_id", flat=True)) == {
        contact.account_id, other.account_id,
    }


def test_a_meeting_still_in_progress_is_not_judged(contact):
    client = FakeGraphClient(
        calendars={"csm@dphish.com": [event(start=NOW - dt.timedelta(minutes=30))]},
        reports={JOIN: ["rana@acme.com"]},
    )
    run(client, now=NOW)
    assert client.attendance_calls == []


def test_no_report_yet_waits_then_gives_up(contact):
    client = FakeGraphClient(calendars={"csm@dphish.com": [event(attendees=["rana@acme.com"])]})
    run(client, now=NOW)
    assert Meeting.objects.get().attendance == Attendance.PENDING

    run(client, now=NOW + REPORT_GIVE_UP + dt.timedelta(hours=4))
    assert Meeting.objects.get().attendance == Attendance.UNAVAILABLE
    assert not Activity.objects.exists()


def test_attendance_counting_can_be_switched_off(contact):
    client = FakeGraphClient(
        calendars={"csm@dphish.com": [event(attendees=["rana@acme.com"])]},
        reports={JOIN: ["rana@acme.com"]},
    )
    run(client, now=NOW, count_attendance=False)

    assert client.attendance_calls == []
    assert not Activity.objects.exists()


def test_a_contact_on_a_deleted_account_does_not_count(account, contact):
    account.deleted_at = NOW
    account.save(update_fields=["deleted_at"])
    client = FakeGraphClient(
        calendars={"csm@dphish.com": [event(attendees=["rana@acme.com"])]},
        reports={JOIN: ["rana@acme.com"]},
    )
    run(client, now=NOW)
    assert not Activity.objects.exists()


# ---------------------------------------------------------------------------
# The HTTP client, with an injected transport
# ---------------------------------------------------------------------------


def test_graph_times_with_seven_fractional_digits_are_read():
    moment = parse_graph_time({"dateTime": "2026-10-05T09:30:00.0000000", "timeZone": "UTC"})
    assert moment == dt.datetime(2026, 10, 5, 9, 30, tzinfo=dt.UTC)


class Recorder:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers))
        for prefix, answer in self.answers:
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected {url}")


TOKEN = ("https://login.microsoftonline.com/", {"access_token": "tok", "expires_in": 3600})


def raw_event(**overrides):
    raw = {
        "id": "AAMk1",
        "iCalUId": "0400000082",
        "subject": "Renewal call",
        "start": {"dateTime": "2026-10-06T07:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-06T07:30:00.0000000", "timeZone": "UTC"},
        "isAllDay": False,
        "isCancelled": False,
        "sensitivity": "normal",
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
        "onlineMeeting": {"joinUrl": JOIN},
        "webLink": "https://outlook.office365.com/owa/?itemid=1",
        "location": {"displayName": "Microsoft Teams Meeting"},
        "organizer": {"emailAddress": {"name": "Casey", "address": "CSM@dphish.com"}},
        "attendees": [
            {"type": "required", "status": {"response": "accepted"},
             "emailAddress": {"name": "Rana", "address": "Rana@Acme.com"}},
        ],
        "responseStatus": {"response": "organizer"},
    }
    raw.update(overrides)
    return raw


def test_the_calendar_view_is_read_page_by_page_in_utc():
    page2 = "https://graph.microsoft.com/v1.0/next-page"
    transport = Recorder([
        TOKEN,
        (page2, {"value": [raw_event(id="AAMk2", iCalUId="0400000083")]}),
        ("https://graph.microsoft.com/v1.0/users/csm@dphish.com/calendarView",
         {"value": [raw_event()], "@odata.nextLink": page2}),
    ])
    client = HttpGraphClient(tenant_id="t", client_id="c", client_secret="s", transport=transport)

    events = client.calendar_view("csm@dphish.com", NOW, NOW + dt.timedelta(days=1))

    assert [e.ical_uid for e in events] == ["0400000082", "0400000083"]
    first = events[0]
    assert first.is_teams and first.join_url == JOIN
    assert first.organizer_email == "csm@dphish.com"
    assert first.attendees[0].email == "rana@acme.com"
    view_call = transport.calls[1]
    assert view_call[2]["Prefer"] == 'outlook.timezone="UTC"'
    assert view_call[2]["Authorization"] == "Bearer tok"
    assert [c[0] for c in transport.calls].count("POST") == 1, "one token for the run"


def test_a_private_event_arrives_without_its_subject():
    transport = Recorder([
        TOKEN,
        ("https://graph.microsoft.com/v1.0/users/",
         {"value": [raw_event(sensitivity="private", subject="Doctor")]}),
    ])
    client = HttpGraphClient(tenant_id="t", client_id="c", client_secret="s", transport=transport)
    [only] = client.calendar_view("csm@dphish.com", NOW, NOW)
    assert only.is_private and only.subject == "" and only.location == ""


def test_attendance_is_read_from_the_report_by_join_url():
    base = "https://graph.microsoft.com/v1.0/users/user-guid/onlineMeetings"
    transport = Recorder([
        TOKEN,
        (f"{base}/mtg-1/attendanceReports/rep-1/attendanceRecords",
         {"value": [{"emailAddress": "Rana@Acme.com"}, {"emailAddress": ""}]}),
        (f"{base}/mtg-1/attendanceReports", {"value": [{"id": "rep-1"}]}),
        (f"{base}?", {"value": [{"id": "mtg-1"}]}),
        ("https://graph.microsoft.com/v1.0/users/csm@dphish.com?", {"id": "user-guid"}),
    ])
    client = HttpGraphClient(tenant_id="t", client_id="c", client_secret="s", transport=transport)

    assert client.attendance("csm@dphish.com", JOIN) == ["rana@acme.com"]
    lookup = next(url for _, url, _ in transport.calls if url.startswith(f"{base}?"))
    assert "JoinWebUrl eq '" + JOIN + "'" in urllib.parse.unquote_plus(lookup)


def test_no_report_is_none_not_an_empty_list():
    base = "https://graph.microsoft.com/v1.0/users/user-guid/onlineMeetings"
    transport = Recorder([
        TOKEN,
        (f"{base}/mtg-1/attendanceReports", {"value": []}),
        (f"{base}?", {"value": [{"id": "mtg-1"}]}),
        ("https://graph.microsoft.com/v1.0/users/csm@dphish.com?", {"id": "user-guid"}),
    ])
    client = HttpGraphClient(tenant_id="t", client_id="c", client_secret="s", transport=transport)
    assert client.attendance("csm@dphish.com", JOIN) is None


def test_a_token_refusal_is_a_graph_error():
    transport = Recorder([("https://login.microsoftonline.com/", {"error": "invalid_client"})])
    client = HttpGraphClient(tenant_id="t", client_id="c", client_secret="s", transport=transport)
    with pytest.raises(GraphError):
        client.calendar_view("csm@dphish.com", NOW, NOW)

