"""The calendar: the same work the Activities page lists, laid out by day.

It owns nothing. Every entry is a task, a logged activity, or a day a repeating
series will fall on - read through ``ability.accounts()`` and
``ability.visible_tasks()`` like every other surface, so an ACM's calendar has
no health alerts on it.

Three ranges, one page: a month to see the shape of the load, a week to plan
one, a day to work it.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import FormView, TemplateView

from csm.activities.models import Activity
from csm.core.access import capabilities as cap
from csm.core.clock import today
from csm.core.views import CapabilityRequiredMixin
from csm.schedule import grid, projection
from csm.schedule.forms import CalendarPreferenceForm
from csm.schedule.models import CalendarPreference

VIEWS = (("month", "Month"), ("week", "Week"), ("day", "Day"))


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

        accounts = self.ability.accounts()
        tasks = list(
            self.ability.visible_tasks()
            .filter(due_date__range=(start, end))
            .select_related("account", "contact", "series")
            .order_by("due_date", "due_time", "id")
        )
        activities = []
        if preference.show_happened:
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
        projected = (
            self._projected(accounts, start, end) if preference.show_projected else []
        )

        first = preference.first_weekday
        context["nav"] = "calendar"
        context["today"] = as_of
        context["focus"] = focus
        context["mode"] = mode
        context["modes"] = VIEWS
        context["preference"] = preference
        context["weekend"] = preference.weekend
        context["headings"] = grid.weekday_headings(first)
        context["by_day"] = self._by_day(tasks, activities, projected)
        context["weeks"] = (
            grid.month_grid(focus, first_weekday=first) if mode == "month" else []
        )
        context["days"] = self._days(focus, mode, preference)
        context["prev"], context["next"] = self._steps(focus, mode)
        context["load"] = len(tasks) + len(projected)
        return context

    # -- what the reader asked for ----------------------------------------

    def _mode(self, preference) -> str:
        wanted = self.request.GET.get("view", preference.default_view)
        return wanted if wanted in {value for value, _ in VIEWS} else preference.default_view

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

    @staticmethod
    def _by_day(tasks, activities, projected) -> dict:
        """One bucket per date, in the order a day is read: done, due, coming."""
        buckets = defaultdict(lambda: {"activities": [], "tasks": [], "projected": []})
        for activity in activities:
            buckets[activity.occurred_on]["activities"].append(activity)
        for task in tasks:
            buckets[task.due_date]["tasks"].append(task)
        for entry in projected:
            buckets[entry.when]["projected"].append(entry)
        return dict(buckets)
