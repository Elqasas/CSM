"""The calendar: the same work the Activities page lists, laid out by day.

It owns nothing. Every entry is a task, a logged activity, a day a repeating
series will fall on - read through ``ability.accounts()`` and
``ability.visible_tasks()`` like every other surface, so an ACM's calendar has
no health alerts on it - or a meeting from the reader's own Outlook calendar.
Meetings are scoped by whose calendar holds them, never by role: nobody sees a
colleague's meetings, a manager included.

Three ranges, one page: a month to see the shape of the load, a week to plan
one, a day to work it. Week and day are drawn by the hour; anything without a
clock time sits in the all-day row above the hours.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import FormView, TemplateView

from csm.activities.models import Activity
from csm.core.access import capabilities as cap
from csm.core.clock import local_time, today
from csm.core.views import CapabilityRequiredMixin
from csm.integrations.teams import settings as teams
from csm.meetings.models import Meeting, meeting_pk_from_activity
from csm.schedule import grid, projection
from csm.schedule.forms import CalendarPreferenceForm
from csm.schedule.models import CalendarPreference

VIEWS = (("month", "Month"), ("week", "Week"), ("day", "Day"))

#: Where an entry came from. "Platform" is everything Bousla itself holds -
#: tasks, the activity log, projected repeats; "Microsoft 365" is the reader's
#: own Outlook calendar.
SOURCES = (("all", "All"), ("platform", "Platform"), ("microsoft", "Microsoft 365"))

#: The hour the time grid opens scrolled to, unless something starts earlier.
WORKDAY_START = 8


class CalendarSettingsView(CapabilityRequiredMixin, FormView):
    """How this reader wants their own calendar drawn.

    Preferences, not permissions: nothing here widens what anybody may see, so
    it needs no capability beyond the one that reaches the calendar at all.
    """

    required_capability = cap.ACTIVITY_VIEW
    template_name = "schedule/settings.html"
    form_class = CalendarPreferenceForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = CalendarPreference.for_user(self.request.user)
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "calendar"
        return context

    def form_valid(self, form):
        form.save()
        messages.success(self.request, "Calendar settings saved.")
        return redirect("schedule:calendar")


class CalendarView(CapabilityRequiredMixin, TemplateView):
    required_capability = cap.ACTIVITY_VIEW
    template_name = "schedule/calendar.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        preference = CalendarPreference.for_user(self.request.user)
        as_of = today()
        focus = self._focus(as_of)
        mode = self._mode(preference)
        start, end = self._range(focus, mode, preference)

        source = self._source()
        platform = source in ("all", "platform")
        microsoft = source in ("all", "microsoft")

        accounts = self.ability.accounts()
        tasks = []
        if platform:
            tasks = list(
                self.ability.visible_tasks()
                .filter(due_date__range=(start, end))
                .select_related("account", "contact", "series")
                .order_by("due_date", "due_time", "id")
            )
        meetings = self._meetings(accounts, start, end) if microsoft else []
        activities = []
        if platform and preference.show_happened:
            activities = list(
                self.ability.visible_activities(
                    Activity.objects.filter(
                        account__in=accounts,
                        is_voided=False,
                        occurred_on__range=(start, end),
                    )
                )
                .select_related("account", "contact")
                .order_by("occurred_on", "id")
            )
            # A counted Teams meeting is already on the page as the meeting;
            # its log line beside it would show the same hour twice.
            shown = {meeting.pk for meeting in meetings}
            activities = [
                a for a in activities if meeting_pk_from_activity(a.external_id) not in shown
            ]
        projected = (
            self._projected(accounts, start, end)
            if platform and preference.show_projected
            else []
        )

        first = preference.first_weekday
        by_day = self._by_day(tasks, activities, projected, meetings)
        days = self._days(focus, mode, preference)
        columns = self._columns(days, by_day, as_of)

        context["nav"] = "calendar"
        context["today"] = as_of
        context["focus"] = focus
        context["mode"] = mode
        context["modes"] = VIEWS
        context["source"] = source
        context["sources"] = SOURCES
        context["microsoft_on"] = teams.current().enabled or bool(meetings)
        context["preference"] = preference
        context["weekend"] = preference.weekend
        context["headings"] = grid.weekday_headings(first)
        context["by_day"] = by_day
        context["weeks"] = (
            grid.month_grid(focus, first_weekday=first) if mode == "month" else []
        )
        context["days"] = days
        context["columns"] = columns
        context["hours"] = range(24)
        context["scroll_to"] = self._scroll_to(columns)
        context["now_top"] = round(grid.minutes(local_time()) / grid.MINUTES_PER_DAY * 100, 4)
        context["prev"], context["next"] = self._steps(focus, mode)
        context["load"] = len(tasks) + len(projected)
        context["meeting_load"] = len(meetings)
        return context

    # -- what the reader asked for ----------------------------------------

    def _mode(self, preference) -> str:
        wanted = self.request.GET.get("view", preference.default_view)
        return wanted if wanted in {value for value, _ in VIEWS} else preference.default_view

    def _source(self) -> str:
        wanted = self.request.GET.get("source", "all")
        return wanted if wanted in {value for value, _ in SOURCES} else "all"

    def _focus(self, as_of) -> dt.date:
        """The day the view is centred on. Today unless a readable date says otherwise."""
        raw = self.request.GET.get("date", "")
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            return as_of

    @staticmethod
    def _range(focus: dt.date, mode: str, preference):
        first = preference.first_weekday
        if mode == "day":
            return focus, focus
        if mode == "week":
            week = grid.week_of(focus, first_weekday=first)
            return week[0], week[-1]
        weeks = grid.month_grid(focus, first_weekday=first)
        return weeks[0][0].date, weeks[-1][-1].date

    @staticmethod
    def _days(focus: dt.date, mode: str, preference):
        if mode == "day":
            return [focus]
        if mode == "week":
            days = grid.week_of(focus, first_weekday=preference.first_weekday)
            if not preference.show_weekends:
                days = [day for day in days if day.weekday() not in preference.weekend]
            return days
        return []

    @staticmethod
    def _steps(focus: dt.date, mode: str):
        if mode == "day":
            return focus - dt.timedelta(days=1), focus + dt.timedelta(days=1)
        if mode == "week":
            return focus - dt.timedelta(weeks=1), focus + dt.timedelta(weeks=1)
        return grid.add_months(focus, -1), grid.add_months(focus, 1)

    # -- the entries ------------------------------------------------------

    def _projected(self, accounts, start, end):
        """Where the repeats will fall, without writing any of them down."""
        from csm.tasks.models import TaskSeries

        series = list(
            TaskSeries.objects.filter(is_active=True, account__in=accounts)
            .select_related("account")
        )
        if not series:
            return []

        taken = defaultdict(set)
        for series_id, due in (
            self.ability.visible_tasks()
            .filter(series__in=series)
            .values_list("series_id", "due_date")
        ):
            taken[series_id].add(due)

        return projection.project_all(series, start=start, end=end, taken_by_series=taken)

    def _meetings(self, accounts, start, end) -> list:
        """The reader's own Outlook meetings in range, each labelled with the
        clients it involves - only clients the reader may see."""
        meetings = list(
            Meeting.objects.for_user(self.request.user)
            .overlapping(start, end)
            .prefetch_related("contacts__account")
            .order_by("starts_at", "id")
        )
        visible = set(accounts.values_list("pk", flat=True))
        for meeting in meetings:
            meeting.clients = sorted({
                contact.account.client_name
                for contact in meeting.contacts.all()
                if contact.account_id in visible
            })
        return meetings

    @staticmethod
    def _by_day(tasks, activities, projected, meetings=()) -> dict:
        """One bucket per date, in the order a day is read: done, met, due, coming."""
        buckets = defaultdict(
            lambda: {"activities": [], "meetings": [], "tasks": [], "projected": []}
        )
        for activity in activities:
            buckets[activity.occurred_on]["activities"].append(activity)
        for meeting in meetings:
            for day in meeting.days():
                buckets[day]["meetings"].append(meeting)
        for task in tasks:
            buckets[task.due_date]["tasks"].append(task)
        for entry in projected:
            buckets[entry.when]["projected"].append(entry)
        return dict(buckets)

    @staticmethod
    def _columns(days, by_day, as_of) -> list[dict]:
        """Each day of a week or day view, split into its all-day row and its hours.

        Whatever has a clock time goes into the hours: a timed task as half an
        hour from its due time, a projected repeat the same, a meeting from its
        start to its end - clipped to this day, so one running past midnight
        shows on both. Everything else (the activity log, untimed tasks, all-day
        events) stays in the row above.
        """
        columns = []
        for day in days:
            bucket = by_day.get(day) or {}
            all_day = {"activities": list(bucket.get("activities", [])),
                       "meetings": [], "tasks": [], "projected": []}
            spans = []
            for task in bucket.get("tasks", []):
                if task.due_time is None:
                    all_day["tasks"].append(task)
                else:
                    start = grid.minutes(task.due_time)
                    spans.append(grid.Span(start, start + grid.TASK_MINUTES, "task", task))
            for entry in bucket.get("projected", []):
                if entry.series.due_time is None:
                    all_day["projected"].append(entry)
                else:
                    start = grid.minutes(entry.series.due_time)
                    spans.append(grid.Span(start, start + grid.TASK_MINUTES, "projected", entry))
            for meeting in bucket.get("meetings", []):
                if meeting.is_all_day:
                    all_day["meetings"].append(meeting)
                    continue
                local_start, local_end = meeting.local_start(), meeting.local_end()
                start = grid.minutes(local_start) if local_start.date() == day else 0
                end = (grid.minutes(local_end) if local_end.date() == day
                       else grid.MINUTES_PER_DAY)
                spans.append(grid.Span(start, max(end, start), "meeting", meeting))
            columns.append({
                "date": day,
                "is_today": day == as_of,
                "all_day": all_day,
                "has_all_day": any(all_day.values()),
                "placed": grid.lay_out(spans),
            })
        return columns

    @staticmethod
    def _scroll_to(columns) -> int:
        """Minutes past midnight the hours open scrolled to: the working day's
        start, or an hour before the earliest timed entry if that is earlier."""
        starts = [placed.span.start for column in columns for placed in column["placed"]]
        earliest = min(starts, default=WORKDAY_START * 60)
        if earliest < WORKDAY_START * 60:
            return max(earliest - 60, 0)
        return WORKDAY_START * 60
