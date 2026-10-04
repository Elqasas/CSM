"""Outlook calendars into ``Meeting`` rows; Teams attendance into activities.

The run, in order:

1. **Read every calendar first.** For each active user, the window from
   ``DAYS_BACK`` ago to ``DAYS_AHEAD`` ahead, recurring meetings already
   expanded by Graph. A user whose address has no mailbox, or whom an access
   policy keeps out, is reported and skipped - their calendar is left as it was
   rather than emptied.
2. **Write in one transaction.** One ``Meeting`` per ``iCalUId``, one
   ``CalendarEntry`` per person holding it. Anything in the window a person's
   calendar no longer returns - cancelled, declined and removed, moved out - is
   taken off their calendar, and a meeting nobody holds any more is deleted.
   Each meeting is linked to every contact whose email is among its invitees.
3. **Ask Teams who actually came.** For an ended Teams meeting with a client
   contact invited, the attendance report is read. An invitation is not
   attendance: only a contact whose email appears in the report counts, and a
   meeting where none did is marked *No client contact joined* and logs nothing.
   A counted meeting writes one ``Meeting`` activity per account, through the
   model, so the ``post_save`` hook rescores it.

Idempotent throughout: meetings upsert on ``iCalUId``, entries on (user,
meeting), and the activity's ``external_id`` is unique with ``source=TEAMS``.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from csm.activities.models import Activity
from csm.contacts.models import Contact
from csm.core import clock
from csm.core.enums import ActivitySource, ActivityType, IntegrationProvider
from csm.integrations.models import SyncState
from csm.integrations.teams.client import (
    GraphClient,
    GraphError,
    GraphEvent,
    GraphForbidden,
    GraphNotFound,
)
from csm.meetings.models import (
    Attendance,
    CalendarEntry,
    Meeting,
    activity_external_id,
)

logger = logging.getLogger(__name__)

DAYS_BACK = 30
DAYS_AHEAD = 90

#: Teams publishes the attendance report a few minutes after the meeting ends.
REPORT_DELAY = dt.timedelta(minutes=10)
#: After this long without a report, stop asking.
REPORT_GIVE_UP = dt.timedelta(days=3)

SUBJECT_MAX = Meeting._meta.get_field("subject").max_length
LOCATION_MAX = Meeting._meta.get_field("location").max_length


def window(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    today = clock.to_business_date(now)
    return (
        clock.start_of_business_day(today - dt.timedelta(days=DAYS_BACK)),
        clock.start_of_business_day(today + dt.timedelta(days=DAYS_AHEAD + 1)),
    )


def contact_index() -> dict[str, list[int]]:
    """Lower-cased email -> the contacts carrying it, on live accounts only.

    Every contact in the company, not anybody's ``Ability``: the sync acts for
    the whole platform, and a meeting must find its client whoever owns it.
    """
    index: dict[str, list[int]] = defaultdict(list)
    rows = (
        Contact.objects.exclude(email="")
        .filter(account__in=_live_accounts())
        .values_list("pk", "email")
    )
    for pk, email in rows:
        index[email.strip().lower()].append(pk)
    return dict(index)


def _live_accounts():
    from csm.accounts.models import Account

    return Account.objects.alive()


def meeting_values(event: GraphEvent) -> dict:
    """The meeting columns an event determines. Pure."""
    return {
        "subject": event.subject[:SUBJECT_MAX],
        "starts_at": event.starts_at,
        "ends_at": max(event.ends_at, event.starts_at),
        "is_all_day": event.is_all_day,
        "is_private": event.is_private,
        "is_teams": event.is_teams,
        "join_url": event.join_url,
        "web_link": event.web_link,
        "location": event.location[:LOCATION_MAX],
        "organizer_email": event.organizer_email,
        "organizer_name": event.organizer_name[:255],
        "attendees": [
            {"email": a.email, "name": a.name, "response": a.response} for a in event.attendees
        ],
    }


def invited_contacts(event: GraphEvent, index: dict[str, list[int]]) -> set[int]:
    addresses = {a.email for a in event.attendees} | {event.organizer_email}
    return {pk for address in addresses if address for pk in index.get(address, [])}


def _attendance_for(meeting: Meeting, has_contacts: bool) -> str:
    """Whether this meeting is now waiting for an attendance report.

    A settled answer is never reopened by a later sync. Only a meeting that has
    not been judged yet moves between *not applicable* and *pending*, as its
    invite list or its Teams link changes.
    """
    if meeting.attendance in (Attendance.COUNTED, Attendance.NO_SHOW, Attendance.UNAVAILABLE):
        return meeting.attendance
    if meeting.is_teams and meeting.join_url and not meeting.is_all_day and has_contacts:
        return Attendance.PENDING
    return Attendance.NOT_APPLICABLE


def apply_calendars(read: dict, start: dt.datetime, end: dt.datetime) -> dict:
    """Write what was read. ``read`` maps a user to the events in their calendar."""
    index = contact_index()
    events: dict[str, GraphEvent] = {}
    for user_events in read.values():
        for event in user_events:
            events.setdefault(event.ical_uid, event)

    created = updated = 0
    meetings: dict[str, Meeting] = {}
    for uid, event in events.items():
        meeting, was_created = Meeting.objects.update_or_create(
            external_id=uid, defaults=meeting_values(event)
        )
        created += was_created
        updated += not was_created
        contacts = invited_contacts(event, index)
        meeting.contacts.set(contacts)
        status = _attendance_for(meeting, bool(contacts))
        if status != meeting.attendance:
            meeting.attendance = status
            meeting.save(update_fields=["attendance"])
        meetings[uid] = meeting

    removed = 0
    for user, user_events in read.items():
        seen = set()
        for event in user_events:
            meeting = meetings[event.ical_uid]
            seen.add(meeting.pk)
            CalendarEntry.objects.update_or_create(
                user=user,
                meeting=meeting,
                defaults={"event_id": event.event_id, "response": event.response[:32]},
            )
        gone = CalendarEntry.objects.filter(
            user=user, meeting__starts_at__lt=end, meeting__ends_at__gt=start
        ).exclude(meeting_id__in=seen)
        removed += gone.delete()[0]

    deleted, _ = Meeting.objects.filter(entries__isnull=True).delete()
    return {"meetings": len(events), "created": created, "updated": updated,
            "entries_removed": removed, "meetings_deleted": deleted}


# -- attendance -------------------------------------------------------------


def _author(meeting: Meeting):
    """Who the activity is recorded as: the organizer if they use the platform,
    else whoever on the platform has the meeting in their calendar."""
    User = get_user_model()
    if meeting.organizer_email:
        organizer = User.objects.filter(
            email__iexact=meeting.organizer_email, is_active=True
        ).first()
        if organizer is not None:
            return organizer
    entry = meeting.entries.select_related("user").order_by("id").first()
    return entry.user if entry else None


def log_attendance(meeting: Meeting, attended: list[Contact]) -> int:
    """One Meeting activity per account a joining contact belongs to."""
    author = _author(meeting)
    if author is None:
        logger.warning("Meeting %s has nobody to record it as; nothing logged", meeting.pk)
        return 0

    by_account: dict[int, list[Contact]] = defaultdict(list)
    for contact in attended:
        by_account[contact.account_id].append(contact)

    written = 0
    for account_id, contacts in by_account.items():
        contacts.sort(key=lambda c: c.full_name)
        try:
            with transaction.atomic():
                Activity.objects.create(
                    account_id=account_id,
                    contact=contacts[0],
                    activity_type=ActivityType.MEETING,
                    occurred_on=clock.to_business_date(meeting.starts_at),
                    subject=meeting.title[:255],
                    notes="Teams attendance report: " + ", ".join(c.full_name for c in contacts) + " joined.",
                    source=ActivitySource.TEAMS,
                    external_id=activity_external_id(meeting.pk, account_id),
                    created_by=author,
                )
        except IntegrityError:
            continue  # already logged by an earlier run
        written += 1
    return written


def check_attendance(client: GraphClient, now: dt.datetime) -> dict:
    """Read the reports of ended Teams meetings, then judge them in one transaction."""
    due = list(
        Meeting.objects.filter(attendance=Attendance.PENDING, ends_at__lte=now - REPORT_DELAY)
    )
    reports: dict[int, list[str] | None] = {}
    for meeting in due:
        if not meeting.organizer_email or not meeting.join_url:
            reports[meeting.pk] = None
            continue
        try:
            reports[meeting.pk] = client.attendance(meeting.organizer_email, meeting.join_url)
        except (GraphNotFound, GraphForbidden) as exc:
            logger.info("No attendance report for meeting %s: %s", meeting.pk, exc)
            reports[meeting.pk] = None

    counted = no_show = given_up = activities = 0
    with transaction.atomic():
        for meeting in due:
            emails = reports[meeting.pk]
            meeting.attendance_checked_at = now
            if emails is None:
                if now - meeting.ends_at > REPORT_GIVE_UP:
                    meeting.attendance = Attendance.UNAVAILABLE
                    given_up += 1
            else:
                joined = set(emails)
                meeting.attended_emails = sorted(joined)
                attended = [
                    c for c in meeting.contacts.filter(account__in=_live_accounts())
                    if c.email.strip().lower() in joined
                ]
                if attended:
                    activities += log_attendance(meeting, attended)
                    meeting.attendance = Attendance.COUNTED
                    counted += 1
                else:
                    meeting.attendance = Attendance.NO_SHOW
                    no_show += 1
            meeting.save(update_fields=["attendance", "attended_emails", "attendance_checked_at"])

    return {"checked": len(due), "counted": counted, "no_show": no_show,
            "no_report": given_up, "activities": activities}


# -- the run ------------------------------------------------------------------


def run(client: GraphClient, *, count_attendance: bool = True, now: dt.datetime | None = None) -> dict:
    now = now or clock.now()
    state, _ = SyncState.objects.get_or_create(provider=IntegrationProvider.TEAMS)
    state.last_started_at = now
    state.save(update_fields=["last_started_at"])

    start, end = window(now)
    User = get_user_model()
    try:
        read: dict = {}
        no_mailbox: list[str] = []
        forbidden: list[str] = []
        for user in User.objects.filter(is_active=True).order_by("email"):
            try:
                events = client.calendar_view(user.email, start, end)
            except GraphNotFound:
                no_mailbox.append(user.email)
                continue
            except GraphForbidden:
                forbidden.append(user.email)
                continue
            read[user] = [e for e in events if not e.is_cancelled]

        with transaction.atomic():
            report = apply_calendars(read, start, end)

        attendance = (
            check_attendance(client, now) if count_attendance
            else {"checked": 0, "counted": 0, "no_show": 0, "no_report": 0, "activities": 0}
        )
    except GraphError as exc:
        state.last_error = str(exc)
        state.save(update_fields=["last_error"])
        raise

    report.update(
        calendars_read=len(read),
        no_mailbox=no_mailbox,
        forbidden=forbidden,
        attendance=attendance,
    )
    state.last_success_at = now
    state.last_error = ""
    state.last_report = report
    state.save(update_fields=["last_success_at", "last_error", "last_report"])
    return report
