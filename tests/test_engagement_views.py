"""Engagement entry, account create/delete, and in-app notifications.

The engagement forms are the ones that move the score, so the tests that matter
are that they do move it, and that the capability split holds: an ACM touches
none of tickets, CSAT or adoption, because all three feed the health picture
they are kept clear of.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from csm.accounts.models import Account
from csm.core.enums import (
    HealthStatus,
    NotificationChannel,
    NotificationKind,
    TicketKind,
    TicketStatus,
)
from csm.engagement.models import AdoptionEvent, CsatResponse, EngagementItem, Ticket
from csm.notifications.models import Notification
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login


@pytest.fixture
def people(db):
    users = {
        "csm": UserFactory(csm=True, email="eng-csm@dphish.com", full_name="Casey Morgan"),
        "senior": UserFactory(senior_csm=True, email="eng-senior@dphish.com"),
        "acm": UserFactory(acm=True, email="eng-acm@dphish.com"),
        "manager": UserFactory(manager=True, email="eng-manager@dphish.com"),
    }
    for user in users.values():
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    return users


@pytest.fixture
def account(people):
    return AccountFactory(
        client_code="ENG01", client_name="Engagement Co",
        csm=people["csm"], acm=people["acm"],
    )


# ---------------------------------------------------------------------------
# Who may record what
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url_name, allowed",
    [
        ("engagement:ticket_create", {"csm", "senior"}),
        ("engagement:csat_create", {"csm", "senior"}),
        ("engagement:adoption_create", {"csm", "senior"}),
        ("engagement:item_create", {"csm", "senior"}),
    ],
)
@pytest.mark.parametrize("role", ["csm", "senior", "acm", "manager"])
def test_the_capability_split_holds_on_every_engagement_form(
    client, account, people, url_name, allowed, role
):
    """An ACM records none of tickets, CSAT or adoption: all three feed the
    health picture they are kept clear of."""
    login(client, people[role])
    response = client.get(reverse(url_name, args=[account.pk]))

    expected = 200 if role in allowed else 403
    assert response.status_code == expected, f"{role} on {url_name}"


def test_a_manager_may_read_the_score_inputs_but_record_nothing(client, account, people):
    """Tickets, CSAT and adoption moved to the health page, beside the factors
    they move; the events page keeps the trend data that feeds no score."""
    login(client, people["manager"])
    health = client.get(reverse("health:panel", args=[account.pk])).content.decode()
    events = client.get(reverse("engagement:panel", args=[account.pk])).content.decode()

    assert "Tickets" in health
    assert "Events and digital content" in events
    # None of the add links are offered on either.
    for body in (health, events):
        assert reverse("engagement:ticket_create", args=[account.pk]) not in body
        assert reverse("engagement:csat_create", args=[account.pk]) not in body


def test_the_panel_is_row_scoped(client, people):
    theirs = AccountFactory(
        client_code="OTHER", csm=UserFactory(csm=True, email="eng-other@dphish.com")
    )
    login(client, people["csm"])
    assert client.get(reverse("engagement:panel", args=[theirs.pk])).status_code == 404


# ---------------------------------------------------------------------------
# Recording moves the score
# ---------------------------------------------------------------------------


def test_recording_a_bug_ticket_rescores_the_account(
    client, account, people, django_capture_on_commit_callbacks
):
    """No view calls the engine. The model hook does, after commit.

    The callbacks have to be captured explicitly: a test runs inside a
    transaction that never commits, so ``transaction.on_commit`` would never
    fire and the rescore this asserts would silently not happen.
    """
    from csm.health import signals

    # Creating the account fixture already queued a recalculation that will
    # never commit, and the guard deduplicates against it. Clearing here is
    # what a real commit would have done between the two saves.
    signals._pending.clear()

    login(client, people["csm"])
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("engagement:ticket_create", args=[account.pk]),
            {
                "ticket_kind": TicketKind.BUG, "status": TicketStatus.OPEN,
                "title": "Sync failing", "description": "",
                "opened_on": dt.date.today().isoformat(), "closed_on": "",
            },
        )

    ticket = Ticket.objects.get()
    assert ticket.account == account
    assert ticket.created_by == people["csm"]

    account.refresh_from_db()
    assert account.health_calculated_at is not None


def test_a_closed_ticket_needs_a_closing_date(client, account, people):
    login(client, people["csm"])
    response = client.post(
        reverse("engagement:ticket_create", args=[account.pk]),
        {
            "ticket_kind": TicketKind.GENERAL, "status": TicketStatus.CLOSED,
            "title": "Done", "description": "",
            "opened_on": dt.date.today().isoformat(), "closed_on": "",
        },
    )
    assert response.status_code == 200
    assert "closing date" in response.content.decode()
    assert Ticket.objects.count() == 0


def test_an_open_ticket_may_not_carry_a_closing_date(client, account, people):
    login(client, people["csm"])
    client.post(
        reverse("engagement:ticket_create", args=[account.pk]),
        {
            "ticket_kind": TicketKind.GENERAL, "status": TicketStatus.OPEN,
            "title": "Still open", "description": "",
            "opened_on": dt.date.today().isoformat(),
            "closed_on": dt.date.today().isoformat(),
        },
    )
    assert Ticket.objects.count() == 0


def test_closing_a_ticket_from_the_panel_stamps_today(client, account, people):
    ticket = Ticket.objects.create(
        account=account, ticket_kind=TicketKind.BUG, status=TicketStatus.OPEN,
        title="Sync failing", opened_on=dt.date.today() - dt.timedelta(days=3),
        created_by=people["csm"],
    )
    login(client, people["csm"])
    client.post(reverse("engagement:ticket_close", args=[account.pk, ticket.pk]))

    ticket.refresh_from_db()
    assert ticket.status == TicketStatus.CLOSED
    assert ticket.closed_on == dt.date.today()


def test_an_acm_may_not_record_csat(client, account, people):
    login(client, people["acm"])
    response = client.post(
        reverse("engagement:csat_create", args=[account.pk]),
        {"score": 5, "recorded_on": dt.date.today().isoformat(), "contact": "", "comment": "Happy"},
    )
    assert response.status_code == 403
    assert CsatResponse.objects.count() == 0


def test_a_rating_cannot_be_recorded_in_the_future(client, account, people):
    login(client, people["csm"])
    client.post(
        reverse("engagement:csat_create", args=[account.pk]),
        {
            "score": 4,
            "recorded_on": (dt.date.today() + dt.timedelta(days=1)).isoformat(),
            "contact": "", "comment": "",
        },
    )
    assert CsatResponse.objects.count() == 0


def test_recording_an_adoption_event(client, account, people):
    login(client, people["csm"])
    client.post(
        reverse("engagement:adoption_create", args=[account.pk]),
        {
            "event_type": "PHISHING_CAMPAIGN",
            "occurred_on": dt.date.today().isoformat(),
            "title": "Q3 campaign", "notes": "",
        },
    )
    assert AdoptionEvent.objects.get().title == "Q3 campaign"


def test_recording_an_engagement_item_for_the_trend(client, account, people):
    login(client, people["csm"])
    client.post(
        reverse("engagement:item_create", args=[account.pk]),
        {
            "item_type": "EVENT", "occurred_on": dt.date.today().isoformat(),
            "title": "User group", "location": "Cairo", "quantity": 1,
        },
    )
    assert EngagementItem.objects.get().title == "User group"


def test_an_acm_cannot_delete_a_ticket(client, account, people):
    ticket = Ticket.objects.create(
        account=account, ticket_kind=TicketKind.BUG, status=TicketStatus.OPEN,
        title="Sync failing", opened_on=dt.date.today(), created_by=people["csm"],
    )
    login(client, people["acm"])
    response = client.post(
        reverse("engagement:delete", args=[account.pk, "ticket", ticket.pk])
    )
    assert response.status_code == 403
    assert Ticket.objects.count() == 1


def test_an_unknown_record_kind_is_a_404_not_a_crash(client, account, people):
    login(client, people["csm"])
    response = client.post(f"/engagement/account/{account.pk}/nonsense/1/delete/")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Account create and delete
# ---------------------------------------------------------------------------


def test_a_csm_can_create_an_account_with_an_import_style_baseline(client, people):
    """A new account with no history must not read as months of neglect."""
    login(client, people["csm"])
    response = client.post(
        reverse("accounts:create"),
        {
            "client_code": "NEW01", "client_name": "Brand New Co",
            "deployment": "CLOUD", "account_class": "B", "size": "MID",
            "lifecycle_stage": "ACTIVE", "boq_users_count": 10,
            "added_users_count": 0, "renewal_count": 0,
            "events_count": 0, "digital_content_count": 0,
            "g2_review_status": "NONE", "gartner_review_status": "NONE",
        },
    )
    assert response.status_code == 302

    account = Account.objects.get(client_code="NEW01")
    assert account.created_by == people["csm"]
    assert account.scoring_baseline_date == dt.date.today()


def test_an_acm_cannot_create_an_account(client, people):
    login(client, people["acm"])
    assert client.get(reverse("accounts:create")).status_code == 403


def test_a_csm_may_delete_their_own_account_but_an_acm_or_manager_cannot(client, account, people):
    for role in ("acm", "manager"):
        login(client, people[role])
        assert client.get(reverse("accounts:delete", args=[account.pk])).status_code == 403

    for role in ("csm", "senior"):
        login(client, people[role])
        assert client.get(reverse("accounts:delete", args=[account.pk])).status_code == 200


def test_deleting_hides_the_account_without_destroying_its_history(client, account, people):
    from csm.activities.models import Activity

    Activity.objects.create(
        account=account, activity_type="CALL", occurred_on=dt.date.today(),
        created_by=people["csm"],
    )
    login(client, people["senior"])
    client.post(reverse("accounts:delete", args=[account.pk]))

    account.refresh_from_db()
    assert account.deleted_at is not None
    assert account.deleted_by == people["senior"]
    # Gone from every list, but the history survives.
    assert not Account.objects.alive().filter(pk=account.pk).exists()
    assert Activity.objects.filter(account=account).exists()


def test_a_deleted_account_disappears_from_the_list(client, account, people):
    login(client, people["senior"])
    client.post(reverse("accounts:delete", args=[account.pk]))

    # Asserted on the queryset, not the page text: the success message names the
    # account it just deleted, so a substring check passes for the wrong reason.
    response = client.get(reverse("accounts:list"))
    assert account not in response.context["object_list"]
    assert reverse("accounts:detail", args=[account.pk]) not in response.content.decode()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def _notify(user, account, title="Renewal due", read=False):
    return Notification.objects.create(
        user=user, account=account, kind=NotificationKind.TASK_ASSIGNED,
        channel=NotificationChannel.IN_APP, title=title,
        read_at=dt.datetime.now(dt.UTC) if read else None,
    )


def test_a_user_sees_only_their_own_notifications(client, account, people):
    _notify(people["csm"], account, "Mine")
    _notify(people["acm"], account, "Theirs")

    login(client, people["csm"])
    body = client.get(reverse("notifications:list")).content.decode()

    assert "Mine" in body
    assert "Theirs" not in body


def test_a_senior_csm_does_not_see_a_colleagues_notifications(client, account, people):
    """Broad account access must not widen who is notified."""
    _notify(people["csm"], account, "Casey only")

    login(client, people["senior"])
    body = client.get(reverse("notifications:list")).content.decode()
    assert "Casey only" not in body


def test_the_list_shows_unread_first_and_all_on_request(client, account, people):
    _notify(people["csm"], account, "Unread one")
    _notify(people["csm"], account, "Already read", read=True)

    login(client, people["csm"])
    unread = client.get(reverse("notifications:list")).content.decode()
    assert "Unread one" in unread
    assert "Already read" not in unread

    everything = client.get(reverse("notifications:list"), {"show": "all"}).content.decode()
    assert "Already read" in everything


def test_marking_one_read_follows_it_to_the_account(client, account, people):
    note = _notify(people["csm"], account)
    login(client, people["csm"])

    response = client.post(reverse("notifications:read", args=[note.pk]))
    note.refresh_from_db()

    assert note.read_at is not None
    assert str(account.pk) in response.url


def test_marking_all_read_clears_the_badge(client, account, people):
    for n in range(3):
        _notify(people["csm"], account, f"Note {n}")

    login(client, people["csm"])
    assert client.get(reverse("dashboard:index")).context["unread_notifications"] == 3

    client.post(reverse("notifications:read_all"))
    assert client.get(reverse("dashboard:index")).context["unread_notifications"] == 0


def test_someone_elses_notification_cannot_be_marked_read(client, account, people):
    note = _notify(people["acm"], account)
    login(client, people["csm"])

    assert client.post(reverse("notifications:read", args=[note.pk])).status_code == 404
    note.refresh_from_db()
    assert note.read_at is None


def test_the_badge_counts_only_in_app_notifications(client, account, people):
    """Email rows are the outbox; counting them would double every alert."""
    _notify(people["csm"], account, "In app")
    Notification.objects.create(
        user=people["csm"], account=account, kind=NotificationKind.HEALTH_ALERT,
        channel=NotificationChannel.EMAIL, title="Emailed",
    )

    login(client, people["csm"])
    assert client.get(reverse("dashboard:index")).context["unread_notifications"] == 1


def test_an_at_risk_account_still_renders_for_a_manager(client, people):
    """A read-only role reaching every new surface."""
    AccountFactory(
        client_code="RISK1", csm=people["csm"],
        effective_health_status=HealthStatus.AT_RISK,
    )
    login(client, people["manager"])
    assert client.get(reverse("notifications:list")).status_code == 200
    assert client.get(reverse("accounts:list")).status_code == 200


# ---------------------------------------------------------------------------
# Task edit and delete
# ---------------------------------------------------------------------------


def _manual_task(account, user, **overrides):
    from csm.core.enums import TaskType
    from csm.tasks.models import Task

    values = {
        "account": account,
        "task_type": TaskType.MANUAL_NEXT_ACTION,
        "title": "Call them back",
        "due_date": dt.date.today(),
        "created_by": user,
    }
    values.update(overrides)
    return Task.objects.create(**values)


def test_a_manual_task_can_be_edited(client, account, people):
    task = _manual_task(account, people["csm"])
    login(client, people["csm"])

    client.post(
        reverse("tasks:edit", args=[task.pk]),
        {
            "title": "Call them back twice", "due_date": dt.date.today().isoformat(),
            "category": "INTERNAL", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION", "description": "", "contact": "",
        },
    )
    task.refresh_from_db()
    assert task.title == "Call them back twice"


def test_an_overdue_task_is_still_editable(client, account, people):
    """Enforcing the not-already-overdue rule on edit would make an overdue task
    uneditable, which is the opposite of what chasing one requires."""
    task = _manual_task(
        account, people["csm"], due_date=dt.date.today() - dt.timedelta(days=5)
    )
    login(client, people["csm"])

    response = client.post(
        reverse("tasks:edit", args=[task.pk]),
        {
            "title": "Chased", "due_date": (dt.date.today() - dt.timedelta(days=5)).isoformat(),
            "category": "INTERNAL", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION", "description": "", "contact": "",
        },
    )
    assert response.status_code == 302
    task.refresh_from_db()
    assert task.title == "Chased"


def test_a_generated_task_is_editable(client, account, people):
    """Every task is editable, generated ones included."""
    from csm.core.enums import TaskType

    task = _manual_task(
        account, people["csm"], task_type=TaskType.AUTO_RENEWAL,
        auto_key="renewal:1:2026-12-31:30", title="Renewal due",
    )
    login(client, people["csm"])

    response = client.post(
        reverse("tasks:edit", args=[task.pk]),
        {
            "title": "Renewal due - call the CFO first",
            "due_date": dt.date.today().isoformat(),
            "category": "CLIENT_FACING", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION", "description": "Ask about seat expansion.",
            "contact": "",
        },
    )
    assert response.status_code == 302
    task.refresh_from_db()
    assert task.title == "Renewal due - call the CFO first"
    assert task.description == "Ask about seat expansion."


def test_an_edit_to_a_generated_task_survives_the_generator(client, people):
    """The claim that a rerun would clobber the edit was wrong, so assert it.

    ``auto_key`` is built from the account and the date the obligation derives
    from, never from the title or due date, and the generator creates through
    ``create_task`` - which the unique index turns into a no-op when the key is
    already there. It never updates a task it finds.
    """
    from csm.tasks.generators import generate_renewal_tasks, renewal_key
    from csm.tasks.models import Task

    end_date = dt.date.today() + dt.timedelta(days=30)
    renewal_account = AccountFactory(
        client_code="RENEW1", client_name="Renewing Co",
        csm=people["csm"], contract_end_date=end_date,
    )

    # Let the generator create the real thing rather than hand-rolling a key.
    assert generate_renewal_tasks() >= 1
    task = Task.objects.get(auto_key=renewal_key(renewal_account.pk, end_date, 30))
    original_key = task.auto_key

    login(client, people["csm"])
    client.post(
        reverse("tasks:edit", args=[task.pk]),
        {
            "title": "Reworded by hand", "due_date": dt.date.today().isoformat(),
            "category": "INTERNAL", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION", "description": "", "contact": "",
        },
    )

    # Run it again, exactly as the nightly job would.
    generate_renewal_tasks()

    task.refresh_from_db()
    assert task.title == "Reworded by hand", "the generator overwrote a hand edit"
    assert task.auto_key == original_key
    assert Task.objects.filter(auto_key=original_key).count() == 1, "a duplicate appeared"


def test_moving_the_contract_date_replaces_a_generated_task(client, people):
    """The edit is not lost here - the obligation changed, so a new one is right."""
    from csm.core.enums import TaskStatus
    from csm.tasks.generators import cancel_stale_renewal_tasks, generate_renewal_tasks
    from csm.tasks.models import Task

    end_date = dt.date.today() + dt.timedelta(days=30)
    renewal_account = AccountFactory(
        client_code="RENEW2", client_name="Moving Co",
        csm=people["csm"], contract_end_date=end_date,
    )
    generate_renewal_tasks()
    original = Task.objects.get(account=renewal_account)

    previous = renewal_account.contract_end_date
    renewal_account.contract_end_date = end_date + dt.timedelta(days=365)
    renewal_account.save(update_fields=["contract_end_date"])
    cancel_stale_renewal_tasks(renewal_account, previous)

    original.refresh_from_db()
    assert original.status == TaskStatus.CANCELLED


def test_deleting_a_manual_task_removes_it(client, account, people):
    from csm.tasks.models import Task

    task = _manual_task(account, people["csm"])
    login(client, people["csm"])

    client.post(reverse("tasks:delete", args=[task.pk]))
    assert not Task.objects.filter(pk=task.pk).exists()


def test_deleting_a_generated_task_cancels_it_instead(client, account, people):
    """A hard delete frees the idempotency key and the task reappears."""
    from csm.core.enums import TaskStatus, TaskType
    from csm.tasks.models import Task

    task = _manual_task(
        account, people["csm"], task_type=TaskType.AUTO_RENEWAL,
        auto_key="renewal:1:2026-12-31:60", title="Renewal due",
    )
    login(client, people["csm"])

    client.post(reverse("tasks:delete", args=[task.pk]))
    task.refresh_from_db()

    assert Task.objects.filter(pk=task.pk).exists()
    assert task.status == TaskStatus.CANCELLED


def test_a_manager_cannot_edit_or_delete_a_task(client, account, people):
    task = _manual_task(account, people["csm"])
    login(client, people["manager"])

    assert client.get(reverse("tasks:edit", args=[task.pk])).status_code == 403
    assert client.post(reverse("tasks:delete", args=[task.pk])).status_code == 403


def test_a_task_on_a_colleagues_account_cannot_be_edited(client, people):
    other = AccountFactory(
        client_code="OTH2", csm=UserFactory(csm=True, email="eng-other2@dphish.com")
    )
    task = _manual_task(other, people["csm"])
    login(client, people["csm"])

    assert client.get(reverse("tasks:edit", args=[task.pk])).status_code == 404


# ---------------------------------------------------------------------------
# Priority and status, on the pages that list tasks
# ---------------------------------------------------------------------------


def test_a_meeting_and_a_call_can_be_created_by_hand(client, account, people):
    """A meeting or a call is something you arrange and then do, which is a
    task. The activity log records the one that happened."""
    from csm.tasks.models import Task

    login(client, people["csm"])
    for kind, title in (("MEETING", "QBR with the CISO"), ("CALL", "Ring them back")):
        client.post(
            reverse("tasks:create", args=[account.pk]),
            {
                "task_type": kind, "title": title,
                "due_date": dt.date.today().isoformat(), "due_time": "",
                "category": "CLIENT_FACING", "priority": "MEDIUM",
                "progress": "NOT_STARTED", "description": "", "contact": "",
            },
        )
        assert Task.objects.get(title=title).task_type == kind


def test_the_generated_kinds_are_not_on_offer(client, account, people):
    """A hand-made renewal would not replace the generated one - it would sit
    beside it, because the generator identifies its own by an idempotency key."""
    login(client, people["csm"])
    form = client.get(reverse("tasks:create", args=[account.pk])).context["form"]

    offered = [value for value, _ in form.fields["task_type"].choices]
    assert offered == ["MANUAL_NEXT_ACTION", "MEETING", "CALL"]
    assert "AUTO_RENEWAL" not in offered
    assert "AUTO_HEALTH_ALERT" not in offered


def test_a_generated_task_cannot_be_retyped(client, account, people):
    """Its auto_key is built from the kind, so changing it would orphan the task
    from the generator that owns it - and the next run would raise a second one
    beside it."""
    from csm.core.enums import TaskType
    from csm.tasks.models import Task

    task = Task.objects.create(
        account=account, task_type=TaskType.AUTO_RENEWAL, title="Renewal in 90 days",
        due_date=dt.date.today(), auto_key="renewal:1:2026-12-31:90",
    )
    login(client, people["csm"])

    form = client.get(reverse("tasks:edit", args=[task.pk])).context["form"]
    assert "task_type" not in form.fields

    client.post(
        reverse("tasks:edit", args=[task.pk]),
        {
            "task_type": "CALL", "title": "Renewal in 90 days",
            "due_date": task.due_date.isoformat(), "due_time": "",
            "category": task.category, "priority": "MEDIUM",
            "progress": "NOT_STARTED", "description": "", "contact": "",
        },
    )
    task.refresh_from_db()
    assert task.task_type == TaskType.AUTO_RENEWAL


def test_a_manual_task_can_be_retyped(client, account, people):
    """A next action that turned out to need a call is a call."""
    from csm.core.enums import TaskType
    from csm.tasks.models import Task

    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Chase them",
        due_date=dt.date.today(), created_by=people["csm"],
    )
    login(client, people["csm"])
    client.post(
        reverse("tasks:edit", args=[task.pk]),
        {
            "task_type": "CALL", "title": "Chase them",
            "due_date": task.due_date.isoformat(), "due_time": "",
            "category": task.category, "priority": "MEDIUM",
            "progress": "IN_PROGRESS", "description": "", "contact": "",
        },
    )
    task.refresh_from_db()
    assert task.task_type == TaskType.CALL


def test_a_task_carries_a_priority_of_its_own(client, account, people):
    """Separate from the due date: when a thing is due and how much it matters
    are different questions, and a list sorted by date answers only one."""
    from csm.tasks.models import Task

    login(client, people["csm"])
    client.post(
        reverse("tasks:create", args=[account.pk]),
        {
            "title": "Chase the renewal", "due_date": dt.date.today().isoformat(),
            "due_time": "", "category": "CLIENT_FACING", "priority": "HIGH", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION",
            "description": "", "contact": "",
        },
    )

    assert Task.objects.get(title="Chase the renewal").priority == "HIGH"


def test_a_task_nobody_prioritised_is_medium(client, account, people):
    """Not Low: a default of Low would make "priority" mean "somebody
    remembered to set it"."""
    from csm.core.enums import TaskType
    from csm.tasks.models import Task

    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION,
        title="Unprioritised", due_date=dt.date.today(), created_by=people["csm"],
    )
    assert task.priority == "MEDIUM"


def test_a_health_alert_is_raised_high(account, people, django_capture_on_commit_callbacks):
    """It is the one task that means stop what you are doing - the only one
    still notified the moment it is raised."""
    from csm.core.enums import TaskType
    from csm.health import services as health_services
    from csm.tasks.models import Task
    from tests.factories import CsatFactory, TicketFactory

    account.scoring_baseline_date = dt.date.today() - dt.timedelta(days=365)
    account.save(update_fields=["scoring_baseline_date"])
    CsatFactory(account=account, score=1, recorded_on=dt.date.today())
    TicketFactory(account=account, ticket_kind="BUG")
    TicketFactory(account=account, ticket_kind="BUG")

    with django_capture_on_commit_callbacks(execute=True):
        health_services.recalculate(account, as_of=dt.date.today())

    alert = Task.objects.filter(task_type=TaskType.AUTO_HEALTH_ALERT).first()
    assert alert is not None
    assert alert.priority == "HIGH"


def test_both_columns_appear_on_the_task_list(client, account, people):
    from csm.core.enums import TaskType
    from csm.tasks.models import Task, TaskAssignment

    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Listed",
        due_date=dt.date.today() + dt.timedelta(days=1), priority="HIGH",
        created_by=people["csm"],
    )
    TaskAssignment.objects.create(task=task, user=people["csm"])
    login(client, people["csm"])

    body = client.get(reverse("tasks:list")).content.decode()
    head = body.split("<thead>")[1].split("</thead>")[0]

    assert "Priority" in head
    assert "Status" in head
    assert "text-bg-danger\">High" in body.replace("\n", "")


def test_both_badges_appear_on_my_day(client, account, people):
    from csm.core.enums import TaskType
    from csm.tasks.models import Task, TaskAssignment

    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="On my day",
        due_date=dt.date.today() + dt.timedelta(days=1), priority="HIGH",
        created_by=people["csm"],
    )
    TaskAssignment.objects.create(task=task, user=people["csm"])
    login(client, people["csm"])

    body = client.get(reverse("dashboard:index")).content.decode()
    assert "On my day" in body
    assert "High" in body
    assert "Not started" in body


def test_status_says_what_somebody_set_not_what_the_clock_says(client, account, people):
    """Overdue is a fact about the clock, not a state anybody chose.

    It belongs beside the due date - where it is already shown in red - rather
    than overwriting "Waiting for input" with something the reader can see from
    the date they are looking at.
    """
    from csm.core.enums import TaskProgress, TaskType
    from csm.tasks.models import Task, TaskAssignment

    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Late one",
        due_date=dt.date.today() - dt.timedelta(days=1),
        progress=TaskProgress.WAITING_INPUT, created_by=people["csm"],
    )
    TaskAssignment.objects.create(task=task, user=people["csm"])
    login(client, people["csm"])

    body = client.get(reverse("tasks:list"), {"state": "overdue"}).content.decode()
    row = body.split("Late one")[1].split("</tr>")[0]

    assert "Waiting for input" in row, "the status is still what was set"
    assert "text-danger" in row, "and the due date still says it is late"


def test_marking_a_task_done_sets_its_status_to_completed(client, account, people):
    """The two move together. A task that is done but still reads "In progress"
    is the state that makes people stop trusting the column."""
    from csm.core.enums import TaskProgress, TaskStatus, TaskType
    from csm.tasks.models import Task

    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Finish me",
        due_date=dt.date.today(), progress=TaskProgress.IN_PROGRESS,
        created_by=people["csm"],
    )
    login(client, people["csm"])
    client.post(reverse("tasks:complete", args=[task.pk]))

    task.refresh_from_db()
    assert task.status == TaskStatus.DONE
    assert task.progress == TaskProgress.COMPLETED


def test_completed_can_be_chosen_when_something_already_happened(
    client, account, people, django_capture_on_commit_callbacks
):
    """The one-step path that replaced the separate "log an activity" form.

    Choosing it closes the task through the service rather than labelling the
    column, so the log entry gets written and the score moves - a task that read
    Completed without any of that would be a lie about the account.
    """
    from csm.activities.models import Activity
    from csm.core.enums import TaskStatus
    from csm.tasks.models import Task

    login(client, people["csm"])
    form = client.get(reverse("tasks:create", args=[account.pk])).context["form"]
    assert [v for v, _ in form.fields["progress"].choices] == [
        "NOT_STARTED", "IN_PROGRESS", "WAITING_INPUT", "COMPLETED",
    ]

    # The log entry is written on the event bus, which delivers on commit - so
    # a test transaction has to be told to run the callbacks.
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("tasks:create", args=[account.pk]),
            {
                "task_type": "CALL", "title": "Rang them just now",
                "due_date": dt.date.today().isoformat(), "due_time": "",
                "category": "CLIENT_FACING", "priority": "MEDIUM",
                "progress": "COMPLETED", "description": "", "contact": "",
            },
        )

    task = Task.objects.get(title="Rang them just now")
    assert task.status == TaskStatus.DONE
    assert task.progress == "COMPLETED"
    assert Activity.objects.filter(task=task, activity_type="CALL").exists()


def test_a_task_starts_not_started(client, account, people):
    from csm.core.enums import TaskType
    from csm.tasks.models import Task

    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Fresh",
        due_date=dt.date.today(), created_by=people["csm"],
    )
    assert task.progress == "NOT_STARTED"


def test_editing_a_task_keeps_its_priority_unless_changed(client, account, people):
    from csm.core.enums import TaskType
    from csm.tasks.models import Task

    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Edit me",
        due_date=dt.date.today(), priority="HIGH", created_by=people["csm"],
    )
    login(client, people["csm"])

    form = client.get(reverse("tasks:edit", args=[task.pk])).context["form"]
    assert form.initial["priority"] == "HIGH"

    client.post(
        reverse("tasks:edit", args=[task.pk]),
        {
            "title": "Edited", "due_date": task.due_date.isoformat(), "due_time": "",
            "category": task.category, "priority": "LOW", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION", "description": "",
            "contact": "",
        },
    )
    task.refresh_from_db()
    assert task.title == "Edited"
    assert task.priority == "LOW"


# ---------------------------------------------------------------------------
# Due times, and the task scope default
# ---------------------------------------------------------------------------


def test_a_task_can_carry_a_time(client, account, people):
    from csm.tasks.models import Task

    login(client, people["csm"])
    client.post(
        reverse("tasks:create", args=[account.pk]),
        {
            "title": "Call at half past two", "due_date": dt.date.today().isoformat(),
            "due_time": "14:30", "category": "CLIENT_FACING", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION", "description": "",
            "contact": "",
        },
    )

    task = Task.objects.get(title="Call at half past two")
    assert task.due_time == dt.time(14, 30)
    assert "at 14:30" in task.due_display


@pytest.mark.parametrize("url_name", ["per_account", "anywhere"])
def test_the_time_field_sits_directly_under_the_date(client, account, people, url_name):
    """Every create form puts Time immediately below Date.

    The standalone form reorders its fields to put the account first, and
    order_fields() sends anything left out of that list to the bottom - so
    omitting due_time did not leave it where it was declared, it moved the time
    input below the description. Asserting on the rendered positions rather than
    on form.fields is deliberate: field order is only interesting because of
    where it puts the input on the page.
    """
    login(client, people["csm"])
    url = (
        reverse("tasks:create", args=[account.pk])
        if url_name == "per_account"
        else reverse("tasks:create_anywhere")
    )

    html = client.get(url).content.decode()
    date_at = html.index('id="id_due_date"')
    time_at = html.index('id="id_due_time"')
    category_at = html.index('id="id_category"')

    assert date_at < time_at < category_at


def test_a_task_without_a_time_is_due_any_time_that_day(client, account, people):
    from csm.tasks.models import Task

    login(client, people["csm"])
    client.post(
        reverse("tasks:create", args=[account.pk]),
        {
            "title": "Some day", "due_date": dt.date.today().isoformat(),
            "due_time": "", "category": "INTERNAL", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION", "description": "", "contact": "",
        },
    )

    task = Task.objects.get(title="Some day")
    assert task.due_time is None
    assert "at" not in task.due_display
    assert task.is_overdue(dt.date.today(), dt.time(23, 59)) is False


def test_a_timed_task_is_overdue_once_the_time_passes(account, people):
    from csm.core.enums import TaskType
    from csm.tasks.models import Task

    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Nine o'clock",
        due_date=dt.date.today(), due_time=dt.time(9, 0), created_by=people["csm"],
    )

    assert task.is_overdue(dt.date.today(), dt.time(8, 59)) is False
    assert task.is_overdue(dt.date.today(), dt.time(9, 1)) is True


def test_a_completed_task_is_never_overdue(account, people):
    from csm.core.enums import TaskStatus, TaskType
    from csm.tasks.models import Task

    task = Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Done",
        due_date=dt.date.today() - dt.timedelta(days=5), status=TaskStatus.DONE,
        completed_at=dt.datetime.now(dt.UTC), created_by=people["csm"],
    )
    assert task.is_overdue(dt.date.today()) is False


def test_the_task_list_defaults_to_everything_you_can_see(client, account, people):
    """A task created on somebody else's account routes to their CSM, so a
    default of "mine" made it look as though the task had vanished."""
    from csm.core.enums import TaskType
    from csm.tasks.models import Task

    Task.objects.create(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION,
        title="Routed elsewhere", due_date=dt.date.today(), created_by=people["senior"],
    )

    login(client, people["senior"])
    response = client.get(reverse("tasks:list"))

    assert response.context["scope"] == "all"
    assert "Routed elsewhere" in response.content.decode()


def test_mine_still_narrows_to_your_own_assignments(client, account, people):
    from csm.core.enums import TaskType
    from csm.tasks.services import create_task

    create_task(
        account=account, task_type=TaskType.MANUAL_NEXT_ACTION, title="Casey's own",
        due_date=dt.date.today(), created_by=people["csm"],
    )

    login(client, people["senior"])
    body = client.get(reverse("tasks:list"), {"scope": "mine"}).content.decode()
    assert "Casey's own" not in body

    login(client, people["csm"])
    body = client.get(reverse("tasks:list"), {"scope": "mine"}).content.decode()
    assert "Casey&#x27;s own" in body or "Casey's own" in body


# ---------------------------------------------------------------------------
# The dashboard health card
# ---------------------------------------------------------------------------


def test_the_health_counts_link_to_a_filtered_account_list(client, people):
    """A number you have to go and look up somewhere else is not much use."""
    from csm.core.enums import HealthStatus

    AccountFactory(client_code="H1", csm=people["csm"],
                   effective_health_status=HealthStatus.HEALTHY)
    AccountFactory(client_code="H2", csm=people["csm"],
                   effective_health_status=HealthStatus.HEALTHY)
    AccountFactory(client_code="R1", client_name="Risky Co", csm=people["csm"],
                   effective_health_status=HealthStatus.AT_RISK)

    login(client, people["csm"])
    body = client.get(reverse("dashboard:index")).content.decode()

    for status in ("HEALTHY", "WATCH", "AT_RISK", "NOT_APPLICABLE"):
        assert f"{reverse('accounts:list')}?status={status}" in body

    # And the link actually narrows the list it points at.
    filtered = client.get(reverse("accounts:list"), {"status": "AT_RISK"})
    codes = {a.client_code for a in filtered.context["object_list"]}
    assert codes == {"R1"}


def test_health_and_the_at_risk_list_share_one_card(client, people):
    from csm.core.enums import HealthStatus

    AccountFactory(client_code="R2", client_name="Risky Two", csm=people["csm"],
                   effective_health_status=HealthStatus.AT_RISK)
    login(client, people["csm"])
    body = client.get(reverse("dashboard:index")).content.decode()

    assert body.count("Portfolio health") == 1
    assert "Needs attention" in body
    assert "Risky Two" in body
    # One card, so the old separate header is gone.
    assert "Health score" not in body


def test_an_acm_sees_no_health_card_at_all(client, people):
    from csm.core.enums import HealthStatus

    AccountFactory(client_code="R3", csm=people["csm"], acm=people["acm"],
                   effective_health_status=HealthStatus.AT_RISK)
    login(client, people["acm"])
    body = client.get(reverse("dashboard:index")).content.decode()

    assert "Portfolio health" not in body
    assert "Needs attention" not in body
    assert "status=AT_RISK" not in body
