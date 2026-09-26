"""Forgot password: email a one-time code, check it, set a new password.

The properties that matter are the ones an attacker would probe: the page must
not reveal who has an account, a code must be guessable only five times, and a
code works once.
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest
import time_machine
from django.core import mail
from django.urls import reverse

from csm.users import password_reset
from csm.users.models import PasswordResetCode
from tests.factories import UserFactory

NEW_PASSWORD = "brand-new-Passw0rd!"


@pytest.fixture
def person(db):
    return UserFactory(csm=True, email="forgetful@dphish.com")


def _code_from_outbox() -> str:
    return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)


def _request_code(client, email="forgetful@dphish.com"):
    return client.post(reverse("users:password_forgot"), {"email": email})


def test_sign_in_offers_forgot_password(client, db):
    body = client.get(reverse("users:login")).content.decode()
    assert reverse("users:password_forgot") in body


def test_the_whole_flow_sets_the_new_password(client, person):
    response = _request_code(client, "Forgetful@dphish.com ")
    assert response.url == reverse("users:password_reset_code")
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["forgetful@dphish.com"]

    response = client.post(reverse("users:password_reset_code"), {"code": _code_from_outbox()})
    assert response.url == reverse("users:password_reset_new")

    response = client.post(
        reverse("users:password_reset_new"),
        {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
    )
    assert response.url == reverse("users:login")
    person.refresh_from_db()
    assert person.check_password(NEW_PASSWORD)


def test_an_unknown_address_looks_exactly_like_a_known_one(client, person):
    known = _request_code(client)
    client.session.flush()
    unknown = client.post(reverse("users:password_forgot"), {"email": "nobody@dphish.com"})

    assert known.url == unknown.url
    assert len(mail.outbox) == 1  # only the real one
    page = client.get(unknown.url).content.decode()
    assert "If an account exists" in page


def test_an_inactive_user_is_sent_nothing(client, person):
    person.is_active = False
    person.save(update_fields=["is_active"])
    _request_code(client)
    assert mail.outbox == []


def test_the_code_is_stored_only_as_a_hash(client, person):
    _request_code(client)
    row = PasswordResetCode.objects.get(user=person)
    assert _code_from_outbox() not in row.code_hash


def test_five_wrong_guesses_kill_the_code(client, person):
    _request_code(client)
    real = _code_from_outbox()
    wrong = "000000" if real != "000000" else "111111"
    for _ in range(password_reset.MAX_ATTEMPTS):
        response = client.post(reverse("users:password_reset_code"), {"code": wrong})
        assert response.status_code == 200
        assert "wrong or has expired" in response.content.decode()

    response = client.post(reverse("users:password_reset_code"), {"code": real})
    assert response.status_code == 200  # the right code, too late


def test_a_code_expires(client, person):
    with time_machine.travel("2026-09-26 09:00Z", tick=False):
        _request_code(client)
        code = _code_from_outbox()
    later = _dt.timedelta(minutes=password_reset.CODE_MINUTES, seconds=1)
    with time_machine.travel(_dt.datetime(2026, 9, 26, 9, tzinfo=_dt.UTC) + later, tick=False):
        response = client.post(reverse("users:password_reset_code"), {"code": code})
    assert response.status_code == 200


def test_a_new_code_retires_the_old_one(client, person):
    _request_code(client)
    first = _code_from_outbox()
    _request_code(client)
    second = _code_from_outbox()
    if first == second:
        pytest.skip("both draws were the same six digits")

    assert client.post(reverse("users:password_reset_code"), {"code": first}).status_code == 200
    assert client.post(
        reverse("users:password_reset_code"), {"code": second}
    ).url == reverse("users:password_reset_new")


def test_a_code_works_once(client, person):
    _request_code(client)
    code = _code_from_outbox()
    client.post(reverse("users:password_reset_code"), {"code": code})
    client.post(
        reverse("users:password_reset_new"),
        {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
    )
    session = client.session
    session["password_reset_email"] = person.email
    session.save()
    assert client.post(reverse("users:password_reset_code"), {"code": code}).status_code == 200


def test_requests_are_throttled_per_hour(client, person):
    for _ in range(password_reset.MAX_CODES_PER_HOUR + 2):
        _request_code(client)
    assert len(mail.outbox) == password_reset.MAX_CODES_PER_HOUR


def test_the_last_step_needs_a_verified_code(client, person):
    _request_code(client)
    response = client.get(reverse("users:password_reset_new"))
    assert response.url == reverse("users:password_forgot")


def test_the_code_step_needs_an_email_first(client, db):
    response = client.get(reverse("users:password_reset_code"))
    assert response.url == reverse("users:password_forgot")


def test_the_new_password_obeys_the_validators(client, person):
    _request_code(client)
    client.post(reverse("users:password_reset_code"), {"code": _code_from_outbox()})
    response = client.post(
        reverse("users:password_reset_new"), {"new_password1": "123", "new_password2": "123"}
    )
    assert response.status_code == 200
    person.refresh_from_db()
    assert not person.check_password("123")
