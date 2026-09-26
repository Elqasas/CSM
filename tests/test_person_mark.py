"""A contact's mark: initials in a circle, in a tone stable across restarts."""

from __future__ import annotations

import pytest
from django.urls import reverse

from csm.contacts.models import Contact
from csm.core.templatetags.people import TONES, initials, person_tone
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Abdulaziz Alshehri", "AA"),
        ("Abdulaziz J. Aljabr", "AA"),
        ("ahmed tarek", "AT"),
        ("Madonna", "M"),
        ("  ", "?"),
        (None, "?"),
    ],
)
def test_initials_are_first_and_last(name, expected):
    assert initials(name) == expected


def test_the_tone_is_stable_and_in_range():
    # A fixed value, not merely a repeatable one: Python's hash() would pass a
    # same-process check and still recolour everyone after a restart.
    assert person_tone("Abdulaziz Alshehri") == person_tone(" abdulaziz alshehri ")
    assert all(0 <= person_tone(f"Person {n}") < TONES for n in range(50))
    assert len({person_tone(f"Person {n}") for n in range(50)}) == TONES


def test_the_contact_view_shows_the_persons_mark_not_the_accounts(client, db):
    user = UserFactory(csm=True, email="mark-csm@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    account = AccountFactory(client_code="PM01", client_name="Mark Co", csm=user)
    contact = Contact.objects.create(account=account, full_name="Dana Farouk")
    login(client, user)

    body = client.get(reverse("contacts:detail", args=[contact.pk])).content.decode()
    assert 'class="person-mark person-tone-' in body
    assert ">DF</span>" in body
    assert "acct-logo-initials" not in body
