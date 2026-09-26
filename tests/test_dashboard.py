"""The analytics dashboard: geometry, the card registry, and the per-role rules.

The geometry tests need no database at all, for the same reason the scoring
engine's tests do not: ``dashboard/charts.py`` is pure functions over numbers.
The edge cases asserted here are the ones that produce a blank chart rather than
an exception, which is the failure mode nobody notices.

The registry tests are the ones that matter for correctness. Specification
section 8 says an ACM sees every card except Health Status distribution, and
that their Overdue Tasks card is filtered to renewal tasks only. Both are
asserted here against the real registry rather than against a comment.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.test import RequestFactory
from django.urls import reverse

from csm.core.access.abilities import Ability
from csm.core.enums import (
    AccountClass,
    Deployment,
    EngagementItemType,
    HealthStatus,
    TaskType,
)
from csm.dashboard import cards as card_module
from csm.dashboard.charts import bars, donut, lines
from csm.engagement.models import EngagementItem
from csm.tasks.models import Task
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login

# ---------------------------------------------------------------------------
# Geometry — no database
# ---------------------------------------------------------------------------


def test_a_donut_of_one_full_slice_still_draws():
    """A single 360-degree arc starts and ends on the same point and renders
    nothing at all, so it has to be split. A portfolio that is entirely Healthy
    is the outcome everyone is working toward, not a corner case."""
    chart = donut([("Healthy", 12, "--viz-status-good")])

    assert chart.total == 12
    assert len(chart.segments) == 1
    segment = chart.segments[0]
    assert segment.percent == pytest.approx(100.0)
    # Two arc commands: the split into halves.
    assert segment.path.count("M ") == 2


def test_a_donut_drops_empty_slices():
    chart = donut(
        [
            ("Healthy", 3, "--viz-status-good"),
            ("Watch", 0, "--viz-status-warning"),
            ("At Risk", 1, "--viz-status-critical"),
        ]
    )
    assert [s.label for s in chart.segments] == ["Healthy", "At Risk"]


def test_an_all_zero_donut_is_empty_rather_than_a_divide_by_zero():
    chart = donut([("Healthy", 0, "--viz-status-good")])
    assert chart.is_empty
    assert chart.segments == ()


def test_donut_percentages_sum_to_one_hundred():
    chart = donut(
        [("a", 1, "--x"), ("b", 1, "--x"), ("c", 1, "--x")]
    )
    assert sum(s.percent for s in chart.segments) == pytest.approx(100.0)


def test_bars_keep_the_order_they_are_given():
    """Class runs VIP to D. Sorting by count would destroy the sequence."""
    rows = [("VIP", 2), ("A", 9), ("B", 4)]
    chart = bars(rows)
    assert [bar.label for bar in chart.bars] == ["VIP", "A", "B"]


def test_a_zero_bar_has_no_path_but_keeps_its_label_and_value():
    chart = bars([("VIP", 0), ("A", 5)])
    vip, a = chart.bars
    assert vip.path == ""
    assert vip.value == 0
    assert a.path != ""


def test_bars_with_no_data_at_all_do_not_divide_by_zero():
    chart = bars([("VIP", 0), ("A", 0)])
    assert chart.max_value == 0
    assert all(bar.path == "" for bar in chart.bars)


def test_the_longest_bar_is_the_maximum():
    chart = bars([("A", 5), ("B", 10)])
    a, b = chart.bars
    assert b.value_x > a.value_x


def test_a_line_chart_shares_one_scale_across_series():
    """Two y-scales would let the author choose which line looks higher."""
    chart = lines(
        ["Jan", "Feb"],
        [("Events", "--viz-series-1", [1, 2]), ("Content", "--viz-series-2", [10, 20])],
    )
    events, content = chart.series
    # The small series sits lower on the plot (larger y) than the big one.
    assert events.points[1].y > content.points[1].y
    assert chart.max_value >= 20


def test_a_line_chart_of_all_zeroes_is_empty_rather_than_flat_at_the_top():
    chart = lines(["Jan", "Feb"], [("Events", "--viz-series-1", [0, 0])])
    assert chart.is_empty


def test_line_axis_maximum_is_rounded_to_something_sayable():
    chart = lines(["Jan"], [("Events", "--viz-series-1", [7])])
    assert chart.max_value == 10


# ---------------------------------------------------------------------------
# The registry and the per-role rules
# ---------------------------------------------------------------------------


@pytest.fixture
def dash_people(db):
    users = {
        "csm": UserFactory(csm=True, email="dash-csm@dphish.com", full_name="Casey Morgan"),
        "acm": UserFactory(acm=True, email="dash-acm@dphish.com", full_name="Alex Chen"),
        "manager": UserFactory(
            manager=True, email="dash-manager@dphish.com", full_name="Morgan Lee"
        ),
    }
    for user in users.values():
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    return users


@pytest.fixture
def dash_portfolio(dash_people):
    return AccountFactory(
        client_code="DASH1",
        client_name="Acme Industrial",
        csm=dash_people["csm"],
        acm=dash_people["acm"],
        account_class=AccountClass.VIP,
        deployment=Deployment.CLOUD,
        effective_health_status=HealthStatus.AT_RISK,
        contract_end_date=dt.date(2026, 11, 30),
    )


def card_ids(user) -> list:
    return [card.id for card in card_module.visible_cards(Ability(user))]


def test_a_csm_sees_every_card(dash_people):
    assert "health_distribution" in card_ids(dash_people["csm"])
    assert len(card_ids(dash_people["csm"])) == len(card_module.REGISTRY)


def test_an_acm_sees_every_card_except_health_distribution(dash_people):
    """Specification section 8, expressed through capabilities rather than roles."""
    ids = card_ids(dash_people["acm"])
    assert "health_distribution" not in ids
    assert set(ids) == {c.id for c in card_module.REGISTRY} - {"health_distribution"}


def test_a_manager_sees_every_card(dash_people):
    assert "health_distribution" in card_ids(dash_people["manager"])


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["csm", "acm", "manager"])
def test_the_analytics_page_renders_for_every_role(client, dash_portfolio, dash_people, role):
    login(client, dash_people[role])
    assert client.get(reverse("dashboard:analytics")).status_code == 200


def test_the_analytics_page_is_scoped_to_the_users_accounts(client, dash_portfolio, dash_people):
    AccountFactory(
        client_code="OTHER",
        client_name="Zenith Holdings",
        csm=UserFactory(csm=True, email="dash-other@dphish.com"),
    )
    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:analytics"))

    totals = {c.id: c.data for c in response.context["cards"]}
    assert totals["total_accounts"]["value"] == 1


def test_an_acm_gets_no_health_filter_and_cannot_apply_one(client, dash_portfolio, dash_people):
    """The filter would otherwise be an oracle for the card the ACM cannot see."""
    AccountFactory(
        client_code="DASH2",
        client_name="Beta Systems",
        csm=dash_people["csm"],
        acm=dash_people["acm"],
        effective_health_status=HealthStatus.HEALTHY,
    )
    login(client, dash_people["acm"])
    response = client.get(
        reverse("dashboard:analytics"), {"status": HealthStatus.AT_RISK}
    )

    assert response.context["shows_health"] is False
    assert "Health status" not in response.content.decode()
    # The filter was ignored, so both accounts are still counted.
    totals = {c.id: c.data for c in response.context["cards"]}
    assert totals["total_accounts"]["value"] == 2


def test_a_csm_can_apply_the_health_filter(client, dash_portfolio, dash_people):
    AccountFactory(
        client_code="DASH3",
        csm=dash_people["csm"],
        effective_health_status=HealthStatus.HEALTHY,
    )
    login(client, dash_people["csm"])
    response = client.get(
        reverse("dashboard:analytics"), {"status": HealthStatus.AT_RISK}
    )
    totals = {c.id: c.data for c in response.context["cards"]}
    assert totals["total_accounts"]["value"] == 1


def test_an_acms_overdue_card_shows_renewals_only(client, dash_portfolio, dash_people):
    """An overdue health alert is a health signal like any other."""
    past = dt.date.today() - dt.timedelta(days=5)
    Task.objects.create(
        account=dash_portfolio, task_type=TaskType.AUTO_RENEWAL,
        title="Renewal due", due_date=past,
    )
    Task.objects.create(
        account=dash_portfolio, task_type=TaskType.AUTO_HEALTH_ALERT,
        title="Health alert", due_date=past,
    )
    Task.objects.create(
        account=dash_portfolio, task_type=TaskType.MANUAL_NEXT_ACTION,
        title="Call them", due_date=past,
    )

    login(client, dash_people["acm"])
    body = client.get(reverse("dashboard:analytics")).content.decode()
    acm_card = {c.id: c.data for c in client.get(
        reverse("dashboard:analytics")).context["cards"]}["overdue_tasks"]

    assert acm_card["value"] == 1
    assert "renewals only" in acm_card["caption"]
    assert "Health alert" not in body

    login(client, dash_people["csm"])
    csm_card = {c.id: c.data for c in client.get(
        reverse("dashboard:analytics")).context["cards"]}["overdue_tasks"]
    assert csm_card["value"] == 3


def test_the_shared_filter_governs_every_card(client, dash_portfolio, dash_people):
    AccountFactory(
        client_code="DASH4",
        csm=dash_people["csm"],
        account_class=AccountClass.D,
        deployment=Deployment.ON_PREM,
    )
    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:analytics"), {"class": AccountClass.VIP})

    data = {c.id: c.data for c in response.context["cards"]}
    assert data["total_accounts"]["value"] == 1
    # The deployment donut narrowed with everything else.
    assert data["deployment_split"]["chart"].total == 1


def test_the_renewals_card_has_its_own_quarter_independent_of_the_page_filter(
    client, dash_portfolio, dash_people
):
    login(client, dash_people["csm"])
    response = client.get(
        reverse("dashboard:analytics"), {"quarter": "4", "quarter_year": "2026"}
    )
    card = {c.id: c.data for c in response.context["cards"]}["renewals_quarter"]

    assert card["quarter"] == 4
    assert card["quarter_year"] == 2026
    assert dash_portfolio in card["accounts"]


def test_a_nonsense_quarter_falls_back_rather_than_erroring(client, dash_portfolio, dash_people):
    login(client, dash_people["csm"])
    response = client.get(
        reverse("dashboard:analytics"), {"quarter": "99", "quarter_year": "banana"}
    )
    assert response.status_code == 200
    card = {c.id: c.data for c in response.context["cards"]}["renewals_quarter"]
    assert card["quarter"] in (1, 2, 3, 4)


def test_the_trend_axis_keeps_months_in_which_nothing_happened(
    client, dash_portfolio, dash_people
):
    """A trend that silently omits empty months is the flattering version."""
    EngagementItem.objects.create(
        account=dash_portfolio,
        item_type=EngagementItemType.EVENT,
        occurred_on=dt.date.today(),
        created_by=dash_people["csm"],
    )
    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:analytics"))
    card = {c.id: c.data for c in response.context["cards"]}["engagement_trend"]

    assert len(card["table"]) == 12
    assert sum(events for _, events, _ in card["table"]) == 1


def test_the_health_donut_labels_every_segment(client, dash_portfolio, dash_people):
    """Red against green is the pair a colour-blind reader cannot separate, so
    the label and count are what actually carry the value."""
    login(client, dash_people["csm"])
    body = client.get(reverse("dashboard:analytics")).content.decode()

    assert "At Risk" in body
    assert "Table view" in body


# ---------------------------------------------------------------------------
# Notification links, and the roster card
# ---------------------------------------------------------------------------


def test_every_notification_url_resolves(db, dash_portfolio, dash_people):
    """A string that looks like a URL is not one.

    Task notifications pointed at "/tasks/<pk>/" for a route that has never
    existed, so every one of them led to a 404. Nothing caught it because the
    value was built by hand; this walks what the dispatcher actually stores.
    """
    from django.urls import Resolver404, resolve

    from csm.core.enums import NotificationKind, TaskType
    from csm.notifications.dispatcher import notify_task
    from csm.notifications.models import Notification
    from csm.tasks.services import create_task

    create_task(
        account=dash_portfolio,
        task_type=TaskType.MANUAL_NEXT_ACTION,
        title="Something to do",
        due_date=dt.date.today(),
        created_by=dash_people["csm"],
    )
    notify_task(
        dash_portfolio.tasks.get(),
        kind=NotificationKind.TASK_ASSIGNED,
        occasion="assigned",
    )

    stored = list(Notification.objects.exclude(url="").values_list("url", flat=True))
    assert stored, "the dispatcher should have written a link"
    for url in stored:
        try:
            resolve(url)
        except Resolver404:  # pragma: no cover - the assertion is the message
            raise AssertionError(f"notification url does not resolve: {url}") from None


def test_reading_a_notification_lands_on_the_account(client, dash_portfolio, dash_people):
    from csm.core.enums import NotificationChannel, NotificationKind
    from csm.notifications.models import Notification

    note = Notification.objects.create(
        user=dash_people["csm"], account=dash_portfolio,
        kind=NotificationKind.TASK_ASSIGNED, channel=NotificationChannel.IN_APP,
        title="Renewal due",
        url=reverse("accounts:detail", args=[dash_portfolio.pk]),
    )

    login(client, dash_people["csm"])
    response = client.post(reverse("notifications:read", args=[note.pk]), follow=True)

    assert response.status_code == 200
    assert dash_portfolio.client_name in response.content.decode()


def test_the_analytics_page_lists_the_accounts_by_name(client, dash_portfolio, dash_people):
    """Every other card is an aggregate; this is the list they are counting."""
    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:analytics"))

    card = {c.id: c.data for c in response.context["cards"]}["account_roster"]
    # Presenters, not models: the shared table asks the presenter for anything
    # health-shaped, so the card hands over the same objects the account list does.
    assert dash_portfolio.client_code in {a.client_code for a in card["accounts"]}

    body = response.content.decode()
    assert "Accounts in this view" in body
    assert dash_portfolio.client_name in body


def test_the_roster_narrows_with_the_shared_filter(client, dash_portfolio, dash_people):
    from csm.core.enums import AccountClass

    AccountFactory(
        client_code="ROSTER2", client_name="Filtered Out",
        csm=dash_people["csm"], account_class=AccountClass.D,
    )
    login(client, dash_people["csm"])

    response = client.get(reverse("dashboard:analytics"), {"class": AccountClass.VIP})
    names = [a.client_name for a in
             {c.id: c.data for c in response.context["cards"]}["account_roster"]["accounts"]]

    assert "Acme Industrial" in names
    assert "Filtered Out" not in names


def test_an_acm_gets_the_roster_without_the_health_dot(client, dash_portfolio, dash_people):
    login(client, dash_people["acm"])
    response = client.get(reverse("dashboard:analytics"))

    card = {c.id: c.data for c in response.context["cards"]}["account_roster"]
    codes = {a.client_code for a in card["accounts"]}
    assert dash_portfolio.client_code in codes, "the ACM still sees their accounts"
    assert card["shows_health"] is False

    body = response.content.decode()
    assert "health-dot" not in body


def test_class_is_a_donut_on_an_ordinal_ramp(client, dash_portfolio, dash_people):
    """VIP through D is a ranking, so one hue dark-to-light carries the order
    that five categorical hues would throw away."""
    from csm.dashboard.cards import CLASS_RAMP

    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:analytics"))
    card = next(c for c in response.context["cards"] if c.id == "class_distribution")

    assert card.kind == "donut"
    tokens = [s.token for s in card.data["chart"].segments]
    # Whatever survives the drop-empty rule keeps the ramp's order.
    assert tokens == [t for t in CLASS_RAMP if t in tokens]


def test_the_distributions_run_in_the_order_they_are_read_in(dash_people):
    """Two rows of four, in the order the page presents them, with the roster of
    names directly beneath the eight cards counting those names."""
    from csm.dashboard.cards import REGISTRY

    order = [c.id for c in REGISTRY]
    rows = [
        ["health_distribution", "lifecycle_distribution", "seat_split",
         "license_distribution"],
        ["country_distribution", "deployment_split", "renewal_year_quarters",
         "class_distribution"],
    ]

    start = order.index(rows[0][0])
    assert order[start:start + 8] == rows[0] + rows[1]
    assert order[start + 8] == "account_roster", "the roster follows the rows it explains"

    widths = {c.id: c.width for c in REGISTRY}
    assert len({widths[c] for c in rows[0] + rows[1]}) == 1, "one row means one width"
    assert widths["account_roster"] == "col-12"


def test_every_card_row_adds_up_to_a_whole_row(dash_people):
    """Cards line up because each run of equal widths fills twelve columns.

    A row that adds up to ten leaves a ragged gap and the next row starts under
    it rather than beside it, which is what "the cards are not aligned" looks
    like from the outside.
    """
    from csm.dashboard.cards import REGISTRY

    span = {"col-6 col-md-3": 3, "col-12 col-md-6 col-xl-3": 3,
            "col-12 col-xl-8": 8, "col-12 col-xl-4": 4, "col-12": 12}

    filled = 0
    for card in REGISTRY:
        assert card.width in span, f"{card.id} has an unmeasured width"
        filled = (filled + span[card.width]) % 12
    assert filled == 0, "the registry ends mid-row"


# ---------------------------------------------------------------------------
# My day: the task card
# ---------------------------------------------------------------------------


@pytest.fixture
def my_tasks(dash_people):
    """One of each kind for the CSM: late, later today, and next week."""
    from csm.core.clock import today
    from csm.core.enums import TaskType
    from csm.tasks.models import Task, TaskAssignment

    account = AccountFactory(client_code="MYD1", client_name="My Day Client",
                             csm=dash_people["csm"])
    as_of = today()
    made = {}
    for name, due_date, due_time in (
        ("late", as_of - dt.timedelta(days=2), None),
        ("late_today", as_of, dt.time(0, 1)),
        ("today", as_of, dt.time(23, 59)),
        ("next_week", as_of + dt.timedelta(days=5), None),
    ):
        task = Task.objects.create(
            account=account, task_type=TaskType.MANUAL_NEXT_ACTION,
            title=f"Task {name}", due_date=due_date, due_time=due_time,
            created_by=dash_people["csm"],
        )
        TaskAssignment.objects.create(task=task, user=dash_people["csm"])
        made[name] = task
    return made


def test_my_day_groups_tasks_by_late_today_and_coming_up(client, my_tasks, dash_people):
    """A flat list ordered by date answers neither "what is late" nor "what is
    today" without the reader checking every badge against the calendar."""
    login(client, dash_people["csm"])
    groups = client.get(reverse("dashboard:index")).context["task_groups"]

    by_label = {g["label"]: [t.title for t in g["tasks"]] for g in groups}
    assert by_label["Overdue"] == ["Task late", "Task late_today"]
    assert by_label["Today"] == ["Task today"]
    assert by_label["Coming up"] == ["Task next_week"]


def test_overdue_work_on_your_accounts_is_named_even_when_it_is_not_yours(
    client, dash_people
):
    """The case that looked like a bug: an overdue task on an account this
    reader owns, assigned to whoever held that account when it was raised.

    Reassigning an account does not move the tasks already routed from it, so
    the task sat on a colleague's list and was invisible on the owner's page.
    It stays out of "my open tasks" - that card means what it says - but the
    page no longer pretends it does not exist.
    """
    from csm.core.clock import today
    from csm.core.enums import TaskType
    from csm.tasks.models import Task, TaskAssignment

    account = AccountFactory(client_code="HANDOVER", csm=dash_people["csm"])
    predecessor = UserFactory(csm=True, email="left-the-team@dphish.com")
    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION,
        title="Raised before the handover", due_date=today() - dt.timedelta(days=1),
        created_by=predecessor,
    )
    TaskAssignment.objects.create(task=task, user=predecessor)

    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:index"))

    assert response.context["overdue_elsewhere"] == 1
    groups = {g["label"]: [t.title for t in g["tasks"]] for g in response.context["task_groups"]}
    assert task.title not in groups["Overdue"], "still not mine; only visible"
    assert "assigned to somebody else" in response.content.decode()


def test_the_going_quiet_card_offers_the_same_row_menu_as_the_contacts_page(
    client, dash_portfolio, dash_people
):
    """Item 2 of the follow-up backlog: edit/CSAT/delete and a clickable row,
    without leaving "My day" for the full contacts list."""
    from csm.contacts.models import Contact

    contact = Contact.objects.create(
        account=dash_portfolio, full_name="Dana Farouk",
        influence="HIGH", interest="HIGH",
    )
    login(client, dash_people["csm"])
    body = client.get(reverse("dashboard:index")).content.decode()

    assert reverse("contacts:edit", args=[contact.pk]) in body
    assert reverse("contacts:delete", args=[contact.pk]) in body
    assert f'data-href="{reverse("contacts:detail", args=[contact.pk])}"' in body


def test_nothing_is_said_when_every_overdue_task_is_your_own(client, my_tasks, dash_people):
    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:index"))
    assert response.context["overdue_elsewhere"] == 0
    assert "assigned to somebody else" not in response.content.decode()


def test_a_timed_task_moves_from_today_to_overdue_during_the_day(
    client, dash_people
):
    """The 00:01 one in the fixture above is late by definition; this pins the
    moment it changes group."""
    import time_machine

    from csm.core.enums import TaskType
    from csm.tasks.models import Task, TaskAssignment

    account = AccountFactory(client_code="MYD2", csm=dash_people["csm"])
    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Nine o'clock",
        due_date=dt.date(2026, 6, 15), due_time=dt.time(9, 0),
        created_by=dash_people["csm"],
    )
    TaskAssignment.objects.create(task=task, user=dash_people["csm"])
    login(client, dash_people["csm"])

    def labels_for(moment):
        with time_machine.travel(moment, tick=False):
            groups = client.get(reverse("dashboard:index")).context["task_groups"]
        return {g["label"] for g in groups if task.title in [t.title for t in g["tasks"]]}

    # 09:00 Cairo is 06:00 UTC in June.
    assert labels_for("2026-06-15 05:00:00+00:00") == {"Today"}
    assert labels_for("2026-06-15 07:00:00+00:00") == {"Overdue"}


def test_each_task_on_my_day_can_be_finished_edited_or_deleted(
    client, my_tasks, dash_people
):
    """Three icons, the same three as the activities page: a tick, a pencil and
    a bin. Words would be more furniture than content on every row of a list."""
    login(client, dash_people["csm"])
    body = client.get(reverse("dashboard:index")).content.decode()

    task = my_tasks["today"]
    assert reverse("tasks:complete", args=[task.pk]) in body
    assert reverse("tasks:edit", args=[task.pk]) in body
    assert reverse("tasks:delete", args=[task.pk]) in body
    for icon in ("bi-check-lg", "bi-pencil", "bi-trash3"):
        assert icon in body, icon
    # Icon-only buttons with no accessible name do not exist to a screen
    # reader, and "x" read aloud is not "delete".
    assert f"Mark {task.title} done" in body
    assert f"Delete {task.title}" in body


def test_finishing_a_task_from_my_day_comes_back_to_my_day(client, my_tasks, dash_people):
    from csm.core.enums import TaskStatus

    login(client, dash_people["csm"])
    task = my_tasks["today"]
    response = client.post(
        reverse("tasks:complete", args=[task.pk]),
        {"next": reverse("dashboard:index")},
    )

    task.refresh_from_db()
    assert task.status == TaskStatus.DONE
    assert response["Location"] == reverse("dashboard:index")


def test_deleting_a_task_from_my_day_comes_back_to_my_day(client, my_tasks, dash_people):
    login(client, dash_people["csm"])
    task = my_tasks["today"]
    response = client.post(
        reverse("tasks:delete", args=[task.pk]),
        {"next": reverse("dashboard:index")},
    )
    assert response["Location"] == reverse("dashboard:index")


@pytest.mark.parametrize("route", ["tasks:complete", "tasks:delete"])
def test_a_task_action_cannot_redirect_off_this_site(client, my_tasks, dash_people, route):
    """``next`` arrives in the post. Unchecked it is an open redirect: a button
    inside this application that lands on somebody else's login page."""
    login(client, dash_people["csm"])
    task = my_tasks["today"]
    response = client.post(
        reverse(route, args=[task.pk]), {"next": "https://evil.example/harvest"}
    )
    assert "evil.example" not in response["Location"]


def test_a_menu_only_offers_what_the_reader_may_do(client, my_tasks, dash_people):
    """The actions are separate capabilities, so they are gated separately: a
    manager sees the tasks and none of the buttons."""
    login(client, dash_people["manager"])
    body = client.get(reverse("dashboard:index")).content.decode()
    task = my_tasks["today"]

    assert reverse("tasks:delete", args=[task.pk]) not in body
    assert reverse("tasks:complete", args=[task.pk]) not in body


# ---------------------------------------------------------------------------
# The renewal chart's year, and the seat split
# ---------------------------------------------------------------------------


def card_on(response, card_id):
    return next(c for c in response.context["cards"] if c.id == card_id)


def test_the_renewal_chart_offers_the_years_the_data_has(client, dash_people):
    """Not a fixed span around today.

    A span is wrong in both directions at once: it lists years the portfolio has
    no renewal in, and stops short of the five-year contract that is the one
    worth looking at.
    """
    from csm.core.clock import today

    for year in (2024, 2027, 2031):
        AccountFactory(
            client_code=f"YR{year}", client_name=f"Ends {year}",
            csm=dash_people["csm"], contract_end_date=dt.date(year, 6, 1),
        )
    login(client, dash_people["csm"])

    offered = card_on(client.get(reverse("dashboard:analytics")), "renewal_year_quarters")
    assert offered.data["year_choices"] == [2024, today().year, 2027, 2031]
    assert offered.data["year"] == today().year, "this year until asked otherwise"


def test_the_renewal_chart_draws_the_year_it_is_given(client, dash_people):
    for code, end in (("A", dt.date(2027, 2, 1)), ("B", dt.date(2027, 8, 9)),
                      ("C", dt.date(2028, 1, 5))):
        AccountFactory(
            client_code=f"YRD{code}", client_name=f"Ends {code}",
            csm=dash_people["csm"], contract_end_date=end,
        )
    login(client, dash_people["csm"])

    card = card_on(
        client.get(reverse("dashboard:analytics"), {"renewal_year": 2027}),
        "renewal_year_quarters",
    )
    assert card.data["rows"] == [("Q1", 1), ("Q2", 0), ("Q3", 1), ("Q4", 0)]
    assert "2027" in card.data["caption"]
    # And its bars still filter the page, to that year rather than to this one.
    assert card.data["chart"].bars[0].key == "2027-1"


def test_a_year_nothing_renews_in_falls_back(client, dash_people):
    """Validated against the years that exist, not against a range, so a
    hand-edited URL cannot produce an empty chart for a year with no data."""
    from csm.core.clock import today

    AccountFactory(
        client_code="YRX", client_name="Ends soon",
        csm=dash_people["csm"], contract_end_date=dt.date(today().year, 3, 3),
    )
    login(client, dash_people["csm"])
    for raw in ("1999", "not-a-year", "", "2031"):
        card = card_on(
            client.get(reverse("dashboard:analytics"), {"renewal_year": raw}),
            "renewal_year_quarters",
        )
        assert card.data["year"] == today().year, raw


def test_one_cards_control_does_not_reset_the_others(client, dash_people):
    """Each card form resubmits the page filter *and* the other card's control.

    Pick 2027 on the chart, change the quarter on the renewals list, and without
    this the chart is back to this year with nobody having asked it to be.
    """
    import re

    AccountFactory(
        client_code="CARRY", client_name="Carry Me",
        csm=dash_people["csm"], contract_end_date=dt.date(2027, 5, 5),
    )
    login(client, dash_people["csm"])
    body = client.get(
        reverse("dashboard:analytics"),
        {"renewal_year": 2027, "quarter": 3, "class": AccountClass.VIP},
    ).content.decode()

    forms = re.findall(r'<form method="get" class="d-flex gap-2[^"]*">(.*?)</form>', body, re.S)
    carried = [set(re.findall(r'name="(\w+)"', form)) for form in forms]

    assert {"class", "quarter", "renewal_year"} <= carried[0], "the year form carries the rest"
    assert {"class", "renewal_year", "quarter"} <= carried[1], "the quarter form carries the year"
    # And neither submits its own control twice, which would send two values.
    assert len(re.findall(r'name="renewal_year"', forms[0])) == 1
    assert len(re.findall(r'name="quarter"', forms[1])) == 1


def test_the_seat_split_separates_contracted_from_added(client, dash_people):
    """BOQ is what the client signed for; added users are what they grew into.

    The Total users figure alone cannot say which of the two a portfolio is made
    of, and that is the question the split exists to answer.
    """
    AccountFactory(
        client_code="SEAT1", client_name="Seats One", csm=dash_people["csm"],
        boq_users_count=800, added_users_count=200,
    )
    AccountFactory(
        client_code="SEAT2", client_name="Seats Two", csm=dash_people["csm"],
        boq_users_count=1000, added_users_count=0,
    )
    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:analytics"))

    seats = card_on(response, "seat_split")
    assert [(s.label, s.value) for s in seats.data["chart"].segments] == [
        ("Contracted (BOQ)", 1800), ("Added since", 200),
    ]
    assert seats.data["chart"].unit == "users", "a seat count is not an account count"
    assert "200 of 2,000 seats" in seats.data["caption"]

    # The stat above it says the same thing as a number, and they must agree.
    users = card_on(response, "total_users")
    assert users.data["value"] == "2,000"
    assert users.data["breakdown"] == [("BOQ users", "1,800"), ("Added users", "200")]


def test_the_seat_split_survives_an_empty_portfolio(client, dash_people):
    login(client, dash_people["csm"])
    seats = card_on(client.get(reverse("dashboard:analytics")), "seat_split")
    assert seats.data["chart"].is_empty
    assert seats.data["caption"] == "No seats recorded."


# ---------------------------------------------------------------------------
# Charts as controls
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed_portfolio(dash_people):
    """Six accounts split across deployment, class and licence."""
    from csm.core.enums import LicenseType

    made = []
    for index, (code, deployment, klass, licence) in enumerate(
        (
            ("MIX1", Deployment.CLOUD, AccountClass.VIP, LicenseType.COMPLETE),
            ("MIX2", Deployment.CLOUD, AccountClass.A, LicenseType.COMPLETE),
            ("MIX3", Deployment.CLOUD, AccountClass.B, LicenseType.ESSENTIAL),
            ("MIX4", Deployment.ON_PREM, AccountClass.VIP, LicenseType.DRILL),
            ("MIX5", Deployment.ON_PREM, AccountClass.C, ""),
            ("MIX6", Deployment.ON_PREM, AccountClass.D, ""),
        )
    ):
        made.append(
            AccountFactory(
                client_code=code,
                client_name=f"Mixed {index + 1}",
                csm=dash_people["csm"],
                deployment=deployment,
                account_class=klass,
                license_type=licence,
            )
        )
    return made


def chart_links(body: str) -> list:
    """Every link on the page that leads back to analytics, unescaped."""
    import re

    return [
        href.replace("&amp;", "&")
        for href in re.findall(r'href="(/analytics/[^"]*)"', body)
    ]


def selected_links(body: str) -> list:
    """Only the links on the segments that are currently filtering the page.

    Matched through the selected mark rather than by reading the whole list: the
    page's own Clear-filters link points at the unfiltered URL too, so "is the
    unfiltered URL somewhere on the page" would pass whether or not the segment
    itself had become a way back.
    """
    import re

    patterns = (
        r'<a href="([^"]*)" class="viz-hit">\s*<path class="viz-segment is-selected"',
        r'<a class="viz-legend-link is-selected"\s*href="([^"]*)"',
        r'<a class="stat-link is-selected" href="([^"]*)"',
    )
    found = []
    for pattern in patterns:
        found += [h.replace("&amp;", "&") for h in re.findall(pattern, body)]
    return found


def total_on(response) -> int:
    return next(c for c in response.context["cards"] if c.id == "total_accounts").data["value"]


def test_a_donut_segment_filters_the_whole_page(client, mixed_portfolio, dash_people):
    """The chart is the control. Clicking Cloud narrows every card, not one."""
    login(client, dash_people["csm"])
    body = client.get(reverse("dashboard:analytics")).content.decode()

    link = next(h for h in chart_links(body) if h.endswith("deployment=CLOUD"))
    response = client.get(link)

    assert total_on(response) == 3
    roster = next(c for c in response.context["cards"] if c.id == "account_roster")
    assert {a.client_name for a in roster.data["accounts"]} == {
        "Mixed 1", "Mixed 2", "Mixed 3",
    }


def test_clicking_the_same_segment_again_puts_the_page_back(
    client, mixed_portfolio, dash_people
):
    """The headline behaviour: a segment is a toggle, not a one-way trip.

    The segment the reader just clicked is still on screen - it is the only one
    left - so it has to be the way back out, or an accidental click can only be
    undone by finding the Clear link.
    """
    login(client, dash_people["csm"])
    filtered = client.get(reverse("dashboard:analytics"), {"deployment": Deployment.CLOUD})
    assert total_on(filtered) == 3

    # The selected segment's own link carries no deployment at all.
    back = selected_links(filtered.content.decode())
    assert back, "the selected segment should be marked and linked"
    assert set(back) == {reverse("dashboard:analytics")}

    assert total_on(client.get(back[0])) == 6


def test_clicking_a_second_chart_keeps_the_first_filter(client, mixed_portfolio, dash_people):
    """A link built by hand from ``?class=VIP`` would silently drop the
    deployment the reader had already chosen. The two compose instead."""
    login(client, dash_people["csm"])
    filtered = client.get(reverse("dashboard:analytics"), {"deployment": Deployment.CLOUD})

    link = next(h for h in chart_links(filtered.content.decode()) if "class=VIP" in h)
    assert "deployment=CLOUD" in link

    response = client.get(link)
    assert total_on(response) == 1
    assert response.context["filters"]["deployment"] == Deployment.CLOUD


def test_a_category_stored_as_empty_is_still_askable_for(client, mixed_portfolio, dash_people):
    """"Not set" is a real answer about a portfolio and has to be clickable.

    It cannot travel as an empty query value - that already means "no filter" -
    so it goes as ``none`` and is translated back at the view.
    """
    login(client, dash_people["csm"])
    body = client.get(reverse("dashboard:analytics")).content.decode()

    link = next(h for h in chart_links(body) if "license=none" in h)
    response = client.get(link)

    assert total_on(response) == 2
    roster = next(c for c in response.context["cards"] if c.id == "account_roster")
    assert {a.client_name for a in roster.data["accounts"]} == {"Mixed 5", "Mixed 6"}


def test_a_segment_that_stands_for_no_single_value_is_inert(client, dash_people):
    """The country chart folds its tail into "Other", which is several countries
    at once. It keeps its bar so the numbers still reconcile, but it is not a
    link to anywhere."""
    from csm.dashboard.charts import bars

    chart = bars([("Egypt", 4, "EG"), ("Other", 7, "")])
    egypt, other = chart.bars
    assert egypt.key == "EG"
    assert other.key == ""


def test_the_managed_service_figure_is_a_toggle(client, dash_people):
    AccountFactory(
        client_code="MAN1", client_name="Managed One",
        csm=dash_people["csm"], is_managed_service=True,
    )
    AccountFactory(
        client_code="MAN2", client_name="Unmanaged One",
        csm=dash_people["csm"], is_managed_service=False,
    )
    login(client, dash_people["csm"])

    body = client.get(reverse("dashboard:analytics")).content.decode()
    link = next(h for h in chart_links(body) if "managed=1" in h)

    response = client.get(link)
    assert total_on(response) == 1
    # And the same figure, now marked as selected, links back to the whole page.
    assert selected_links(response.content.decode()) == [reverse("dashboard:analytics")]


def test_an_unreadable_chart_filter_shows_everything(client, mixed_portfolio, dash_people):
    login(client, dash_people["csm"])
    for param, value in (
        ("deployment", "NOPE"), ("class", "ZZ"), ("stage", "42"), ("license", "FREE"),
    ):
        assert total_on(client.get(reverse("dashboard:analytics"), {param: value})) == 6


def test_an_acm_gets_no_clickable_health_segments(client, mixed_portfolio, dash_people):
    """The health donut is the card an ACM does not get, so its links cannot
    exist either - a status link is a health signal in a URL."""
    login(client, dash_people["acm"])
    body = client.get(reverse("dashboard:analytics")).content.decode()
    assert not [h for h in chart_links(body) if "status=" in h]


def test_a_page_filtered_to_nothing_still_has_a_way_back(client, mixed_portfolio, dash_people):
    """Three clicks can narrow the page to zero accounts, and an empty chart has
    no segment left to click again. Each active filter therefore carries its own
    chip, which removes that one and leaves the others alone - Clear filters
    would throw away choices the reader still wants."""
    import re

    login(client, dash_people["csm"])
    response = client.get(
        reverse("dashboard:analytics"),
        {"deployment": Deployment.CLOUD, "class": AccountClass.D},
    )
    assert total_on(response) == 0

    body = response.content.decode()
    chips = [
        h.replace("&amp;", "&")
        for h in re.findall(r'<a class="filter-chip" href="([^"]*)"', body)
    ]
    assert len(chips) == 2

    # Each one drops itself and keeps the other, so the reader steps back rather
    # than starting over.
    assert total_on(client.get(next(h for h in chips if "deployment" not in h))) == 1
    assert total_on(client.get(next(h for h in chips if "class" not in h))) == 3


def test_every_active_filter_is_named_in_words(client, mixed_portfolio, dash_people):
    """A chip saying ``AT_RISK`` or ``48`` would be a stored value, not a
    sentence. What it says is what the reader clicked."""
    from csm.core.enums import LicenseType

    login(client, dash_people["csm"])
    response = client.get(
        reverse("dashboard:analytics"),
        {
            "class": AccountClass.VIP,
            "license": LicenseType.COMPLETE,
            "csm": dash_people["csm"].pk,
            "managed": "1",
            "renewal": "2026-1",
        },
    )
    labels = [chip["label"] for chip in response.context["active_filters"]]
    assert labels == [
        "VIP", "Complete", "Managed service", "Renewing Q1 2026", "CSM: Casey Morgan",
    ]


def test_a_licence_nobody_recorded_reads_as_words_too(client, mixed_portfolio, dash_people):
    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:analytics"), {"license": "none"})
    assert [c["label"] for c in response.context["active_filters"]] == ["No edition recorded"]


# -- the tag itself ---------------------------------------------------------


def toggle(query: str, param: str, value: str) -> str:
    from django.template import Context

    from csm.core.templatetags.query import toggle_filter

    request = RequestFactory().get(f"/analytics/{query}")
    return toggle_filter(Context({"request": request}), param, value)


def test_the_toggle_sets_clears_and_preserves():
    assert toggle("", "class", "VIP") == "/analytics/?class=VIP"
    assert toggle("?class=VIP", "class", "VIP") == "/analytics/"
    assert toggle("?class=VIP", "class", "A") == "/analytics/?class=A"
    assert toggle("?country=EG", "class", "VIP") in (
        "/analytics/?country=EG&class=VIP",
        "/analytics/?class=VIP&country=EG",
    )


def test_the_toggle_drops_the_page_number():
    """A page number from a wider result set points into a list that no longer
    has that many pages."""
    assert "page" not in toggle("?page=4", "class", "VIP")


def test_a_segment_with_no_key_gets_no_link():
    assert toggle("", "class", "") == ""
    assert toggle("", "", "VIP") == ""


# ---------------------------------------------------------------------------
# Owner and renewal-quarter filters
# ---------------------------------------------------------------------------


@pytest.fixture
def renewal_spread(dash_people):
    """Four accounts for one CSM, spread across quarters and two years."""
    from csm.core.clock import today

    year = today().year
    made = {}
    for code, end in (
        ("Q1", dt.date(year, 2, 10)),
        ("Q3", dt.date(year, 8, 1)),
        ("Q4", dt.date(year, 11, 30)),
        ("NEXT", dt.date(year + 1, 5, 5)),
    ):
        made[code] = AccountFactory(
            client_code=f"REN-{code}",
            client_name=f"Renewal {code}",
            csm=dash_people["csm"],
            acm=dash_people["acm"],
            contract_end_date=end,
        )
    return made


def test_the_renewal_quarters_card_covers_the_whole_calendar_year(
    client, renewal_spread, dash_people
):
    """An empty quarter is drawn as empty, not skipped.

    Dropping Q2 would leave a three-bar chart that reads as a three-quarter
    year, and the reader has no way of knowing which quarter is missing.
    """
    from csm.core.clock import today

    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:analytics"))
    card = next(c for c in response.context["cards"] if c.id == "renewal_year_quarters")

    # Q2 is empty and next year's renewal is absent entirely: the card is the
    # shape of one calendar year, not of everything ahead.
    assert card.data["rows"] == [("Q1", 1), ("Q2", 0), ("Q3", 1), ("Q4", 1)]
    assert str(today().year) in card.data["caption"]


def test_the_renewal_quarters_card_shares_a_width_with_the_distributions(dash_people):
    """Which row it sits in is asserted once, above; this is only that it is one
    of the eight and not, say, full width on its own."""
    from csm.dashboard.cards import REGISTRY

    widths = {c.id: c.width for c in REGISTRY}
    assert widths["renewal_year_quarters"] == widths["health_distribution"]
    assert widths["renewal_year_quarters"] == widths["seat_split"]


def test_the_renewal_quarter_filter_narrows_every_card(client, renewal_spread, dash_people):
    from csm.core.clock import today

    login(client, dash_people["csm"])
    response = client.get(
        reverse("dashboard:analytics"), {"renewal": f"{today().year}-1"}
    )

    total = next(c for c in response.context["cards"] if c.id == "total_accounts")
    roster = next(c for c in response.context["cards"] if c.id == "account_roster")
    assert total.data["value"] == 1
    assert [a.client_name for a in roster.data["accounts"]] == ["Renewal Q1"]


def test_a_nonsense_renewal_quarter_leaves_the_page_alone(client, renewal_spread, dash_people):
    """An unreadable filter shows everything, never nothing.

    A portfolio that looks empty reads as data loss; an ignored filter is
    visibly an ignored filter, because the control still shows what was asked
    for.
    """
    login(client, dash_people["csm"])
    for value in ("2026-9", "Q4", "", "--", "99999-1"):
        response = client.get(reverse("dashboard:analytics"), {"renewal": value})
        total = next(c for c in response.context["cards"] if c.id == "total_accounts")
        assert total.data["value"] == 4, value


def test_the_renewal_quarter_options_survive_being_chosen(client, renewal_spread, dash_people):
    """Built from the unfiltered scope.

    Derived from the filtered queryset instead, picking Q1 would leave Q1 as
    the only option and no way back without editing the URL.
    """
    from csm.core.clock import today

    login(client, dash_people["csm"])
    year = today().year
    response = client.get(reverse("dashboard:analytics"), {"renewal": f"{year}-1"})

    assert [v for v, _ in response.context["renewal_quarters"]] == [
        f"{year}-1", f"{year}-3", f"{year}-4", f"{year + 1}-2",
    ]


def test_the_owner_filters_narrow_the_page(client, dash_portfolio, dash_people):
    mine = dash_portfolio
    theirs = AccountFactory(
        client_code="OWNED",
        client_name="Zenith Holdings",
        csm=dash_people["csm"],
        acm=UserFactory(acm=True, email="dash-acm2@dphish.com", full_name="Robin Vale"),
        contract_end_date=dt.date(2027, 3, 1),
    )

    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:analytics"), {"acm": dash_people["acm"].pk})
    roster = next(c for c in response.context["cards"] if c.id == "account_roster")
    assert [a.client_name for a in roster.data["accounts"]] == [mine.client_name]

    response = client.get(reverse("dashboard:analytics"), {"csm": dash_people["csm"].pk})
    roster = next(c for c in response.context["cards"] if c.id == "account_roster")
    assert {a.client_name for a in roster.data["accounts"]} == {
        mine.client_name, theirs.client_name
    }


def test_the_owner_filters_name_only_people_inside_the_scope(
    client, dash_portfolio, dash_people
):
    """The dropdown is a list of colleagues, so it is scoped like everything else.

    A CSM who can see one account must not be handed the name of every CSM in
    the company, nor a count of them.
    """
    stranger = UserFactory(csm=True, email="dash-stranger@dphish.com", full_name="Kit Sayer")
    AccountFactory(client_code="FARAWAY", client_name="Far Away", csm=stranger)

    login(client, dash_people["csm"])
    response = client.get(reverse("dashboard:analytics"))

    names = [label for _, label in response.context["csm_choices"]]
    assert names == ["Casey Morgan"]
    assert "Kit Sayer" not in names

    login(client, dash_people["manager"])
    names = [
        label for _, label in client.get(reverse("dashboard:analytics")).context["csm_choices"]
    ]
    assert {"Casey Morgan", "Kit Sayer"} <= set(names), "a manager sees the whole company"


def test_unassigned_is_offered_only_when_something_is_unassigned(
    client, dash_portfolio, dash_people
):
    login(client, dash_people["manager"])
    values = [v for v, _ in client.get(reverse("dashboard:analytics")).context["acm_choices"]]
    assert "none" not in values

    AccountFactory(client_code="NOACM", client_name="No ACM", csm=dash_people["csm"], acm=None)
    response = client.get(reverse("dashboard:analytics"))
    assert "none" in [v for v, _ in response.context["acm_choices"]]

    response = client.get(reverse("dashboard:analytics"), {"acm": "none"})
    roster = next(c for c in response.context["cards"] if c.id == "account_roster")
    assert [a.client_name for a in roster.data["accounts"]] == ["No ACM"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-4", (2026, 4)),
        (" 2026-1 ", (2026, 1)),
        ("2026-0", None),
        ("2026-5", None),
        ("Q4", None),
        ("2026", None),
        ("", None),
        (None, None),
        ("-", None),
    ],
)
def test_a_renewal_quarter_is_read_strictly_or_not_at_all(raw, expected):
    from csm.accounts.filters import parse_renewal_quarter

    assert parse_renewal_quarter(raw) == expected


def test_the_roster_and_the_account_list_render_the_same_table(
    client, dash_portfolio, dash_people
):
    """One partial, so the two pages cannot describe the same accounts
    differently. Two copies would drift the first time a column changed."""
    login(client, dash_people["csm"])

    roster = client.get(reverse("dashboard:analytics")).content.decode()
    listing = client.get(reverse("accounts:list")).content.decode()

    # Collapsed, because the template indents its header cells and the point
    # is which columns exist, not how they are wrapped.
    def headers(html):
        import re

        head = html.split("<thead>")[1].split("</thead>")[0]
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", head)).split()

    roster_card = roster.split("Accounts in this view")[1]
    roster_headers = headers(roster_card)
    list_headers = headers(listing)

    for column in ("Account", "Class", "License", "Stage", "Health", "CSM", "ACM", "Renewal"):
        assert column in roster_headers, column
        assert column in list_headers, column

    # One deliberate difference: the list carries a row-actions column and the
    # roster does not. The roster is a report you read, and a delete beside a
    # chart is a misclick. Asserted exactly, so any *other* divergence fails.
    assert list_headers == roster_headers + ["Actions"]

    # Same row furniture on both: the logo, the status dot and the health pill.
    for marker in ("acct-logo", "health-dot", "health-pill"):
        assert marker in roster_card, marker
        assert marker in listing, marker


def test_both_tables_offer_the_same_sort_keys(client, dash_portfolio, dash_people):
    """One partial, one set of keys.

    A column that sorts on one page and does nothing on the other, under an
    identical heading, is worse than a table that does not sort at all.
    """
    import re

    login(client, dash_people["csm"])

    roster = client.get(reverse("dashboard:analytics")).content.decode()
    roster_card = roster.split("Accounts in this view")[1]
    listing = client.get(reverse("accounts:list")).content.decode()

    def keys(body):
        return set(re.findall(r"sort=(-?\w+)", body))

    expected = {"name", "class", "license", "boq", "stage", "health", "csm", "acm", "renewal"}
    assert keys(listing) == expected
    assert keys(roster_card) == expected


def test_sorting_the_roster_keeps_the_page_filter(client, mixed_portfolio, dash_people):
    """A bare ``?sort=name`` would throw the filter away.

    Sorting a filtered page silently unfiltering it is the kind of bug nobody
    reports, because the reader assumes they mis-clicked.
    """
    login(client, dash_people["csm"])
    filtered = client.get(reverse("dashboard:analytics"), {"deployment": Deployment.CLOUD})

    card = filtered.content.decode().split("Accounts in this view")[1]
    link = next(
        href.replace("&amp;", "&")
        for href in __import__("re").findall(r'href="(/analytics/[^"]*sort=boq[^"]*)"', card)
    )
    assert "deployment=CLOUD" in link

    response = client.get(link)
    assert total_on(response) == 3


def test_the_roster_sorts_by_the_column_asked_for(client, dash_people):
    for code, name, boq in (("S1", "Alpha", 10), ("S2", "Beta", 900), ("S3", "Gamma", 300)):
        AccountFactory(
            client_code=code, client_name=name, csm=dash_people["csm"], boq_users_count=boq
        )
    login(client, dash_people["csm"])

    def names(**params):
        response = client.get(reverse("dashboard:analytics"), params)
        roster = next(c for c in response.context["cards"] if c.id == "account_roster")
        return [a.client_name for a in roster.data["accounts"]]

    assert names() == ["Alpha", "Beta", "Gamma"], "by name unless asked otherwise"
    assert names(sort="boq") == ["Alpha", "Gamma", "Beta"]
    assert names(sort="-boq") == ["Beta", "Gamma", "Alpha"]


def test_health_sorts_by_severity_not_by_spelling(client, dash_people):
    """Alphabetically the stored values run At Risk, Healthy, N/A, Watch, which
    sits the accounts in trouble next to the ones that are fine."""
    from csm.accounts.filters import apply_sort
    from csm.accounts.models import Account

    for code, status in (
        ("H1", HealthStatus.HEALTHY), ("H2", HealthStatus.AT_RISK),
        ("H3", HealthStatus.NOT_APPLICABLE), ("H4", HealthStatus.WATCH),
    ):
        AccountFactory(
            client_code=code, client_name=f"Account {code}",
            csm=dash_people["csm"], effective_health_status=status,
        )

    ordered = apply_sort(Account.objects.alive(), "health", shows_health=True)
    assert [a.effective_health_status for a in ordered] == [
        HealthStatus.AT_RISK, HealthStatus.WATCH,
        HealthStatus.HEALTHY, HealthStatus.NOT_APPLICABLE,
    ]


def test_an_unknown_sort_key_falls_back_to_the_name(client, dash_people):
    from csm.accounts.filters import apply_sort
    from csm.accounts.models import Account

    for code in ("Z", "A", "M"):
        AccountFactory(client_code=f"U{code}", client_name=code, csm=dash_people["csm"])

    for raw in ("nonsense", "", None, "-", "pk", "password"):
        ordered = apply_sort(Account.objects.alive(), raw, shows_health=True)
        assert [a.client_name for a in ordered] == ["A", "M", "Z"], raw
