"""Logging and viewing activity.

Logging an activity is what moves a health score, via the model-level hooks in
``health/signals.py``. Nothing here calls the scoring engine: the view saves the
row and the signal does the rest, which is exactly why the hook lives at the
model layer rather than in a view.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import FormView, TemplateView

from csm.activities import filters, timeline
from csm.activities.forms import ActivityForm, ActivityVoidForm
from csm.activities.models import Activity
from csm.core.access import capabilities as cap
from csm.core.clock import local_time, now, today
from csm.core.views import AccountScopedMixin, CapabilityRequiredMixin, safe_next


class KindChoicesMixin:
    """Which task kinds the filter offers.

    A health alert is a health signal, so an ACM is not offered one to filter
    by - the same rule ``visible_tasks`` already applies to the rows, said again
    where the control is built. Offering it would leak that the kind exists.
    """

    def _kind_choices(self):
        from csm.core.enums import TaskType

        return [
            (value, label)
            for value, label in TaskType.choices
            if value != TaskType.AUTO_HEALTH_ALERT or self.ability.can(cap.HEALTH_VIEW)
        ]


def read_filters(request, *, default_period: str) -> dict:
    """What the reader asked for, defaults filled in.

    One function for both pages so a filter cannot work on the portfolio view
    and quietly do nothing on a client's own.
    """
    from csm.core.enums import TaskType

    nature = request.GET.get("nature", "all")
    if nature not in {value for value, _ in filters.NATURES}:
        nature = "all"

    period = request.GET.get("period", default_period)
    if period not in {value for value, _, _ in filters.PERIODS}:
        period = default_period

    kind = request.GET.get("kind", "").strip()
    if kind not in TaskType.values:
        kind = ""

    return {
        "nature": nature,
        "period": period,
        "mine": request.GET.get("mine") == "1",
        "kind": kind,
        "q": request.GET.get("q", "").strip(),
        "account": request.GET.get("account", "").strip(),
        "include_voided": request.GET.get("include_voided") == "1",
    }


class ActivityListView(KindChoicesMixin, CapabilityRequiredMixin, TemplateView):
    """Everything across the portfolio: what happened, and what is planned.

    The same merge as one client's timeline, over every account this reader can
    see. Both go through ``timeline.build`` so the two pages cannot start
    describing the same week differently.

    Not paginated. A merged stream needs a cursor per source to page correctly,
    and a wrong second page is harder to notice than a short one - so it takes
    the most recent slice of each and says so.
    """

    required_capability = cap.ACTIVITY_VIEW
    template_name = "activities/activity_list.html"
    LIMIT = 60

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        accounts = self.ability.accounts()
        as_of = today()

        state = read_filters(self.request, default_period=filters.DEFAULT_PERIOD)
        since = filters.cutoff(state["period"], today=as_of)
        mine_for = self.request.user if state["mine"] else None
        account_id = state["account"] if str(state["account"]).isdigit() else ""

        activities = []
        if filters.wants_activities(state["nature"]):
            found = filters.narrow_activities(
                self.ability.visible_activities(
                    Activity.objects.filter(account__in=accounts)
                ),
                since=since, account_id=account_id, term=state["q"],
                mine_for=mine_for,
            ).select_related("account", "contact", "created_by")
            if not state["include_voided"]:
                found = found.filter(is_voided=False)
            activities = list(found.order_by("-occurred_on", "-id")[: self.LIMIT])

        tasks = []
        if filters.wants_tasks(state["nature"]):
            tasks = list(
                filters.narrow_tasks(
                    self.ability.visible_tasks(),
                    nature=state["nature"], since=since, account_id=account_id,
                    term=state["q"], mine_for=mine_for, kind=state["kind"],
                    as_of=as_of, as_of_time=local_time(),
                )
                .select_related("account", "contact")
                .order_by("-due_date", "-id")[: self.LIMIT]
            )

        entries = timeline.build(activities, tasks)
        context["nav"] = "activity"
        context["entries"] = entries
        context["filters"] = state
        context["natures"] = filters.NATURES
        context["periods"] = [(value, label) for value, label, _ in filters.PERIODS]
        context["kinds"] = self._kind_choices()
        context["account_choices"] = accounts.order_by("client_name").values_list(
            "pk", "client_name"
        )
        context["has_filters"] = any(
            [state["q"], account_id, state["kind"], state["mine"],
             state["nature"] != "all",
             state["period"] != filters.DEFAULT_PERIOD, state["include_voided"]]
        )
        context["truncated"] = (
            len(activities) == self.LIMIT or len(tasks) == self.LIMIT
        )
        context["limit"] = self.LIMIT
        context["today"] = as_of
        return context


class AccountTimelineView(KindChoicesMixin, AccountScopedMixin, TemplateView):
    """One client, everything that touched them, on one line.

    Three streams read together and written nowhere: activities, which are the
    record the scoring engine reads, and tasks, which are obligations - meetings
    and calls among them. They stay separate tables for the reason in README.md;
    this is the interface answer to wanting them in one place.

    Tasks come from ``ability.visible_tasks()`` rather than ``Task.objects``, so
    an ACM's timeline has no health alerts in it. Without that the page would
    leak the existence of an alert, which is itself a health signal.
    """

    required_capability = cap.ACTIVITY_VIEW
    template_name = "activities/timeline.html"
    account_url_kwarg = "account_pk"

    #: Enough to read a year of a busy account without paginating a merged list,
    #: which would need one cursor per stream to be correct.
    LIMIT = 100

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        as_of = today()

        # One client, so the default reaches back further than the portfolio
        # page: the question here is "what has been going on with them", which
        # a ninety-day window answers badly for a quiet account.
        state = read_filters(self.request, default_period="all")
        since = filters.cutoff(state["period"], today=as_of)
        mine_for = self.request.user if state["mine"] else None

        activities = []
        if filters.wants_activities(state["nature"]):
            found = filters.narrow_activities(
                self.ability.visible_activities(
                    Activity.objects.filter(account=self.account)
                ),
                since=since, account_id="", term=state["q"],
                mine_for=mine_for,
            ).select_related("contact", "created_by")
            if not state["include_voided"]:
                found = found.filter(is_voided=False)
            activities = list(found.order_by("-occurred_on", "-id")[: self.LIMIT])

        tasks = []
        if filters.wants_tasks(state["nature"]):
            tasks = list(
                filters.narrow_tasks(
                    self.ability.visible_tasks().filter(account=self.account),
                    nature=state["nature"], since=since, account_id="",
                    term=state["q"], mine_for=mine_for, kind=state["kind"],
                    as_of=as_of, as_of_time=local_time(),
                )
                .select_related("contact")
                .order_by("-due_date", "-id")[: self.LIMIT]
            )

        entries = timeline.build(activities, tasks)
        context["nav"] = "activity"
        context["entries"] = entries
        context["filters"] = state
        context["natures"] = filters.NATURES
        context["periods"] = [(value, label) for value, label, _ in filters.PERIODS]
        context["kinds"] = self._kind_choices()
        context["has_filters"] = any(
            [state["q"], state["kind"], state["mine"],
             state["nature"] != "all", state["period"] != "all",
             state["include_voided"]]
        )
        context["today"] = as_of
        return context


class ActivityCreateView(AccountScopedMixin, FormView):
    required_capability = cap.ACTIVITY_CREATE
    template_name = "activities/activity_form.html"
    form_class = ActivityForm
    account_url_kwarg = "account_pk"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["account"] = self.account
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "accounts"
        context["next"] = safe_next(self.request)
        return context

    def form_valid(self, form):
        activity = form.save(commit=False)
        activity.account = self.account
        activity.created_by = self.request.user
        activity.save()
        messages.success(self.request, "Activity logged.")
        return redirect(
            safe_next(self.request) or reverse("accounts:detail", args=[self.account.pk])
        )


class ActivityVoidView(CapabilityRequiredMixin, FormView):
    """Void rather than delete.

    The activity log is append-only: a health score was calculated from these
    rows and deleting one would make an old history row unexplainable. Voiding
    records who did it and why, and the scoring queries skip voided rows, so the
    score corrects itself on the next recalculation.
    """

    required_capability = cap.ACTIVITY_VOID
    template_name = "activities/activity_confirm_void.html"
    form_class = ActivityVoidForm

    def get_activity(self):
        return get_object_or_404(
            Activity.objects.filter(account__in=self.ability.accounts()),
            pk=self.kwargs["pk"],
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "accounts"
        context["activity"] = self.get_activity()
        return context

    def form_valid(self, form):
        activity = self.get_activity()
        activity.is_voided = True
        activity.voided_by = self.request.user
        activity.voided_at = now()
        activity.void_reason = form.cleaned_data["void_reason"]
        activity.save(
            update_fields=["is_voided", "voided_by", "voided_at", "void_reason"]
        )
        messages.success(self.request, "Activity voided.")
        return redirect("accounts:detail", pk=activity.account_id)
