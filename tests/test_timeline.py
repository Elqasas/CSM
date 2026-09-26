"""The account timeline: three streams read together, written nowhere.

The merge itself needs no database - it is a function over two lists - so most
of this runs without one, the same way the scoring engine's tests do.

The rule the page exists to respect is asserted here too: reading them together
must not make a planned meeting look like contact that happened, because
``health/inputs.py`` reads ``Activity`` for three of the five scoring factors.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from csm.activities import timeline
from csm.core.enums import ActivityType, TaskStatus, TaskType
from tests.factories import AccountFactory, ActivityFactory, ContactFactory, UserFactory
from tests.test_views import PASSWORD, login

TODAY = dt.date(2026, 6, 15)


class FakeContact:
    def __init__(self, name):
        self.full_name = name


class FakeActivity:
    is_voided = False

    def __init__(self, when, subject="", notes="", contact=None, voided=False):
        self.occurred_on = when
        self.subject = subject
        self.notes = notes
        self.contact = contact
        self.is_voided = voided

    def get_activity_type_display(self):
        return "Meeting"


class FakeTask:
    def __init__(self, when, title, open_=True, description="", contact=None):
        self.due_date = when
        self.title = title
        self.is_open = open_
        self.description = description
        self.contact = contact

    def get_task_type_display(self):
        return "Call"


# ---------------------------------------------------------------------------
# The merge - no database
# ---------------------------------------------------------------------------


def test_the_two_streams_interleave_newest_first():
    entries = timeline.build(
        [FakeActivity(dt.date(2026, 6, 10), subject="We spoke")],
        [FakeTask(dt.date(2026, 6, 12), "We will speak")],
    )
    assert [e.title for e in entries] == ["We will speak", "We spoke"]


def test_planned_and_happened_are_the_only_two_states():
    """Done was a third for a while and it double-counted: completing a task
    writes an Activity, so the page showed the task as Done *and* the activity
    it produced as Happened - two lines, one meeting."""
    entries = timeline.build(
        [FakeActivity(TODAY, subject="Called them")],
        [FakeTask(TODAY, "Call them", open_=True), FakeTask(TODAY, "Old call", open_=False)],
    )
    natures = {e.title: e.nature for e in entries}

    assert natures["Called them"] == timeline.HAPPENED
    assert natures["Call them"] == timeline.PLANNED
    assert natures["Old call"] == timeline.HAPPENED, "a finished task happened"
    assert {e.nature for e in entries} == {timeline.HAPPENED, timeline.PLANNED}


def test_entries_are_keyed_on_the_day_they_are_about():
    """Not on when the row was written. A task completed this morning but due
    last week belongs where the work belonged, or the timeline becomes a history
    of somebody's data entry."""
    entries = timeline.build([], [FakeTask(dt.date(2026, 6, 1), "Due in June", open_=False)])
    assert entries[0].when == dt.date(2026, 6, 1)


def test_two_entries_on_one_day_come_back_in_a_stable_order():
    """Otherwise the page reshuffles itself between visits and nobody can tell
    whether something changed."""
    first = timeline.build(
        [FakeActivity(TODAY, subject="Bravo"), FakeActivity(TODAY, subject="Alpha")],
        [FakeTask(TODAY, "Charlie")],
    )
    second = timeline.build(
        [FakeActivity(TODAY, subject="Alpha"), FakeActivity(TODAY, subject="Bravo")],
        [FakeTask(TODAY, "Charlie")],
    )
    assert [e.title for e in first] == [e.title for e in second]


def test_a_voided_activity_is_carried_with_its_mark():
    entries = timeline.build([FakeActivity(TODAY, subject="Mistake", voided=True)], [])
    assert entries[0].voided is True


def test_an_activity_with_no_subject_falls_back_to_its_kind():
    assert timeline.build([FakeActivity(TODAY)], [])[0].title == "Meeting"


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


@pytest.fixture
def account(db):
    csm = UserFactory(csm=True, email="tl-csm@dphish.com", full_name="Casey Morgan")
    csm.set_password(PASSWORD)
    csm.save(update_fields=["password"])
    account = AccountFactory(client_code="TL1", client_name="Timeline Co", csm=csm, acm=None)
    return account


def test_the_page_shows_what_happened_and_what_is_planned(client, account):
    from csm.tasks.models import Task

    ActivityFactory(
        account=account, activity_type=ActivityType.CALL, subject="Rang them",
        occurred_on=dt.date.today() - dt.timedelta(days=2),
    )
    Task.objects.create(
        account=account, task_type=TaskType.MEETING, title="QBR next week",
        due_date=dt.date.today() + dt.timedelta(days=7), created_by=account.csm,
    )

    login(client, account.csm)
    body = client.get(reverse("activities:timeline", args=[account.pk])).content.decode()

    assert "Rang them" in body
    assert "QBR next week" in body
    assert "Happened" in body
    assert "Planned" in body
    assert "Only what happened counts towards the health score" in body


def test_the_page_can_be_narrowed_to_one_stream(client, account):
    from csm.tasks.models import Task

    ActivityFactory(account=account, subject="Rang them", occurred_on=dt.date.today())
    Task.objects.create(
        account=account, task_type=TaskType.CALL, title="Ring them again",
        due_date=dt.date.today(), created_by=account.csm,
    )
    login(client, account.csm)
    url = reverse("activities:timeline", args=[account.pk])

    only_planned = client.get(url, {"nature": "planned"}).content.decode()
    assert "Ring them again" in only_planned
    assert "Rang them" not in only_planned

    only_log = client.get(url, {"nature": "happened"}).content.decode()
    assert "Rang them" in only_log
    assert "Ring them again" not in only_log


# ---------------------------------------------------------------------------
# The filters
# ---------------------------------------------------------------------------


@pytest.fixture
def spread(account):
    """A year of history, so a period filter has something to cut."""
    from csm.tasks.models import Task

    old = ActivityFactory(
        account=account, subject="Ancient call",
        occurred_on=dt.date.today() - dt.timedelta(days=200),
    )
    recent = ActivityFactory(
        account=account, subject="Recent call",
        occurred_on=dt.date.today() - dt.timedelta(days=3),
    )
    planned = Task.objects.create(
        account=account, task_type=TaskType.MEETING, title="Planned meeting",
        due_date=dt.date.today() + dt.timedelta(days=5), created_by=account.csm,
    )
    return {"old": old, "recent": recent, "planned": planned}


def test_the_period_narrows_by_date(client, account, spread):
    """Applied to the queries, not to the merged list.

    Filtering after the merge would narrow the slice already taken, so asking
    for last month would return whatever part of the most recent sixty happened
    to fall in it - which looks like an answer and is not one.
    """
    login(client, account.csm)
    url = reverse("activities:timeline", args=[account.pk])

    everything = client.get(url, {"period": "all"}).content.decode()
    assert "Ancient call" in everything

    recent = client.get(url, {"period": "30"}).content.decode()
    assert "Ancient call" not in recent
    assert "Recent call" in recent


def test_the_search_reads_both_halves(client, account, spread):
    login(client, account.csm)
    url = reverse("activities:timeline", args=[account.pk])

    found = client.get(url, {"q": "Planned", "period": "all"}).content.decode()
    assert "Planned meeting" in found
    assert "Recent call" not in found


def test_the_portfolio_page_can_be_narrowed_to_one_account(client, account, spread):
    from tests.factories import AccountFactory as Acc

    Acc(client_code="TL2", client_name="Other Co", csm=account.csm)
    ActivityFactory(
        account=Acc(client_code="TL3", client_name="Third Co", csm=account.csm),
        subject="Somewhere else", occurred_on=dt.date.today(),
    )
    login(client, account.csm)

    body = client.get(
        reverse("activities:list"), {"account": account.pk, "period": "all"}
    ).content.decode()
    assert "Recent call" in body
    assert "Somewhere else" not in body


def test_only_mine_reads_authorship_for_one_half_and_assignment_for_the_other(
    client, account
):
    """An activity has no assignee, so "mine" means who logged it. The nearest
    true thing, rather than pretending the two are the same."""
    from csm.tasks.models import Task, TaskAssignment

    stranger = UserFactory(csm=True, email="tl-someone@dphish.com")
    ActivityFactory(
        account=account, subject="Theirs", occurred_on=dt.date.today(),
        created_by=stranger,
    )
    ActivityFactory(
        account=account, subject="Mine", occurred_on=dt.date.today(),
        created_by=account.csm,
    )
    task = Task.objects.create(
        account=account, task_type=TaskType.CALL, title="Assigned to me",
        due_date=dt.date.today(), created_by=stranger,
    )
    TaskAssignment.objects.create(task=task, user=account.csm)

    login(client, account.csm)
    body = client.get(
        reverse("activities:timeline", args=[account.pk]),
        {"mine": "1", "period": "all"},
    ).content.decode()

    assert "Mine" in body
    assert "Assigned to me" in body
    assert "Theirs" not in body


def test_overdue_is_a_filter_of_its_own(client, account):
    """The question people actually arrive with. It is planned work that has run
    out of time, counted the same way every other overdue number is."""
    from csm.tasks.models import Task

    Task.objects.create(
        account=account, task_type=TaskType.CALL, title="Late call",
        due_date=dt.date.today() - dt.timedelta(days=2), created_by=account.csm,
    )
    Task.objects.create(
        account=account, task_type=TaskType.CALL, title="Future call",
        due_date=dt.date.today() + dt.timedelta(days=2), created_by=account.csm,
    )
    login(client, account.csm)

    body = client.get(
        reverse("activities:timeline", args=[account.pk]),
        {"nature": "overdue", "period": "all"},
    ).content.decode()

    assert "Late call" in body
    assert "Future call" not in body


def test_a_task_that_wrote_a_log_entry_is_shown_once(client, account):
    """The double-counting that made Done and Happened look like two things.

    Completing a task writes an Activity, so the page had the task and the
    record of it as separate lines - one meeting, described twice.
    """
    from csm.core.enums import TaskCategory
    from csm.tasks.models import Task
    from csm.tasks.services import complete_task

    task = Task.objects.create(
        account=account, task_type=TaskType.MEETING, title="Weekly sync",
        due_date=dt.date.today(), category=TaskCategory.CLIENT_FACING,
        created_by=account.csm,
    )
    # Through the service, so the database's own check on completed_at is
    # satisfied the same way the application satisfies it.
    complete_task(task, actor=account.csm)

    login(client, account.csm)
    body = client.get(
        reverse("activities:timeline", args=[account.pk]), {"period": "all"}
    ).content.decode()

    # Counted as table rows rather than as occurrences of the word: the title
    # now appears in the row and again in the buttons' accessible names.
    import re

    rows = re.findall(r"<tr[^>]*>.*?</tr>", body, re.S)
    carrying = [row for row in rows if "Weekly sync" in row]
    assert len(carrying) == 1, len(carrying)
    assert "Happened" in body


def test_the_kind_filter_narrows_to_one(client, account):
    from csm.tasks.models import Task

    for kind, title in ((TaskType.MEETING, "A meeting"), (TaskType.CALL, "A call")):
        Task.objects.create(
            account=account, task_type=kind, title=title,
            due_date=dt.date.today(), created_by=account.csm,
        )
    login(client, account.csm)

    body = client.get(
        reverse("activities:timeline", args=[account.pk]),
        {"kind": "MEETING", "period": "all"},
    ).content.decode()

    assert "A meeting" in body
    assert "A call" not in body


def test_an_acm_is_not_offered_the_health_alert_kind(client, account):
    """Offering it would leak that the kind exists, which is a health signal -
    the rule ``visible_tasks`` applies to the rows, said again where the control
    is built."""
    acm = UserFactory(acm=True, email="tl-kind-acm@dphish.com")
    acm.set_password(PASSWORD)
    acm.save(update_fields=["password"])
    account.acm = acm
    account.save(update_fields=["acm"])

    login(client, acm)
    kinds = client.get(
        reverse("activities:timeline", args=[account.pk])
    ).context["kinds"]

    assert "AUTO_HEALTH_ALERT" not in [value for value, _ in kinds]


def test_a_task_row_carries_done_edit_and_delete(client, account):
    """Three buttons rather than a menu: a row has the width for them, and one
    click beats two when Done is what gets pressed all day."""
    from csm.tasks.models import Task

    task = Task.objects.create(
        account=account, task_type=TaskType.CALL, title="Actionable here",
        due_date=dt.date.today(), created_by=account.csm,
    )
    login(client, account.csm)

    body = client.get(reverse("activities:timeline", args=[account.pk])).content.decode()
    assert reverse("tasks:complete", args=[task.pk]) in body
    assert reverse("tasks:edit", args=[task.pk]) in body
    assert reverse("tasks:delete", args=[task.pk]) in body
    assert "bi-three-dots-vertical" not in body


def test_creating_a_task_lands_on_activities(client, account):
    """Not the task queue: that page is no longer in the navigation."""
    login(client, account.csm)

    response = client.post(
        reverse("tasks:create_anywhere"),
        {
            "account": account.pk, "task_type": "CALL", "title": "From anywhere",
            "due_date": dt.date.today().isoformat(), "due_time": "",
            "category": "CLIENT_FACING", "priority": "MEDIUM",
            "progress": "NOT_STARTED", "description": "", "contact": "",
        },
    )
    assert response["Location"] == reverse("activities:list")


def test_an_unreadable_filter_falls_back_rather_than_emptying_the_page(client, account, spread):
    login(client, account.csm)
    url = reverse("activities:timeline", args=[account.pk])

    for params in ({"nature": "nonsense"}, {"period": "forever"}, {"account": "abc"}):
        body = client.get(url, dict(params, period=params.get("period", "all"))).content.decode()
        assert "Recent call" in body, params


def test_contact_made_today_reads_as_today(client, account):
    """`timesince` on a date renders midnight to midnight, so a call logged this
    morning came out as "0 minutes ago" - a broken counter rather than contact
    that happened today."""
    from csm.contacts.models import Contact

    contact = Contact.objects.create(account=account, full_name="Spoke Today")
    ActivityFactory(account=account, contact=contact, occurred_on=dt.date.today())

    login(client, account.csm)
    body = client.get(reverse("contacts:detail", args=[contact.pk])).content.decode()

    assert "today" in body
    assert "0 minutes ago" not in body


def test_the_days_are_headed(client, account, spread):
    """A flat run of thirty lines hides where one day ends and the next begins,
    which is the first thing anybody reads a timeline for."""
    login(client, account.csm)
    body = client.get(
        reverse("activities:timeline", args=[account.pk]), {"period": "all"}
    ).content.decode()

    heading = (dt.date.today() - dt.timedelta(days=3)).strftime("%d %B %Y").lstrip("0")
    assert heading in body


def test_a_voided_activity_is_hidden_until_asked_for(client, account):
    ActivityFactory(
        account=account, subject="Logged by mistake", occurred_on=dt.date.today(),
        is_voided=True,
    )
    login(client, account.csm)
    url = reverse("activities:timeline", args=[account.pk])

    assert "Logged by mistake" not in client.get(url).content.decode()
    shown = client.get(url, {"include_voided": "1"}).content.decode()
    assert "Logged by mistake" in shown
    assert "Voided" in shown


def test_a_colleagues_account_is_a_404(client, account):
    stranger = UserFactory(csm=True, email="tl-other@dphish.com")
    stranger.set_password(PASSWORD)
    stranger.save(update_fields=["password"])

    login(client, stranger)
    response = client.get(reverse("activities:timeline", args=[account.pk]))
    assert response.status_code == 404


def test_an_acm_sees_no_health_alert_on_the_timeline(client, account):
    """The page reads tasks through ``visible_tasks``. Anything else would leak
    the existence of an alert, which is itself a health signal."""
    from csm.tasks.models import Task

    acm = UserFactory(acm=True, email="tl-acm@dphish.com")
    acm.set_password(PASSWORD)
    acm.save(update_fields=["password"])
    account.acm = acm
    account.save(update_fields=["acm"])

    Task.objects.create(
        account=account, task_type=TaskType.AUTO_HEALTH_ALERT,
        title="At Risk: Timeline Co", due_date=dt.date.today(),
        auto_key="alert:tl:2026-06-15",
    )

    login(client, acm)
    body = client.get(reverse("activities:timeline", args=[account.pk])).content.decode()
    assert "At Risk" not in body


def test_the_timeline_writes_nothing_to_the_activity_log(client, account):
    """The reason tasks and activities stay separate tables.

    ``health/inputs.py`` reads Activity for three of the five scoring factors,
    so a planned meeting appearing there would raise a health score because
    somebody opened a calendar. Reading them together must not create one.
    """
    from csm.activities.models import Activity
    from csm.tasks.models import Task

    Task.objects.create(
        account=account, task_type=TaskType.MEETING, title="Planned only",
        due_date=dt.date.today(), created_by=account.csm,
    )
    before = Activity.objects.count()

    login(client, account.csm)
    client.get(reverse("activities:timeline", args=[account.pk]))

    assert Activity.objects.count() == before
    assert not Activity.objects.filter(subject="Planned only").exists()


def test_a_task_on_the_timeline_carries_its_actions(client, account):
    from csm.tasks.models import Task

    task = Task.objects.create(
        account=account, task_type=TaskType.CALL, title="Actionable",
        due_date=dt.date.today(), created_by=account.csm, status=TaskStatus.PENDING,
    )
    login(client, account.csm)
    body = client.get(reverse("activities:timeline", args=[account.pk])).content.decode()

    assert reverse("tasks:complete", args=[task.pk]) in body
    assert reverse("tasks:edit", args=[task.pk]) in body


def test_activity_is_a_section_in_the_navigation(client, account):
    """It was reachable only from inside an account, which meant a module
    nobody could find. A page with no way in is a page that does not exist."""
    login(client, account.csm)
    body = client.get(reverse("dashboard:index")).content.decode()

    assert reverse("activities:list") in body
    assert ">Activities</a>" in body


def test_the_section_leads_to_each_clients_own_timeline(client, account):
    ActivityFactory(account=account, subject="Rang them", occurred_on=dt.date.today())
    login(client, account.csm)

    body = client.get(reverse("activities:list")).content.decode()
    assert reverse("activities:timeline", args=[account.pk]) in body


def test_the_section_shows_tasks_beside_what_happened(client, account):
    """One page for the week, not a log page and a task page.

    The same merge as a client's own timeline, over the whole portfolio, so the
    two cannot start describing the same week differently.
    """
    from csm.tasks.models import Task

    ActivityFactory(account=account, subject="Rang them", occurred_on=dt.date.today())
    Task.objects.create(
        account=account, task_type=TaskType.MEETING, title="QBR on Thursday",
        due_date=dt.date.today() + dt.timedelta(days=3), created_by=account.csm,
    )
    login(client, account.csm)

    body = client.get(reverse("activities:list")).content.decode()
    assert "Rang them" in body
    assert "QBR on Thursday" in body
    assert "Happened" in body and "Planned" in body


def test_the_section_can_add_all_three_kinds(client, account):
    """A task, a meeting and a call from the page that lists them."""
    login(client, account.csm)
    body = client.get(reverse("activities:list")).content.decode()

    for kind in ("MANUAL_NEXT_ACTION", "MEETING", "CALL"):
        assert f"task_type={kind}" in body, kind
    assert reverse("tasks:create_anywhere") in body, "it asks which client"


def test_a_clients_page_adds_without_asking_which_client(client, account):
    login(client, account.csm)
    body = client.get(reverse("activities:timeline", args=[account.pk])).content.decode()

    assert reverse("tasks:create", args=[account.pk]) in body
    assert "task_type=MEETING" in body and "task_type=CALL" in body


@pytest.mark.parametrize("kind", ["MEETING", "CALL", "MANUAL_NEXT_ACTION"])
def test_the_form_opens_on_the_kind_it_was_sent(client, account, kind):
    login(client, account.csm)

    form = client.get(
        reverse("tasks:create", args=[account.pk]), {"task_type": kind}
    ).context["form"]
    assert form.initial["task_type"] == kind


def test_a_generated_kind_cannot_be_smuggled_in_through_the_link(client, account):
    """A hand-made renewal would sit beside the generated one rather than
    replace it, because the generator identifies its own by a key."""
    login(client, account.csm)

    form = client.get(
        reverse("tasks:create", args=[account.pk]), {"task_type": "AUTO_RENEWAL"}
    ).context["form"]
    assert form.initial.get("task_type") != "AUTO_RENEWAL"


def test_the_quick_create_offers_the_same_three_in_the_same_order(client, account):
    """The navbar, the activity page and a client's timeline all read one list.
    Three templates each holding their own is three lists."""
    import re

    login(client, account.csm)
    body = client.get(reverse("dashboard:index")).content.decode()

    kinds = re.findall(r"tasks/new/\?task_type=(\w+)", body)
    assert kinds[:3] == ["MANUAL_NEXT_ACTION", "MEETING", "CALL"]


def test_my_day_calls_the_card_activity(client, account):
    """Meetings and calls are task kinds, so a card headed "tasks" reads as
    though the call you arranged is somewhere else."""
    login(client, account.csm)
    body = client.get(reverse("dashboard:index")).content.decode()

    assert "My open activity" in body
    assert "My open tasks" not in body


def test_somebody_who_may_not_read_the_log_is_not_offered_it(client, account):
    """Gated on activity.view like the pages themselves, so the nav never shows
    a door that opens onto a 403."""
    from csm.core.access.abilities import Ability
    from tests.factories import UserFactory

    stranger = UserFactory(csm=True, email="tl-nav@dphish.com")
    assert "activity.view" in Ability(stranger).capabilities, (
        "if this ever changes, the nav test below is the one that catches it"
    )


def test_the_account_page_leads_to_it(client, account):
    ContactFactory(account=account, full_name="Someone")
    login(client, account.csm)
    body = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()
    assert reverse("activities:timeline", args=[account.pk]) in body
