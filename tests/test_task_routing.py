"""Routing tests.

The matrix itself is pure, so most of this needs no database at all.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from csm.core.enums import LifecycleStage, TaskAssignmentRole, TaskType
from csm.notifications import routing
from csm.notifications.routing import Recipient, RoutingContext, resolve_recipients

CSM_ID, ACM_ID, SENIOR_ID, STRANGER_ID = 1, 2, 3, 4


def ctx(**kwargs) -> RoutingContext:
    defaults = {
        "task_type": TaskType.MANUAL_NEXT_ACTION.value,
        "lifecycle_stage": LifecycleStage.ACTIVE.value,
        "csm_id": CSM_ID,
        "acm_id": ACM_ID,
        "creator_id": None,
    }
    defaults.update(kwargs)
    return RoutingContext(**defaults)


def ids(recipients) -> set[int]:
    return {r.user_id for r in recipients}


# ---------------------------------------------------------------------------
# The structural guarantee
# ---------------------------------------------------------------------------


def test_routing_never_reads_a_role_or_seniority():
    """Access is broad; notifications follow assignment. Enforced structurally.

    Checked against the parsed syntax tree rather than the text, so prose in a
    docstring cannot fail it and a real reference cannot hide from it. If this
    fails, someone has given the routing layer a way to widen who gets notified
    based on who somebody is rather than on what they are assigned.
    """
    tree = ast.parse(inspect.getsource(routing))

    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(m.startswith("csm.core.access") for m in imported)

    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "is_senior" not in attributes
    assert "role" not in attributes

    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "Role" not in names
    assert "User" not in names


def test_routing_context_carries_only_assignment_facts():
    fields = set(RoutingContext.__dataclass_fields__)
    assert fields == {"task_type", "lifecycle_stage", "csm_id", "acm_id", "creator_id"}


# ---------------------------------------------------------------------------
# Priority 1: the POC exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_type", [t.value for t in TaskType])
def test_poc_routes_every_task_type_to_both(task_type):
    recipients = resolve_recipients(
        ctx(task_type=task_type, lifecycle_stage=LifecycleStage.POC.value)
    )
    assert ids(recipients) == {CSM_ID, ACM_ID}


def test_poc_beats_the_senior_reassignment_rule():
    """Priority one wins outright, which is the correct reading of the spec."""
    recipients = resolve_recipients(
        ctx(
            task_type=TaskType.MANUAL_NEXT_ACTION.value,
            lifecycle_stage=LifecycleStage.POC.value,
            creator_id=SENIOR_ID,
        )
    )
    assert ids(recipients) == {CSM_ID, ACM_ID}


def test_poc_alone_is_the_exception_no_other_stage_shares_tasks():
    for stage in LifecycleStage:
        if stage == LifecycleStage.POC:
            continue
        recipients = resolve_recipients(
            ctx(task_type=TaskType.AUTO_HEALTH_ALERT.value, lifecycle_stage=stage.value)
        )
        assert ids(recipients) == {CSM_ID}


# ---------------------------------------------------------------------------
# Priority 2: by task type
# ---------------------------------------------------------------------------


def test_health_alert_goes_to_the_csm_alone():
    recipients = resolve_recipients(ctx(task_type=TaskType.AUTO_HEALTH_ALERT.value))
    assert ids(recipients) == {CSM_ID}
    assert ACM_ID not in ids(recipients)


@pytest.mark.parametrize(
    "task_type", [TaskType.AUTO_RENEWAL.value, TaskType.AUTO_QUARTERLY_MEETING.value]
)
def test_renewal_and_quarterly_go_to_both(task_type):
    assert ids(resolve_recipients(ctx(task_type=task_type))) == {CSM_ID, ACM_ID}


def test_manual_task_goes_to_its_creator_when_they_own_the_account():
    assert ids(resolve_recipients(ctx(creator_id=CSM_ID))) == {CSM_ID}
    assert ids(resolve_recipients(ctx(creator_id=ACM_ID))) == {ACM_ID}


def test_manual_task_from_a_senior_on_someone_elses_account_goes_to_that_csm():
    """A senior helping out is not taking the work over."""
    recipients = resolve_recipients(ctx(creator_id=SENIOR_ID))
    assert ids(recipients) == {CSM_ID}
    assert SENIOR_ID not in ids(recipients)


def test_manual_task_records_the_capacity_the_recipient_holds():
    recipients = resolve_recipients(ctx(creator_id=ACM_ID))
    assert recipients[0].assigned_as == TaskAssignmentRole.ACM


# ---------------------------------------------------------------------------
# Missing assignments
# ---------------------------------------------------------------------------


def test_an_account_with_no_csm_still_routes_its_renewal_to_the_acm():
    recipients = resolve_recipients(ctx(task_type=TaskType.AUTO_RENEWAL.value, csm_id=None))
    assert ids(recipients) == {ACM_ID}


def test_a_health_alert_on_an_account_with_no_csm_resolves_to_nobody():
    """Deliberately empty, so the task surfaces in the unassigned bucket."""
    assert resolve_recipients(ctx(task_type=TaskType.AUTO_HEALTH_ALERT.value, csm_id=None)) == ()


def test_a_manual_task_with_no_csm_falls_back_to_its_creator():
    recipients = resolve_recipients(ctx(csm_id=None, acm_id=None, creator_id=STRANGER_ID))
    assert ids(recipients) == {STRANGER_ID}


def test_the_same_person_as_csm_and_acm_is_not_notified_twice():
    recipients = resolve_recipients(
        ctx(task_type=TaskType.AUTO_RENEWAL.value, csm_id=CSM_ID, acm_id=CSM_ID)
    )
    assert len(recipients) == 1


# ---------------------------------------------------------------------------
# Meetings and calls are hand-made tasks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [TaskType.MEETING, TaskType.CALL])
def test_a_meeting_or_a_call_routes_like_any_hand_made_task(kind):
    """Made by a person, so it goes to the person who made it - and to the
    account's own CSM when a senior made it on somebody else's account."""
    assert resolve_recipients(ctx(task_type=kind, creator_id=CSM_ID)) == (
        Recipient(CSM_ID, TaskAssignmentRole.CSM.value),
    )
    assert resolve_recipients(ctx(task_type=kind, creator_id=SENIOR_ID)) == (
        Recipient(CSM_ID, TaskAssignmentRole.CSM.value),
    )


@pytest.mark.parametrize("kind", [TaskType.MEETING, TaskType.CALL])
def test_a_meeting_or_a_call_still_obeys_the_poc_exception(kind):
    assert resolve_recipients(
        ctx(task_type=kind, lifecycle_stage=LifecycleStage.POC, creator_id=CSM_ID)
    ) == (
        Recipient(CSM_ID, TaskAssignmentRole.CSM.value),
        Recipient(ACM_ID, TaskAssignmentRole.ACM.value),
    )


def test_every_kind_a_person_can_create_is_routed_by_name():
    """A type nobody routes falls through to "both", which is a silent default.

    Adding a kind to the enum and forgetting this module is the way that
    happens, so the list of hand-made kinds is asserted against the form's.
    """
    from csm.core.enums import MANUAL_TASK_TYPES

    assert set(MANUAL_TASK_TYPES) == {
        TaskType.MANUAL_NEXT_ACTION, TaskType.MEETING, TaskType.CALL
    }
    for kind in MANUAL_TASK_TYPES:
        assert resolve_recipients(ctx(task_type=kind, creator_id=CSM_ID)) == (
            Recipient(CSM_ID, TaskAssignmentRole.CSM.value),
        ), kind


# ---------------------------------------------------------------------------
# Overdue: the SQL half and the Python half must agree
# ---------------------------------------------------------------------------
# ``TaskQuerySet.overdue`` and ``Task.is_overdue`` answer the same question in
# two languages, which is the shape of bug this codebase has already had once
# with the power/interest mapping: both sides look right, they disagree at one
# boundary, and nothing tests the pair.


@pytest.fixture
def clock_spread(db):
    """One task per interesting position around a fixed 'now' of 14:00."""
    import datetime as dt

    from csm.core.enums import TaskType
    from csm.tasks.models import Task
    from tests.factories import AccountFactory

    account = AccountFactory(client_code="OVD1")
    today = dt.date(2026, 6, 15)
    made = {}
    for name, due_date, due_time in (
        ("yesterday_no_time", today - dt.timedelta(days=1), None),
        ("yesterday_late_time", today - dt.timedelta(days=1), dt.time(23, 59)),
        ("today_no_time", today, None),
        ("today_passed", today, dt.time(9, 0)),
        ("today_exactly_now", today, dt.time(14, 0)),
        ("today_ahead", today, dt.time(17, 30)),
        ("tomorrow_early", today + dt.timedelta(days=1), dt.time(0, 1)),
    ):
        made[name] = Task.objects.create(
            account=account, task_type=TaskType.MANUAL_NEXT_ACTION,
            title=name, due_date=due_date, due_time=due_time,
        )
    return made


@pytest.mark.django_db
def test_the_query_and_the_model_agree_on_what_is_overdue(clock_spread):
    import datetime as dt

    from csm.tasks.models import Task

    today = dt.date(2026, 6, 15)
    for as_of, as_of_time in (
        (today, dt.time(0, 0)),
        (today, dt.time(8, 59)),
        (today, dt.time(9, 1)),
        (today, dt.time(14, 0)),
        (today, dt.time(23, 59)),
        (today + dt.timedelta(days=1), dt.time(0, 1)),
    ):
        from_sql = set(Task.objects.overdue(as_of, as_of_time).values_list("title", flat=True))
        from_python = {
            task.title
            for task in Task.objects.all()
            if task.is_overdue(as_of, as_of_time)
        }
        assert from_sql == from_python, (as_of, as_of_time)


@pytest.mark.django_db
def test_a_task_goes_overdue_when_its_time_passes(clock_spread):
    """The reported bug: it only went overdue at midnight.

    A 09:00 task that is still open at two in the afternoon is late, and every
    counter, badge and list in the application now says so.
    """
    import datetime as dt

    from csm.tasks.models import Task

    today = dt.date(2026, 6, 15)
    before = set(Task.objects.overdue(today, dt.time(8, 59)).values_list("title", flat=True))
    after = set(Task.objects.overdue(today, dt.time(14, 0)).values_list("title", flat=True))

    assert "today_passed" not in before
    assert "today_passed" in after


@pytest.mark.django_db
def test_a_task_with_no_time_waits_for_the_day_to_end(clock_spread):
    """Sticking with the date when there is no time is the whole point of the
    column being optional: "due today" has always meant any time today."""
    import datetime as dt

    from csm.tasks.models import Task

    today = dt.date(2026, 6, 15)
    late = set(Task.objects.overdue(today, dt.time(23, 59)).values_list("title", flat=True))
    assert "today_no_time" not in late
    assert "yesterday_no_time" in late

    tomorrow = set(
        Task.objects.overdue(today + dt.timedelta(days=1), dt.time(0, 1))
        .values_list("title", flat=True)
    )
    assert "today_no_time" in tomorrow


@pytest.mark.django_db
def test_a_task_due_at_this_exact_minute_is_not_yet_late(clock_spread):
    """Strictly after, not at. A task due at 14:00 is not late at 14:00."""
    import datetime as dt

    from csm.tasks.models import Task

    at = set(Task.objects.overdue(dt.date(2026, 6, 15), dt.time(14, 0)).values_list(
        "title", flat=True
    ))
    assert "today_exactly_now" not in at


@pytest.mark.django_db
def test_a_finished_task_is_never_overdue(clock_spread):
    import datetime as dt

    from csm.core.enums import TaskStatus
    from csm.tasks.models import Task

    task = clock_spread["today_passed"]
    task.status = TaskStatus.DONE
    task.completed_at = dt.datetime.now(dt.UTC)
    task.save(update_fields=["status", "completed_at"])

    as_of, as_of_time = dt.date(2026, 6, 15), dt.time(23, 0)
    assert task.title not in set(
        Task.objects.overdue(as_of, as_of_time).values_list("title", flat=True)
    )
    task.refresh_from_db()
    assert task.is_overdue(as_of, as_of_time) is False


@pytest.mark.django_db
def test_the_pages_count_a_timed_task_as_overdue(client, clock_spread):
    """Through the real pages, because the counters are where it was wrong.

    14:00 Cairo is 12:00 UTC in June, when Egypt is on +03:00 - pinned in UTC
    so the test says which instant it means.
    """
    import time_machine
    from django.urls import reverse

    from tests.factories import UserFactory
    from tests.test_views import PASSWORD, login

    user = UserFactory(senior_csm=True, email="ovd-senior@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    login(client, user)

    def overdue_titles(response):
        return {task.title for task in response.context["object_list"]}

    with time_machine.travel("2026-06-15 05:59:00+00:00", tick=False):
        early = client.get(reverse("tasks:list"), {"state": "overdue", "scope": "all"})
        early_titles = overdue_titles(early)
    with time_machine.travel("2026-06-15 12:00:00+00:00", tick=False):
        late = client.get(reverse("tasks:list"), {"state": "overdue", "scope": "all"})
        late_titles = overdue_titles(late)
        dashboard = client.get(reverse("dashboard:index")).context["overdue_count"]

    # 08:59 Cairo: only yesterday's. 15:00 Cairo: the 09:00 and 14:00 ones too.
    assert early_titles == {"yesterday_no_time", "yesterday_late_time"}
    assert late_titles == early_titles | {"today_passed", "today_exactly_now"}
    assert "today_ahead" not in late_titles, "17:30 has not come round yet"
    assert "today_no_time" not in late_titles, "no time means any time today"
    assert dashboard == len(late_titles), "and the dashboard says the same number"
