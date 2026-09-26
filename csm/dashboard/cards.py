"""The dashboard card registry.

Every card declares what it needs rather than checking who is asking. A card
names a capability; the registry drops it when the user lacks it. That is how
"an ACM sees every card except Health Status distribution" is expressed without
any card, view or template reading a role - the rule falls out of
``HEALTH_VIEW`` being absent from an ACM's capability set, which is decided once
in ``core/access``.

The Overdue Tasks card is the one genuine per-role difference the capability
model does not express by itself: an ACM sees it filtered to renewal tasks. That
narrowing is written as "this viewer cannot see health", not as "this viewer is
an ACM", so it stays true if the roles are ever rearranged.

Card data functions take an already-scoped account queryset. They never widen
it, and they never call ``Account.objects``: the scoping and the shared filter
are applied once by the view before any card runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from django.db.models import Count, Q, Sum

from csm.core.access import capabilities as cap
from csm.core.enums import (
    AccountClass,
    Deployment,
    EngagementItemType,
    HealthStatus,
    LicenseType,
    LifecycleStage,
    ReviewStatus,
    TaskStatus,
    TaskType,
)
from csm.dashboard.charts import bars, donut, lines

# ---------------------------------------------------------------------------
# Palette tokens
# ---------------------------------------------------------------------------
# Names of CSS custom properties, not colours. The values live in app.css so the
# light and dark steps sit together and a card cannot invent a hue.

SERIES_1 = "--viz-series-1"
SERIES_2 = "--viz-series-2"

#: One hue, dark to light, for Class. VIP through D is a ranking, so the ramp
#: carries the ordering that five categorical hues would throw away. Validated
#: as an ordinal ramp: monotone lightness, every adjacent gap clear, and the
#: lightest step still visible against a white card.
CLASS_RAMP = (
    "--viz-rank-1", "--viz-rank-2", "--viz-rank-3", "--viz-rank-4", "--viz-rank-5",
)
STATUS_GOOD = "--viz-status-good"
STATUS_WARNING = "--viz-status-warning"
STATUS_CRITICAL = "--viz-status-critical"
STATUS_NEUTRAL = "--viz-status-neutral"


@dataclass(frozen=True)
class Card:
    id: str
    title: str
    kind: str  # stat | donut | bars | line | list
    build: Callable
    capability: str | None = None
    width: str = "col-12 col-md-6 col-xl-4"
    note: str = ""


@dataclass
class RenderedCard:
    card: Card
    data: dict

    @property
    def id(self) -> str:
        return self.card.id

    @property
    def title(self) -> str:
        return self.card.title

    @property
    def kind(self) -> str:
        return self.card.kind

    @property
    def width(self) -> str:
        return self.card.width

    @property
    def note(self) -> str:
        return self.card.note


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------


def _total_accounts(ctx) -> dict:
    return {"value": ctx["accounts"].count(), "caption": "in your portfolio"}


def _class_distribution(ctx) -> dict:
    counted = dict(
        ctx["accounts"].values_list("account_class").annotate(n=Count("pk"))
    )
    slices = [
        (label, counted.get(value, 0), CLASS_RAMP[index], value)
        for index, (value, label) in enumerate(AccountClass.choices)
    ]
    return {"chart": donut(slices), "filter_param": "class"}


def _health_distribution(ctx) -> dict:
    """Traffic lights, with the colour never carrying the meaning.

    Red against green is the pair a deuteranopic reader cannot separate, so each
    segment is directly labelled with its status and count and the same numbers
    are repeated as a table. The colour is a second channel, not the only one.
    """
    counted = dict(
        ctx["accounts"].values_list("effective_health_status").annotate(n=Count("pk"))
    )
    slices = [
        ("Healthy", counted.get(HealthStatus.HEALTHY, 0), STATUS_GOOD, HealthStatus.HEALTHY),
        ("Watch", counted.get(HealthStatus.WATCH, 0), STATUS_WARNING, HealthStatus.WATCH),
        (
            "At Risk",
            counted.get(HealthStatus.AT_RISK, 0),
            STATUS_CRITICAL,
            HealthStatus.AT_RISK,
        ),
        (
            "N/A",
            counted.get(HealthStatus.NOT_APPLICABLE, 0),
            STATUS_NEUTRAL,
            HealthStatus.NOT_APPLICABLE,
        ),
    ]
    not_scored = ctx["accounts"].filter(effective_health_status__isnull=True).count()
    if not_scored:
        # No key: "never calculated" is not one of the values the status filter
        # can express, and a segment that filtered to nothing would be worse
        # than one that does not respond at all.
        slices.append(("Not yet scored", not_scored, STATUS_NEUTRAL))
    return {"chart": donut(slices), "filter_param": "status"}


def _renewals_in_quarter(ctx) -> dict:
    """Card 4 carries its own quarter filter, independent of the page filter."""
    quarter = ctx["quarter"]
    year = ctx["quarter_year"]
    # Date lookups rather than the annotation: the rows are being listed, not
    # grouped, so there is nothing to gain from an extra column and the
    # `contract_end_date` index still applies.
    qs = (
        ctx["accounts"]
        .filter(contract_end_date__quarter=quarter, contract_end_date__year=year)
        .order_by("contract_end_date", "client_name")
    )
    return {
        "value": qs.count(),
        "caption": f"renewing in Q{quarter} {year}",
        "accounts": list(qs[:12]),
        "quarter": quarter,
        "quarter_year": year,
        "quarter_choices": [1, 2, 3, 4],
        "year_choices": ctx["year_choices"],
    }


def _renewal_year_quarters(ctx) -> dict:
    """The shape of one calendar year's renewals, Q1 to Q4.

    Beside the distribution donuts because it answers the same kind of question
    about the same filtered set - where the portfolio sits - rather than listing
    anything. The renewals card further down is the list: pick a quarter there
    and it names the accounts.

    Four counts in one aggregate rather than a group-by, and the quarters are
    written out even when empty: a year with nothing in Q3 should show an empty
    Q3, not skip from Q2 to Q4 and let the eye read a shorter year.
    """
    year = ctx["renewal_year"]
    in_year = ctx["accounts"].filter(contract_end_date__year=year)
    counted = in_year.aggregate(
        **{
            f"q{quarter}": Count("pk", filter=Q(contract_end_date__quarter=quarter))
            for quarter in (1, 2, 3, 4)
        }
    )
    rows = [
        (f"Q{quarter}", counted[f"q{quarter}"], f"{year}-{quarter}")
        for quarter in (1, 2, 3, 4)
    ]
    return {
        # Taller bars than the default: four of them, in a column beside three
        # 160px donuts, so the chart fills the same band of the row instead of
        # sitting in the top third of its card.
        "chart": bars(rows, width=280.0, bar_height=24.0, gap=16.0),
        "rows": [(label, n) for label, n, _ in rows],
        "token": SERIES_1,
        "filter_param": "renewal",
        "caption": f"Contract end dates in {year}: {sum(n for _, n, _ in rows)} in total.",
        "empty": f"No contract ends in {year} for this filter.",
        # The card's own year, like the renewals list's own quarter: the page
        # filter says which accounts, this says which year to draw. Offering a
        # fixed span instead would list years the portfolio has no renewals in
        # and leave out the ones it does.
        "year": year,
        "year_choices": ctx["renewal_year_choices"],
        "year_param": "renewal_year",
    }


def _managed_service(ctx) -> dict:
    total = ctx["accounts"].count()
    managed = ctx["accounts"].filter(is_managed_service=True).count()
    return {
        "value": managed,
        "caption": (
            f"of {total} accounts ({100 * managed // total}%)" if total else "no accounts"
        ),
        # The figure is also the control: clicking it narrows the page to the
        # managed accounts, clicking it again puts the page back.
        "filter_param": "managed",
        "filter_value": "1",
    }


def _country_distribution(ctx) -> dict:
    """Countries by account count, biggest first.

    This one *is* sorted by size, unlike Class and Lifecycle Stage: country has
    no inherent order, so magnitude is the only sequence available. Capped at ten
    bars, with the rest folded into a single row rather than dropped, so the
    total still reconciles with the accounts card.
    """
    from django_countries.fields import Country

    counted = (
        ctx["accounts"]
        .exclude(country="")
        .values_list("country")
        .annotate(n=Count("pk"))
        .order_by("-n", "country")
    )
    rows = [(Country(code).name or code, n, code) for code, n in counted]

    shown, rest = rows[:10], rows[10:]
    if rest:
        # "Other" is several countries at once, so it is not a filter value and
        # carries no key. It stays in the chart because dropping it would stop
        # the bars reconciling with the account count beside them.
        shown = shown + [("Other", sum(n for _, n, _ in rest), "")]
    # Still wider than the column it sits in - country names run long, and the
    # SVG scales down rather than clipping - but narrower than it was now that
    # this row holds four cards instead of three.
    return {
        "chart": bars(shown, width=320.0),
        "rows": [(label, n) for label, n, _ in rows],
        "token": SERIES_1,
        "filter_param": "country",
    }


def _total_users(ctx) -> dict:
    totals = ctx["accounts"].aggregate(
        n=Sum("total_users_count"), boq=Sum("boq_users_count"), added=Sum("added_users_count")
    )
    total = totals["n"] or 0
    return {
        "value": f"{total:,}",
        "caption": "licensed users across the portfolio",
        "breakdown": [
            ("BOQ users", f"{totals['boq'] or 0:,}"),
            ("Added users", f"{totals['added'] or 0:,}"),
        ],
    }


def _seat_split(ctx) -> dict:
    """Contracted seats against seats added since, across the portfolio.

    BOQ is what the client signed for; added users are what they grew into
    afterwards. The sum of the two is the Total users figure above, which on its
    own cannot say whether a portfolio is growing inside its contracts or simply
    sold large ones - and that is the question the split answers.
    """
    totals = ctx["accounts"].aggregate(
        boq=Sum("boq_users_count"), added=Sum("added_users_count")
    )
    boq = totals["boq"] or 0
    added = totals["added"] or 0
    slices = [
        ("Contracted (BOQ)", boq, SERIES_1),
        ("Added since", added, SERIES_2),
    ]
    return {
        "chart": donut(slices, unit="users"),
        "caption": (
            f"{added:,} of {boq + added:,} seats were added after the contract."
            if boq + added
            else "No seats recorded."
        ),
    }


def _deployment_split(ctx) -> dict:
    counted = dict(ctx["accounts"].values_list("deployment").annotate(n=Count("pk")))
    slices = [
        ("Cloud", counted.get(Deployment.CLOUD, 0), SERIES_1, Deployment.CLOUD),
        ("On-premises", counted.get(Deployment.ON_PREM, 0), SERIES_2, Deployment.ON_PREM),
    ]
    return {"chart": donut(slices), "filter_param": "deployment"}


def _reviews(ctx) -> dict:
    published = ctx["accounts"].aggregate(
        g2=Count("pk", filter=Q(g2_review_status=ReviewStatus.PUBLISHED)),
        gartner=Count("pk", filter=Q(gartner_review_status=ReviewStatus.PUBLISHED)),
        in_flight=Count(
            "pk",
            filter=Q(g2_review_status__in=[ReviewStatus.REQUESTED, ReviewStatus.IN_PROGRESS])
            | Q(gartner_review_status__in=[ReviewStatus.REQUESTED, ReviewStatus.IN_PROGRESS]),
        ),
    )
    return {
        "value": published["g2"] + published["gartner"],
        "caption": "published reviews",
        "breakdown": [
            ("G2", published["g2"]),
            ("Gartner", published["gartner"]),
            ("In progress", published["in_flight"]),
        ],
    }


def _license_distribution(ctx) -> dict:
    """Which product editions the portfolio is on.

    Accounts with no edition recorded get their own bar rather than being
    dropped: "we do not know" is a real answer about a portfolio, and hiding it
    would make the chart add up to fewer accounts than the total beside it.
    """
    counted = dict(ctx["accounts"].values_list("license_type").annotate(n=Count("pk")))
    rows = [(label, counted.get(value, 0), value) for value, label in LicenseType.choices]

    unset = counted.get("", 0)
    if unset:
        # "none" rather than the empty string it is stored as: an empty query
        # value means "no filter at all", so in a URL the two would be the same
        # thing.
        rows.append(("Not set", unset, "none"))
    return {
        "chart": bars(rows),
        "rows": [(label, n) for label, n, _ in rows],
        "token": SERIES_1,
        "filter_param": "license",
    }


def _lifecycle_distribution(ctx) -> dict:
    counted = dict(ctx["accounts"].values_list("lifecycle_stage").annotate(n=Count("pk")))
    rows = [(label, counted.get(value, 0), value) for value, label in LifecycleStage.choices]
    return {
        "chart": bars(rows),
        "rows": [(label, n) for label, n, _ in rows],
        "token": SERIES_1,
        "filter_param": "stage",
    }


def _engagement_trend(ctx) -> dict:
    """Card 11: events and digital content over the last twelve months.

    Both series are counts of the same kind of thing, so they share one y-scale.
    The pre-import scalar counts on Account are deliberately absent here - they
    carry no date and cannot be placed on a time axis, which is the whole reason
    EngagementItem exists.
    """
    from csm.engagement.models import EngagementItem

    months = ctx["months"]
    items = (
        EngagementItem.objects.filter(
            account__in=ctx["accounts"], occurred_on__gte=months[0][0]
        )
        .values_list("item_type", "occurred_on")
    )

    buckets = {
        EngagementItemType.EVENT: dict.fromkeys(range(len(months)), 0),
        EngagementItemType.DIGITAL_CONTENT: dict.fromkeys(range(len(months)), 0),
    }
    index_of = {start: position for position, (start, _) in enumerate(months)}
    for item_type, occurred_on in items:
        start = occurred_on.replace(day=1)
        position = index_of.get(start)
        if position is not None and item_type in buckets:
            buckets[item_type][position] += 1

    labels = [label for _, label in months]
    events = list(buckets[EngagementItemType.EVENT].values())
    content = list(buckets[EngagementItemType.DIGITAL_CONTENT].values())
    series = [
        ("Events", SERIES_1, events),
        ("Digital content", SERIES_2, content),
    ]
    return {
        "chart": lines(labels, series),
        # Built here rather than by pivoting inside the template. The table view
        # is the accessible equivalent of the chart, so it should be as hard to
        # get wrong as the chart is.
        "table": list(zip(labels, events, content, strict=True)),
    }


def _account_roster(ctx) -> dict:
    """The accounts themselves, by name.

    Every other card is an aggregate, which answers "how many" and leaves
    "which ones" hanging - and the shared filter makes that worse, because
    narrowing to Egypt and VIP changes every number on the page without ever
    saying whose. This card is the list those counts are counting.
    """
    from csm.accounts.filters import apply_sort
    from csm.core.access.presenters import AccountPresenter

    # Same ordering as the account list, because it is the same table: a column
    # that sorts on one page and does nothing on the other, under an identical
    # header, is worse than a table that does not sort at all.
    accounts = apply_sort(
        ctx["accounts"].select_related("csm", "acm"),
        ctx["sort"],
        shows_health=ctx["shows_health"],
    )
    shown = 60
    return {
        # Presenters, not models: the shared table asks the presenter for
        # anything health-shaped, which is what keeps the gating real on this
        # page as well as on the account list.
        "accounts": [
            AccountPresenter(account, ctx["ability"]) for account in accounts[:shown]
        ],
        "total": accounts.count(),
        "shown": shown,
        "shows_health": ctx["shows_health"],
    }


def _overdue_tasks(ctx) -> dict:
    """Card 12, narrowed for anyone who may not see health.

    An ACM gets renewal tasks only. Expressed as "cannot see health" rather than
    "is an ACM", so the rule survives the roles being rearranged, and because
    the reason is the same one everywhere else: an overdue health alert is a
    health signal.
    """
    # Through the queryset rather than a date comparison of its own, so this
    # card counts what the rest of the application calls overdue - including a
    # task whose time has passed today.
    tasks = ctx["tasks"].filter(status=TaskStatus.PENDING).overdue(
        ctx["as_of"], ctx["as_of_time"]
    )
    if not ctx["shows_health"]:
        tasks = tasks.filter(task_type=TaskType.AUTO_RENEWAL)

    by_owner = {}
    for task in tasks.select_related("account").prefetch_related("assignments__user"):
        owners = [a.user for a in task.assignments.all()] or [None]
        for owner in owners:
            key = owner.full_name if owner else "Unassigned"
            entry = by_owner.setdefault(key, {"owner": key, "total": 0, "types": {}})
            entry["total"] += 1
            label = TaskType(task.task_type).label
            entry["types"][label] = entry["types"].get(label, 0) + 1

    rows = sorted(by_owner.values(), key=lambda e: (-e["total"], e["owner"]))
    return {
        "value": tasks.count(),
        "caption": "overdue" + ("" if ctx["shows_health"] else ", renewals only"),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

FULL = "col-12"
WIDE = "col-12 col-xl-8"
THIRD = "col-12 col-md-6 col-xl-4"
#: The distribution row: three donuts and the renewal quarters, side by side on
#: a wide screen, two abreast on a tablet, stacked on a phone.
FOURTH = "col-12 col-md-6 col-xl-3"
QUARTER = "col-6 col-md-3"

REGISTRY = (
    Card("total_accounts", "Total accounts", "stat", _total_accounts, width=QUARTER),
    Card("total_users", "Total users", "stat", _total_users, width=QUARTER),
    Card("managed_service", "Managed service", "stat", _managed_service, width=QUARTER),
    Card("reviews", "Reviews", "stat", _reviews, width=QUARTER),
    Card(
        "health_distribution",
        "Health status",
        "donut",
        _health_distribution,
        capability=cap.HEALTH_VIEW,
        width=FOURTH,
        note="Colour is a second channel here: every segment is labelled, and the "
             "same numbers appear as a table, because red against green is the one "
             "pair a colour-blind reader cannot separate.",
    ),
    Card(
        "lifecycle_distribution", "Lifecycle stage", "bars",
        _lifecycle_distribution, width=FOURTH,
    ),
    Card("seat_split", "Seats: BOQ and added", "donut", _seat_split, width=FOURTH),
    Card("license_distribution", "License editions", "bars", _license_distribution, width=FOURTH),
    # Second row of four, then the roster of the accounts every one of these
    # eight cards is counting, directly beneath them.
    Card("country_distribution", "Countries", "bars", _country_distribution, width=FOURTH),
    Card("deployment_split", "Cloud vs on-premises", "donut", _deployment_split, width=FOURTH),
    Card(
        "renewal_year_quarters", "Renewal quarters", "bars",
        _renewal_year_quarters, width=FOURTH,
    ),
    Card("class_distribution", "Class distribution", "donut", _class_distribution, width=FOURTH),
    Card("account_roster", "Accounts in this view", "roster", _account_roster, width=FULL),
    Card(
        "engagement_trend",
        "Events and digital content",
        "line",
        _engagement_trend,
        width=WIDE,
    ),
    Card(
        "renewals_quarter",
        "Renewals by quarter",
        "renewals",
        _renewals_in_quarter,
        width="col-12 col-xl-4",
    ),
    # Full width because it is last: a half-width card with nothing beside it
    # leaves the grid ending on a ragged half-row.
    Card("overdue_tasks", "Overdue tasks by owner", "tasks", _overdue_tasks, width=FULL),
)


def visible_cards(ability):
    """The cards this user may see, in registry order."""
    return tuple(
        card
        for card in REGISTRY
        if card.capability is None or ability.can(card.capability)
    )


def render_cards(ability, ctx) -> list:
    return [RenderedCard(card=card, data=card.build(ctx)) for card in visible_cards(ability)]


def quarter_of(day: date) -> int:
    return (day.month - 1) // 3 + 1
