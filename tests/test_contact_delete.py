"""Deleting a contact, and the three-dot row menu that offers it.

A contact has no soft-delete columns, unlike an account - every other FK onto
one (Activity, Task, CsatResponse) is SET_NULL, so nothing destructive rides
along with it.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from csm.activities.models import Activity
from csm.contacts.models import Contact
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login


@pytest.fixture
def staff(db):
    users = {
        "csm": UserFactory(csm=True, email="cd-csm@dphish.com"),
        "senior": UserFactory(senior_csm=True, email="cd-senior@dphish.com"),
        "acm": UserFactory(acm=True, email="cd-acm@dphish.com"),
        "manager": UserFactory(manager=True, email="cd-manager@dphish.com"),
    }
    for user in users.values():
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    return users


@pytest.fixture
def account(staff):
    return AccountFactory(client_code="CD01", client_name="Delete Co", csm=staff["csm"])


@pytest.fixture
def contact(account):
    return Contact.objects.create(
        account=account, full_name="Dana Farouk", email="dana@acme.example"
    )


def test_a_manager_may_not_delete_a_contact(client, staff, contact):
    """A manager holds no ``contact.edit`` at all, so this is a flat 403
    regardless of which account the contact belongs to."""
    login(client, staff["manager"])
    assert client.get(reverse("contacts:delete", args=[contact.pk])).status_code == 403
    assert client.post(reverse("contacts:delete", args=[contact.pk])).status_code == 403


def test_an_acm_may_delete_a_contact_on_their_own_account(client, staff, contact, account):
    """An ACM holds ``contact.edit`` - stakeholder contacts are commercial,
    not a health signal - so row scope, not the capability, is what limits
    them to accounts they are actually assigned to."""
    account.acm = staff["acm"]
    account.save(update_fields=["acm"])

    login(client, staff["acm"])
    assert client.post(reverse("contacts:delete", args=[contact.pk])).status_code == 302
    assert not Contact.objects.filter(pk=contact.pk).exists()


def test_a_csm_may_delete_a_contact_on_their_own_account(client, staff, contact):
    login(client, staff["csm"])
    response = client.post(reverse("contacts:delete", args=[contact.pk]))
    assert response.status_code == 302
    assert not Contact.objects.filter(pk=contact.pk).exists()


def test_a_colleagues_contact_is_a_404_not_a_403_to_delete(client, staff, account):
    other_account = AccountFactory(client_code="CD02", client_name="Other Co", csm=staff["senior"])
    other_contact = Contact.objects.create(account=other_account, full_name="Not Yours")

    login(client, staff["csm"])
    assert client.get(reverse("contacts:delete", args=[other_contact.pk])).status_code == 404


def test_deleting_a_contact_keeps_activities_but_drops_the_reference(client, staff, contact, account):
    activity = Activity.objects.create(
        account=account, contact=contact, activity_type="CALL",
        occurred_on="2026-01-01", created_by=staff["csm"],
    )
    login(client, staff["csm"])
    client.post(reverse("contacts:delete", args=[contact.pk]))

    activity.refresh_from_db()
    assert activity.contact_id is None


def test_the_confirmation_page_names_the_contact(client, staff, contact):
    login(client, staff["csm"])
    body = client.get(reverse("contacts:delete", args=[contact.pk])).content.decode()
    assert "Dana Farouk" in body
    assert "cannot be undone" in body


def test_the_attention_list_row_offers_edit_and_delete(client, staff, account):
    contact = Contact.objects.create(
        account=account, full_name="Dana Farouk", influence="HIGH", interest="HIGH",
    )
    login(client, staff["csm"])
    body = client.get(reverse("contacts:attention")).content.decode()

    assert reverse("contacts:edit", args=[contact.pk]) in body
    assert reverse("contacts:delete", args=[contact.pk]) in body
    assert f'data-href="{reverse("contacts:detail", args=[contact.pk])}"' in body


def test_an_acm_gets_edit_and_delete_but_no_csat_on_the_row_menu(client, staff, account):
    """An ACM holds contact.edit but not csat.record: the menu offers the
    first two and leaves the third out, the same split accounts.edit uses."""
    account.acm = staff["acm"]
    account.save(update_fields=["acm"])
    contact = Contact.objects.create(
        account=account, full_name="Dana Farouk", influence="HIGH", interest="HIGH",
    )
    login(client, staff["acm"])
    body = client.get(reverse("contacts:attention")).content.decode()

    assert reverse("contacts:edit", args=[contact.pk]) in body
    assert reverse("contacts:delete", args=[contact.pk]) in body
    assert reverse("engagement:csat_create", args=[account.pk]) not in body


def test_the_row_menu_reads_task_call_meeting_csat_edit_delete(client, staff, account):
    """In that order, as bare nouns: the menu is the verb, so "Add" is noise."""
    contact = Contact.objects.create(
        account=account, full_name="Dana Farouk", influence="HIGH", interest="HIGH",
    )
    login(client, staff["csm"])
    body = client.get(reverse("dashboard:index")).content.decode()
    menu = body[body.index(f'aria-label="Actions for {contact.full_name}"'):]
    menu = " ".join(menu[: menu.index("</ul>")].split())

    labels = [f"</i>{word} </a>" for word in ("Task", "Call", "Meeting", "CSAT", "Edit", "Delete")]
    positions = [menu.index(label) for label in labels]
    assert positions == sorted(positions)
    assert "Add " not in menu
    for kind in ("MANUAL_NEXT_ACTION", "CALL", "MEETING"):
        assert f"task_type={kind}&amp;contact={contact.pk}" in menu
    assert f"?contact={contact.pk}&amp;next=" in menu


def test_csat_opened_from_a_contact_preselects_them(client, staff, account, contact):
    login(client, staff["csm"])
    url = reverse("engagement:csat_create", args=[account.pk])
    form = client.get(f"{url}?contact={contact.pk}").context["form"]

    assert str(form.initial["contact"]) == str(contact.pk)
