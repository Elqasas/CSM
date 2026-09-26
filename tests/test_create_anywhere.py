"""Creating a contact or a task without starting from an account page.

Both already existed, reachable only from inside an account. Promoting the
account from a URL segment to a form field moves the row-scope question with it:
the picker must offer exactly the accounts ``Ability.accounts()`` returns, since
a dropdown listing a colleague's client leaks the same fact the account list
refuses to.

The task form also spans accounts in its contact list, which creates a pairing
nobody else has to worry about: a contact at one client attached to a task at
another is nonsense the database has no opinion about.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from csm.contacts.models import Contact
from csm.core.enums import CommunicationChannel, Level
from csm.tasks.models import Task
from tests.factories import AccountFactory, ContactFactory, UserFactory
from tests.test_views import PASSWORD, login


@pytest.fixture
def people(db):
    users = {
        "csm": UserFactory(csm=True, email="any-csm@dphish.com", full_name="Casey Morgan"),
        "other": UserFactory(csm=True, email="any-other@dphish.com"),
        "manager": UserFactory(manager=True, email="any-mgr@dphish.com"),
    }
    for user in users.values():
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    return users


@pytest.fixture
def mine(people):
    return AccountFactory(client_code="MINE", client_name="Acme Industrial", csm=people["csm"])


@pytest.fixture
def theirs(people):
    return AccountFactory(client_code="THEIRS", client_name="Zenith Holdings", csm=people["other"])


# ---------------------------------------------------------------------------
# The account picker is row scope, rendered as a dropdown
# ---------------------------------------------------------------------------


def test_the_contact_picker_offers_only_your_accounts(client, mine, theirs, people):
    login(client, people["csm"])
    response = client.get(reverse("contacts:create_anywhere"))

    choices = list(response.context["form"].fields["account"].queryset)
    assert mine in choices
    assert theirs not in choices


def test_the_task_picker_offers_only_your_accounts(client, mine, theirs, people):
    login(client, people["csm"])
    response = client.get(reverse("tasks:create_anywhere"))

    choices = list(response.context["form"].fields["account"].queryset)
    assert mine in choices
    assert theirs not in choices


def test_posting_a_colleagues_account_is_refused(client, mine, theirs, people):
    """The dropdown is the honest part; the queryset is the enforcing part."""
    login(client, people["csm"])
    response = client.post(
        reverse("contacts:create_anywhere"),
        {
            "account": theirs.pk, "full_name": "Sneaky Contact", "title": "",
            "email": "", "phone": "", "influence": "", "interest": "",
            "preferred_channel": "", "is_active": "on", "notes": "", "channels": [],
        },
    )

    assert response.status_code == 200
    assert not Contact.objects.filter(full_name="Sneaky Contact").exists()


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def test_adding_a_contact_from_the_contacts_page(client, mine, people):
    login(client, people["csm"])
    response = client.post(
        reverse("contacts:create_anywhere"),
        {
            "account": mine.pk, "full_name": "Dana Farouk", "title": "CISO",
            "email": "dana@acme.example", "phone": "", "influence": Level.HIGH,
            "interest": Level.HIGH, "preferred_channel": CommunicationChannel.EMAIL,
            "is_active": "on", "notes": "",
            "channels": [CommunicationChannel.EMAIL, CommunicationChannel.TEAMS],
        },
    )

    assert response.status_code == 302
    contact = Contact.objects.get(full_name="Dana Farouk")
    assert contact.account == mine
    assert contact.quadrant == "MANAGE_CLOSELY"
    # The channel fix applies on this path too, not only the per-account one.
    assert set(contact.channels.values_list("channel", flat=True)) == {
        CommunicationChannel.EMAIL, CommunicationChannel.TEAMS
    }


def test_a_blank_assessment_still_lands_in_the_triage_tray(client, mine, people):
    login(client, people["csm"])
    client.post(
        reverse("contacts:create_anywhere"),
        {
            "account": mine.pk, "full_name": "Unknown Person", "title": "",
            "email": "unknown@acme.example", "phone": "", "influence": "", "interest": "",
            "preferred_channel": "", "is_active": "on", "notes": "", "channels": [],
        },
    )

    contact = Contact.objects.get(full_name="Unknown Person")
    assert contact.quadrant is None
    assert contact in Contact.objects.needs_triage()


def test_the_button_is_offered_to_someone_who_may_add_contacts(client, mine, people):
    login(client, people["csm"])
    body = client.get(reverse("contacts:attention")).content.decode()
    assert reverse("contacts:create_anywhere") in body


def test_a_manager_is_not_offered_the_button_and_cannot_reach_it(client, mine, people):
    """A manager is view-only, which the page and the view both say."""
    login(client, people["manager"])

    body = client.get(reverse("contacts:attention")).content.decode()
    assert reverse("contacts:create_anywhere") not in body
    assert client.get(reverse("contacts:create_anywhere")).status_code == 403


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def test_creating_a_task_from_the_tasks_page(client, mine, people):
    login(client, people["csm"])
    response = client.post(
        reverse("tasks:create_anywhere"),
        {
            "account": mine.pk, "title": "Call the CFO",
            "due_date": dt.date.today().isoformat(), "category": "CLIENT_FACING", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION",
            "contact": "", "description": "About the renewal.",
        },
    )

    assert response.status_code == 302
    task = Task.objects.get(title="Call the CFO")
    assert task.account == mine
    assert task.created_by == people["csm"]
    # Routed, exactly as a task created from the account page would be.
    assert task.assignments.exists()


def test_a_contact_from_another_account_is_refused(client, mine, theirs, people):
    """A contact at one client on a task at another is nonsense the database
    has no opinion about, so the form has one."""
    mine_too = AccountFactory(client_code="MINE2", client_name="Beta Systems", csm=people["csm"])
    stranger = ContactFactory(account=mine_too, full_name="Wrong Client Person")

    login(client, people["csm"])
    response = client.post(
        reverse("tasks:create_anywhere"),
        {
            "account": mine.pk, "title": "Mismatched",
            "due_date": dt.date.today().isoformat(), "category": "INTERNAL", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION",
            "contact": stranger.pk, "description": "",
        },
    )

    assert response.status_code == 200
    assert "works for Beta Systems" in response.content.decode()
    assert not Task.objects.filter(title="Mismatched").exists()


def test_a_matching_contact_is_accepted(client, mine, people):
    contact = ContactFactory(account=mine, full_name="Right Person")
    login(client, people["csm"])

    client.post(
        reverse("tasks:create_anywhere"),
        {
            "account": mine.pk, "title": "Matched",
            "due_date": dt.date.today().isoformat(), "category": "INTERNAL", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION",
            "contact": contact.pk, "description": "",
        },
    )

    assert Task.objects.get(title="Matched").contact == contact


def test_the_contact_list_does_not_span_into_a_colleagues_accounts(
    client, mine, theirs, people
):
    ContactFactory(account=theirs, full_name="Their Contact")
    login(client, people["csm"])

    response = client.get(reverse("tasks:create_anywhere"))
    names = [c.full_name for c in response.context["form"].fields["contact"].queryset]
    assert "Their Contact" not in names


def test_a_new_task_still_cannot_start_overdue(client, mine, people):
    login(client, people["csm"])
    response = client.post(
        reverse("tasks:create_anywhere"),
        {
            "account": mine.pk, "title": "Late already",
            "due_date": (dt.date.today() - dt.timedelta(days=1)).isoformat(),
            "category": "INTERNAL", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION", "contact": "", "description": "",
        },
    )

    assert response.status_code == 200
    assert not Task.objects.filter(title="Late already").exists()


def test_the_button_is_on_the_tasks_page(client, mine, people):
    login(client, people["csm"])
    body = client.get(reverse("tasks:list")).content.decode()
    assert reverse("tasks:create_anywhere") in body


def test_a_manager_gets_no_new_task_button(client, mine, people):
    login(client, people["manager"])

    body = client.get(reverse("tasks:list")).content.decode()
    assert reverse("tasks:create_anywhere") not in body
    assert client.get(reverse("tasks:create_anywhere")).status_code == 403
