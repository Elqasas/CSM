"""The landing page and the analytics dashboard.

Two distinct things. ``DashboardView`` is the working view a signed-in user
lands on: what is overdue, what has gone red, who has gone quiet.
``AnalyticsDashboardView`` is section 8 of the specification - the card grid with
one filter governing every card.

Both start from ``ability.accounts()``. The analytics filter is applied once,
before any card runs, so a card cannot widen its own scope: card builders receive
a queryset and never touch ``Account.objects``.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.views.generic import TemplateView

from csm.accounts.filters import (
    QUARTERS,
    RENEWAL_QUARTER_PARAM,
    RENEWAL_YEAR_PARAM,
    apply_renewal_parts,
    parse_quarter,
    parse_year,
    renewal_year_choices,
)
from csm.contacts.models import Contact
from csm.core.access import capabilities as cap
from csm.core.clock import local_time, today
from csm.core.enums import (
    AccountClass,
    Deployment,
    HealthStatus,
    LicenseType,
    LifecycleStage,
    TaskStatus,
)
from csm.core.views import AbilityMixin, CapabilityRequiredMixin
from csm.dashboard.cards import quarter_of, render_cards
from csm.users.models import DisplayPreference


def clock_reading(zone: str, *, twelve_hour: bool = False) -> str:
    """The time now in ``zone``, as the page's first paint shows it.

    Rendered server-side so the clock is correct before any script runs, and
    correct at all where scripts never do. An unknown zone falls back to the
    business one rather than raising: a wrong clock is a nuisance, a broken
    dashboard is not.

    The twelve-hour form is built by hand rather than with ``%I:%M %p``, whose
    output depends on the machine's locale - a server in one country would
    otherwise render a different string from the browser's own formatter.
    """
    import datetime as _dt
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        where = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        where = ZoneInfo(settings.APP_TIME_ZONE)

    now = _dt.datetime.now(tz=where)
    if not twelve_hour:
        return now.strftime("%H:%M")
    hour = now.hour % 12 or 12
    return f"{hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"


class DashboardView(AbilityMixin, TemplateView):
    """"My day": the reader's own queue. Also where sign-in and the logo land.

    A reader without ``myday.view`` - a manager, who owns no queue - is sent on
    to Analytics, so the landing URL stays one URL for everybody.
    """

    template_name = "dashboard/index.html"

    def get(self, request, *args, **kwargs):
        if not self.ability.can(cap.MYDAY_VIEW):
            return redirect("dashboard:analytics")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        as_of = today()
        accounts = self.ability.accounts()
        tasks = self.ability.visible_tasks()

        # The clock is a display preference and nothing else: `as_of` above -
        # the date every count on this page is made against - stays the
        # business date whatever zone the reader is sitting in.
        display = DisplayPreference.for_user(self.request.user)
        context["nav"] = "dashboard"
        context["today"] = as_of
        context["clock_timezone"] = display.effective_timezone
        context["clock_twelve_hour"] = display.twelve_hour_clock
        context["clock_now"] = clock_reading(
            display.effective_timezone, twelve_hour=display.twelve_hour_clock
        )
        context["clock_country"] = display.country
        context["business_timezone"] = settings.APP_TIME_ZONE
        context["account_total"] = accounts.count()

        # Grouped rather than one list ordered by date. "What is late" and
        # "what is today" are two different questions, and a flat list answers
        # neither without the reader comparing every badge to today's date.
        as_of_time = local_time()
        mine = list(
            tasks.for_user(self.request.user)
            .filter(status=TaskStatus.PENDING)
            .select_related("account")
            .order_by("due_date", "due_time")[:40]
        )
        overdue_mine = [t for t in mine if t.is_overdue(as_of, as_of_time)]
        today_mine = [
            t for t in mine if t.due_date == as_of and not t.is_overdue(as_of, as_of_time)
        ]
        context["task_groups"] = [
            {"label": "Overdue", "tone": "danger", "tasks": overdue_mine[:10]},
            {"label": "Today", "tone": "primary", "tasks": today_mine[:10]},
            {
                "label": "Coming up",
                "tone": "secondary",
                "tasks": [t for t in mine if t.due_date > as_of][:10],
            },
        ]
        context["my_open_total"] = len(mine)

        # Overdue work on accounts this user can see that is assigned to
        # somebody else. The card is "my open tasks" and stays that way, but an
        # overdue task on your own account that routed to a colleague - because
        # they held the account when it was raised - was invisible here and
        # looked like the card ignoring it.
        context["overdue_elsewhere"] = (
            tasks.overdue(as_of, as_of_time)
            .exclude(assignments__user=self.request.user)
            .distinct()
            .count()
        )
        context["overdue_count"] = tasks.overdue(as_of, local_time()).count()
        context["unassigned_count"] = tasks.unassigned().count()

        context["shows_health"] = self.ability.can(cap.HEALTH_VIEW)
        if context["shows_health"]:
            counts = accounts.aggregate(
                healthy=Count("pk", filter=Q(effective_health_status=HealthStatus.HEALTHY)),
                watch=Count("pk", filter=Q(effective_health_status=HealthStatus.WATCH)),
                at_risk=Count("pk", filter=Q(effective_health_status=HealthStatus.AT_RISK)),
                not_applicable=Count(
                    "pk", filter=Q(effective_health_status=HealthStatus.NOT_APPLICABLE)
                ),
            )
            context["health_counts"] = counts
            context["at_risk_accounts"] = (
                accounts.filter(effective_health_status=HealthStatus.AT_RISK)
                .order_by("red_since", "client_name")[:10]
            )

        context["going_quiet"] = (
            Contact.objects.filter(account__in=accounts)
            .select_related("account")
            .by_attention()[:8]
        )
        context["triage_count"] = (
            Contact.objects.filter(account__in=accounts).needs_triage().count()
        )
        return context


#: A chart segment stands for one stored value, so clicking it has to mean
#: exactly one filter. ``(query parameter, model field, the values it accepts)``.
CHART_FILTERS = (
    ("class", "account_class", AccountClass.values),
    ("deployment", "deployment", Deployment.values),
    ("license", "license_type", LicenseType.values),
    ("stage", "lifecycle_stage", LifecycleStage.values),
)

#: What a chart sends for a category stored as the empty string - the accounts
#: with no licence recorded, say. An empty query value already means "no filter",
#: so the absence needs a name of its own to be askable for.
UNSET = "none"


#: What the analytics filter form carries as hidden inputs: every parameter the
#: page reads that is not one of the form's own lists. Without them, changing
#: the country would drop the class button, a chart click, or a card's own
#: quarter.
HIDDEN_FILTER_PARAMS = (
    "status", "class", "rq", "deployment", "license", "stage", "managed",
    "quarter", "quarter_year", "renewal_year", "sort",
)


class AnalyticsDashboardView(CapabilityRequiredMixin, TemplateView):
    """Section 8: one filter, many cards, per-role card visibility."""

    required_capability = cap.DASHBOARD_VIEW
    template_name = "dashboard/analytics.html"

    #: How far back the engagement trend looks.
    TREND_MONTHS = 12

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        as_of = today()
        accounts = self._filtered_accounts()
        shows_health = self.ability.can(cap.HEALTH_VIEW)

        card_context = {
            "ability": self.ability,
            "accounts": accounts,
            "tasks": self.ability.visible_tasks().filter(account__in=accounts),
            "as_of": as_of,
            "as_of_time": local_time(),
            "shows_health": shows_health,
            "sort": self.request.GET.get("sort", ""),
            "months": self._months(as_of),
            "quarter": self._quarter(as_of),
            "quarter_year": self._quarter_year(as_of),
            "year_choices": self._year_choices(as_of),
            "renewal_year": self._renewal_year(as_of),
            "renewal_year_choices": self._renewal_year_choices(as_of),
        }

        context["nav"] = "analytics"
        context["today"] = as_of
        context["shows_health"] = shows_health
        context["cards"] = render_cards(self.ability, card_context)
        context["filters"] = self._filter_state()
        context["has_filters"] = any(
            value for key, value in context["filters"].items()
            if key not in ("quarter", "quarter_year")
        )
        context["countries"] = self._country_choices()
        context["active_filters"] = self._active_filters()
        context["carried_params"] = self._carried_params(context["active_filters"])
        context["csm_choices"] = self._owner_choices("csm")
        context["acm_choices"] = self._owner_choices("acm")
        context["hidden_filters"] = [
            (param, self.request.GET[param].strip())
            for param in HIDDEN_FILTER_PARAMS
            if self.request.GET.get(param, "").strip()
        ]
        context["quarters"] = QUARTERS
        context["renewal_years"] = renewal_year_choices(self.ability.accounts())
        context["account_classes"] = AccountClass.choices
        context["health_statuses"] = HealthStatus.choices
        return context

    # -- The shared filter ------------------------------------------------

    def _filtered_accounts(self):
        """One filter over every card, per the specification.

        Health status is offered only to users who may see health. Without that
        guard the filter would be an oracle: an ACM could compare result counts
        across statuses and read the distribution off the other cards.
        """
        qs = self.ability.accounts()

        country = self.request.GET.get("country", "").strip()
        if country:
            qs = qs.filter(country=country)

        # The charts are controls: a segment carries the value it stands for and
        # links back here with it. Every one of these is therefore reachable by
        # clicking as well as by typing, and each is checked against its own
        # choices rather than trusted.
        for param, field, allowed in CHART_FILTERS:
            value = self.request.GET.get(param, "").strip()
            if value == UNSET:
                qs = qs.filter(**{field: ""})
            elif value in allowed:
                qs = qs.filter(**{field: value})

        managed = self.request.GET.get("managed", "").strip()
        if managed in ("1", "0"):
            qs = qs.filter(is_managed_service=managed == "1")

        status = self.request.GET.get("status", "").strip()
        if status and self.ability.can(cap.HEALTH_VIEW):
            qs = qs.filter(effective_health_status=status)

        # Who owns the account. Ungated: an ACM already knows which accounts are
        # theirs, and the answer carries no health signal - it says who is
        # responsible, not how it is going.
        for param, field in (("csm", "csm"), ("acm", "acm")):
            value = self.request.GET.get(param, "").strip()
            if value == "none":
                qs = qs.filter(**{f"{field}__isnull": True})
            elif value.isdigit():
                qs = qs.filter(**{f"{field}_id": int(value)})

        qs = apply_renewal_parts(
            qs,
            self.request.GET.get(RENEWAL_QUARTER_PARAM),
            self.request.GET.get(RENEWAL_YEAR_PARAM),
        )

        return qs

    def _filter_state(self) -> dict:
        return {
            "country": self.request.GET.get("country", ""),
            "class": self.request.GET.get("class", ""),
            "status": self.request.GET.get("status", "")
            if self.ability.can(cap.HEALTH_VIEW)
            else "",
            "deployment": self.request.GET.get("deployment", ""),
            "license": self.request.GET.get("license", ""),
            "stage": self.request.GET.get("stage", ""),
            "managed": self.request.GET.get("managed", ""),
            "csm": self.request.GET.get("csm", ""),
            "acm": self.request.GET.get("acm", ""),
            "rq": self.request.GET.get(RENEWAL_QUARTER_PARAM, ""),
            "ry": self.request.GET.get(RENEWAL_YEAR_PARAM, ""),
            "quarter": self.request.GET.get("quarter", ""),
            "quarter_year": self.request.GET.get("quarter_year", ""),
        }

    def _active_filters(self) -> list:
        """What is currently narrowing the page, each removable on its own.

        Clicking a segment is only half a toggle. Two or three clicks can narrow
        the page to nothing, and an empty chart has no segment left to click
        back - the reader would be left with Clear filters, which throws away
        the other choices as well. These chips are the way back out of exactly
        one of them.
        """
        from django_countries.fields import Country

        labels = {
            "country": lambda v: Country(v).name or v,
            "class": dict(AccountClass.choices).get,
            "status": dict(HealthStatus.choices).get,
            "deployment": dict(Deployment.choices).get,
            "stage": dict(LifecycleStage.choices).get,
            "license": lambda v: (
                "No edition recorded" if v == UNSET else dict(LicenseType.choices).get(v)
            ),
            "managed": {"1": "Managed service", "0": "Not managed"}.get,
            "rq": lambda v: f"Renewing in Q{q}" if (q := parse_quarter(v)) else "",
            "ry": lambda v: f"Renewing in {y}" if (y := parse_year(v)) else "",
            "csm": lambda v: f"CSM: {self._person_name(v)}",
            "acm": lambda v: f"ACM: {self._person_name(v)}",
        }

        chips = []
        state = self._filter_state()
        for param, describe in labels.items():
            value = state.get(param, "")
            if not value:
                continue
            label = describe(value)
            if label:
                chips.append({"param": param, "value": value, "label": label})
        return chips

    def _carried_params(self, active_filters: list) -> list:
        """Everything a card's own form has to resubmit to leave the page as it is.

        The page filters, and also the *other* cards' controls. Two cards here
        carry a selector of their own - the renewals list's quarter and the
        renewal chart's year - and a form that resubmits only the page filters
        silently resets the other card: pick 2027 on the chart, change the
        quarter on the list, and the chart is back to this year with nobody
        having asked it to be.
        """
        carried = list(active_filters)
        for param in ("quarter", "quarter_year", "renewal_year", "sort"):
            value = self.request.GET.get(param, "").strip()
            if value:
                carried.append({"param": param, "value": value, "label": ""})
        return carried

    def _person_name(self, value: str) -> str:
        if value == UNSET:
            return "Unassigned"
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(pk=value).first() if value.isdigit() else None
        return (user.full_name or user.email) if user else ""

    def _owner_choices(self, field: str) -> list:
        """The people who hold accounts in this user's scope, and nobody else.

        Built from ``ability.accounts()`` rather than from the user table: a
        list of every CSM in the company would name colleagues whose portfolios
        this reader cannot see, and the size of that list is itself information
        the page has no reason to give away.
        """
        from django.contrib.auth import get_user_model

        scope = self.ability.accounts()
        ids = set(
            scope.exclude(**{f"{field}__isnull": True})
            .values_list(f"{field}_id", flat=True)
            .distinct()
        )
        people = [
            (str(user.pk), user.full_name or user.email)
            for user in get_user_model().objects.filter(pk__in=ids).order_by("full_name")
        ]
        # Only offered when it would match something. An option that always
        # returns nothing is a control that lies about the data.
        if scope.filter(**{f"{field}__isnull": True}).exists():
            people.append(("none", "Unassigned"))
        return people

    def _country_choices(self):
        codes = (
            self.ability.accounts()
            .exclude(country="")
            .values_list("country", flat=True)
            .distinct()
        )
        from django_countries.fields import Country

        return sorted(((code, Country(code).name) for code in codes), key=lambda p: p[1])

    # -- Card 4's own quarter selector ------------------------------------

    def _quarter(self, as_of) -> int:
        raw = self.request.GET.get("quarter", "")
        if raw.isdigit() and 1 <= int(raw) <= 4:
            return int(raw)
        return quarter_of(as_of)

    def _quarter_year(self, as_of) -> int:
        raw = self.request.GET.get("quarter_year", "")
        if raw.isdigit() and as_of.year - 5 <= int(raw) <= as_of.year + 5:
            return int(raw)
        return as_of.year

    @staticmethod
    def _year_choices(as_of) -> list:
        return list(range(as_of.year - 1, as_of.year + 3))

    # -- The renewal chart's own year -------------------------------------

    def _renewal_year(self, as_of) -> int:
        """Which year the quarter chart draws. This year unless asked otherwise.

        Validated against the years the reader actually has renewals in rather
        than against a span, so a hand-edited URL cannot produce an empty chart
        for a year nothing renews in.
        """
        raw = self.request.GET.get("renewal_year", "")
        choices = self._renewal_year_choices(as_of)
        if raw.isdigit() and int(raw) in choices:
            return int(raw)
        return as_of.year

    def _renewal_year_choices(self, as_of) -> list:
        """Every year this user has a contract ending in, plus the current one.

        From the data because a fixed span is wrong in both directions at once:
        it offers years the portfolio has nothing in, and stops short of the
        five-year contract that is the interesting one. The current year is
        always present so the default is always selectable.
        """
        from django.db.models.functions import ExtractYear

        years = set(
            self.ability.accounts()
            .exclude(contract_end_date__isnull=True)
            .annotate(renewal_year_num=ExtractYear("contract_end_date"))
            .values_list("renewal_year_num", flat=True)
            .distinct()
        )
        years.add(as_of.year)
        return sorted(years)

    # -- The trend axis ---------------------------------------------------

    def _months(self, as_of) -> list:
        """The last twelve month starts, oldest first, with short labels.

        Built from the business clock rather than the database so an empty month
        still appears on the axis. A trend that silently omits the months in
        which nothing happened is the flattering version of the truth.
        """
        months = []
        year, month = as_of.year, as_of.month
        for _ in range(self.TREND_MONTHS):
            months.append(dt.date(year, month, 1))
            month -= 1
            if month == 0:
                month = 12
                year -= 1
        months.reverse()
        return [(start, start.strftime("%b")) for start in months]
