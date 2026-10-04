"""Meetings read from Microsoft 365 calendars.

A ``Meeting`` is a calendar entry: something planned, with a start and an end.
It is **not** an ``Activity`` and nothing in ``health/inputs.py`` reads it - a
meeting booked for next month must not count as contact that happened. When a
Teams meeting has ended and its attendance report shows a client contact
actually joined, the sync writes an ``Activity`` for it; that row, not this one,
is what the score sees. README.md, "Meetings, tasks and the Teams integration",
has the reasoning.

One ``Meeting`` per occurrence, keyed on Outlook's ``iCalUId``: the same
meeting in five people's calendars is one row with five ``CalendarEntry`` rows,
each holding that person's own event id. Who may see a meeting is exactly who
has it in their own calendar - nothing in ``core/access`` widens that.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.db import models

from csm.contacts.models import Contact
from csm.core import clock


class Attendance(models.TextChoices):
    """Whether this meeting counts as client contact, and why or why not."""

    NOT_APPLICABLE = "N/A", "Not a client Teams meeting"
    PENDING = "PENDING", "Waiting for the attendance report"
    COUNTED = "COUNTED", "Client attended - logged"
    NO_SHOW = "NO_SHOW", "No client contact joined"
    UNAVAILABLE = "UNAVAILABLE", "No attendance report"


#: Statuses the sync no longer revisits.
SETTLED = (Attendance.COUNTED, Attendance.NO_SHOW, Attendance.UNAVAILABLE)


class MeetingQuerySet(models.QuerySet):
    def for_user(self, user) -> MeetingQuerySet:
        """Meetings in this person's own calendar, and no one else's."""
        return self.filter(entries__user=user).distinct()

    def overlapping(self, start: dt.date, end: dt.date) -> MeetingQuerySet:
        """Meetings touching any business day from ``start`` to ``end``, inclusive."""
        return self.filter(
            starts_at__lt=clock.start_of_business_day(end + dt.timedelta(days=1)),
            ends_at__gt=clock.start_of_business_day(start),
        )


class Meeting(models.Model):
    external_id = models.CharField(
        max_length=255, unique=True, help_text="Outlook's iCalUId, the same in every calendar."
    )
    subject = models.CharField(max_length=255, blank=True, default="")
    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField()
    is_all_day = models.BooleanField(default=False)
    is_private = models.BooleanField(
        default=False,
        help_text="Marked private in Outlook. Subject and location are not stored.",
    )
    is_teams = models.BooleanField(default=False)
    join_url = models.TextField(blank=True, default="")
    web_link = models.TextField(blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    organizer_email = models.EmailField(blank=True, default="")
    organizer_name = models.CharField(max_length=255, blank=True, default="")

    #: ``[{"email": ..., "name": ..., "response": ...}]`` - who was invited, as
    #: Outlook lists them. An invitation is not attendance; that comes from the
    #: Teams attendance report and lands in ``attended_emails``.
    attendees = models.JSONField(default=list, blank=True)
    contacts = models.ManyToManyField(Contact, blank=True, related_name="meetings")

    attendance = models.CharField(
        max_length=12, choices=Attendance.choices, default=Attendance.NOT_APPLICABLE
    )
    attended_emails = models.JSONField(default=list, blank=True)
    attendance_checked_at = models.DateTimeField(null=True, blank=True)

    synced_at = models.DateTimeField(auto_now=True)

    objects = MeetingQuerySet.as_manager()

    class Meta:
        db_table = "meetings"
        ordering = ["starts_at", "id"]

    def __str__(self) -> str:
        return self.title

    @property
    def title(self) -> str:
        if self.is_private:
            return "Busy"
        return self.subject or "(no subject)"

    def local_start(self) -> dt.datetime:
        return self.starts_at.astimezone(clock.business_tz())

    def local_end(self) -> dt.datetime:
        return self.ends_at.astimezone(clock.business_tz())

    @property
    def start_time(self) -> dt.time:
        """Clock time in the business timezone. Templates get this rather than
        the instant: Django's ``time`` filter would render an aware datetime in
        ``TIME_ZONE``, which is UTC here."""
        return self.local_start().time()

    @property
    def end_time(self) -> dt.time:
        return self.local_end().time()

    @property
    def has_ended(self) -> bool:
        return self.ends_at <= clock.now()

    def days(self) -> list[dt.date]:
        """Every business day this meeting appears on.

        An all-day event is a run of calendar dates, not an interval of time:
        Outlook stores it as midnight to midnight in UTC, so converting it to
        Cairo would push it into the evening before. Its dates are read as
        written. A timed meeting ending exactly at midnight does not spill into
        the next day.
        """
        if self.is_all_day:
            first, last = self.starts_at.date(), self.ends_at.date() - dt.timedelta(days=1)
        else:
            first = self.local_start().date()
            last = (self.local_end() - dt.timedelta(microseconds=1)).date()
        last = max(first, last)
        return [first + dt.timedelta(days=n) for n in range((last - first).days + 1)]


class CalendarEntry(models.Model):
    """This meeting is in this person's Outlook calendar."""

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="entries")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_entries"
    )
    event_id = models.CharField(max_length=255)
    response = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        db_table = "meeting_calendar_entries"
        constraints = [
            models.UniqueConstraint(fields=["user", "meeting"], name="meeting_entry_once_per_user"),
        ]

    def __str__(self) -> str:
        return f"{self.meeting} in {self.user_id}'s calendar"


def activity_external_id(meeting_pk: int, account_pk: int) -> str:
    """The ``Activity.external_id`` a counted meeting writes for one account.

    Unique with ``source=TEAMS``, so writing it twice is an ``IntegrityError``
    rather than a second contact.
    """
    return f"meeting:{meeting_pk}:account:{account_pk}"


def meeting_pk_from_activity(external_id: str | None) -> int | None:
    """The reverse, for the calendar: hide the log line its meeting already shows."""
    parts = (external_id or "").split(":")
    if len(parts) == 4 and parts[0] == "meeting" and parts[1].isdigit():
        return int(parts[1])
    return None
