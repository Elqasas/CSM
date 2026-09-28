"""Free-text notes on the account page.

What matters: who can read and write them (an ACM neither - notes are the CSM's
free text about the client and can carry exactly the health signal an ACM is
kept clear of), that a note on another CSM's account is a 404, and that add,
edit and delete each do what they say.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from csm.accounts.models import AccountNote
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login


@pytest.fixture
def people(db):
    users = {
        "csm": UserFactory(csm=True, email="note-csm@dphish.com", full_name="Casey Morgan"),
        "other_csm": UserFactory(csm=True, email="note-other@dphish.com"),
        "senior": UserFactory(senior_csm=True, email="note-senior@dphish.com"),
        "acm": UserFactory(acm=True, email="note-acm@dphish.com"),
        "manager": UserFactory(manager=True, email="note-manager@dphish.com"),
        "admin": UserFactory(admin=True, email="note-admin@dphish.com"),
    }
    for user in users.values():
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    return users


@pytest.fixture
def account(people):
    return AccountFactory(client_name="Notes Co", csm=people["csm"], acm=people["acm"])


@pytest.fixture
def note(account, people):
    return AccountNote.objects.create(
        account=account, body="Prefers calls on Sunday", created_by=people["csm"]
    )


@pytest.mark.parametrize(
    "who, sees_card, sees_buttons",
    [
        ("csm", True, True),
        ("senior", True, True),
        ("admin", True, True),
        ("manager", True, False),
        ("acm", False, False),
    ],
)
def test_the_card_follows_the_capabilities(client, people, account, note, who, sees_card, sees_buttons):
    login(client, people[who])
    page = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()
    assert ("Prefers calls on Sunday" in page) is sees_card
    assert (reverse("accounts:note_create", args=[account.pk]) in page) is sees_buttons
    assert (reverse("accounts:note_delete", args=[account.pk, note.pk]) in page) is sees_buttons


def test_add_edit_and_delete(client, people, account):
    login(client, people["csm"])
    detail = reverse("accounts:detail", args=[account.pk])

    response = client.post(
        reverse("accounts:note_create", args=[account.pk]),
        {"body": "Renewal champion is the CISO", "next": detail},
    )
    assert response.status_code == 302 and response.url == detail
    note = AccountNote.objects.get(account=account)
    assert note.created_by == people["csm"]

    login(client, people["senior"])
    client.post(
        reverse("accounts:note_edit", args=[account.pk, note.pk]),
        {"body": "Renewal champion is the CIO"},
    )
    note.refresh_from_db()
    assert note.body == "Renewal champion is the CIO"
    assert note.created_by == people["csm"]
    assert note.updated_by == people["senior"]

    client.post(reverse("accounts:note_delete", args=[account.pk, note.pk]))
    assert not AccountNote.objects.exists()


def test_an_empty_note_is_refused(client, people, account):
    login(client, people["csm"])
    response = client.post(reverse("accounts:note_create", args=[account.pk]), {"body": ""})
    assert response.status_code == 200
    assert not AccountNote.objects.exists()


def test_the_edit_form_opens_with_the_text(client, people, account, note):
    login(client, people["csm"])
    page = client.get(reverse("accounts:note_edit", args=[account.pk, note.pk])).content.decode()
    assert "Prefers calls on Sunday" in page


@pytest.mark.parametrize("who, status", [("acm", 403), ("manager", 403), ("other_csm", 404)])
def test_nobody_else_can_write(client, people, account, note, who, status):
    login(client, people[who])
    urls = [
        reverse("accounts:note_create", args=[account.pk]),
        reverse("accounts:note_edit", args=[account.pk, note.pk]),
        reverse("accounts:note_delete", args=[account.pk, note.pk]),
    ]
    for url in urls:
        assert client.post(url, {"body": "overwritten"}).status_code == status
    note.refresh_from_db()
    assert note.body == "Prefers calls on Sunday"


def test_a_note_is_reached_only_through_its_own_account(client, people, account, note):
    """The note pk alone is not enough: under another account it is a 404."""
    elsewhere = AccountFactory(csm=people["csm"])
    login(client, people["csm"])
    response = client.post(reverse("accounts:note_delete", args=[elsewhere.pk, note.pk]))
    assert response.status_code == 404
    assert AccountNote.objects.filter(pk=note.pk).exists()
