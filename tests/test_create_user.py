"""Creating real staff.

``createsuperuser`` makes a database administrator, which is not the same thing
as a platform user: the four access levels come from ``role`` and ``is_senior``,
and a Django superuser bypasses every one of them. These tests pin that
distinction, and that the command refuses the combinations the database would
refuse anyway - with an explanation rather than an IntegrityError.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from csm.core.access.abilities import Ability
from csm.core.enums import Role
from csm.users.models import User


def run(*args, **kwargs) -> str:
    out = StringIO()
    call_command("create_user", *args, stdout=out, **kwargs)
    return out.getvalue()


@pytest.mark.django_db
def test_creating_a_senior_csm():
    output = run("Ahmed.AlQasas@dphish.com", "Ahmed AlQasas", "--senior")

    user = User.objects.get(email="ahmed.alqasas@dphish.com")
    assert user.role == Role.CSM
    assert user.is_senior is True
    assert user.is_active is True
    assert "CSM Senior" in output


@pytest.mark.django_db
def test_the_email_is_stored_folded():
    """It is the login field, so case must not create a second account."""
    run("Mixed.Case@dphish.com", "Mixed Case")
    assert User.objects.filter(email="mixed.case@dphish.com").exists()


@pytest.mark.django_db
def test_a_generated_password_is_printed_once_and_works():
    output = run("pw@dphish.com", "Password Person")
    password = next(
        line.split("Password:")[1].strip()
        for line in output.splitlines()
        if "Password:" in line
    )

    user = User.objects.get(email="pw@dphish.com")
    assert user.check_password(password)
    assert len(password) >= 20


@pytest.mark.django_db
def test_an_explicit_password_is_not_echoed():
    output = run("quiet@dphish.com", "Quiet Person", "--password", "a-chosen-password-9876")

    assert "a-chosen-password-9876" not in output
    assert User.objects.get(email="quiet@dphish.com").check_password(
        "a-chosen-password-9876"
    )


@pytest.mark.django_db
def test_seniority_is_refused_on_anything_but_a_csm():
    """Seniority is not a thing an ACM or a Manager has.

    A database constraint says so; the command says so first, in words.
    """
    with pytest.raises(CommandError, match="only applies to a CSM"):
        run("acm@dphish.com", "An ACM", "--role", "ACM", "--senior")

    assert not User.objects.filter(email="acm@dphish.com").exists()


@pytest.mark.django_db
def test_a_duplicate_email_is_refused_with_a_readable_message():
    run("dup@dphish.com", "First")

    with pytest.raises(CommandError, match="already exists"):
        run("DUP@dphish.com", "Second")

    assert User.objects.filter(email="dup@dphish.com").count() == 1


@pytest.mark.django_db
def test_admin_is_off_unless_asked_for():
    """Django admin bypasses the whole access layer, so it is never a default."""
    run("plain@dphish.com", "Plain Person", "--senior")
    user = User.objects.get(email="plain@dphish.com")

    assert user.is_staff is False
    assert user.is_superuser is False


@pytest.mark.django_db
def test_admin_is_granted_and_warned_about_when_asked_for():
    """--admin is shorthand for the Admin role now that it is one."""
    output = run("boss@dphish.com", "The Admin", "--admin")
    user = User.objects.get(email="boss@dphish.com")

    assert user.role == "ADMIN"
    assert user.is_staff and user.is_superuser
    assert "bypasses" in output


@pytest.mark.django_db
def test_admin_and_another_role_together_are_refused():
    """Two ways of saying the same thing, disagreeing.

    One of them has to lose, and the operator cannot see which - so neither
    does, and the command says so.
    """
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="not both"):
        run("clash@dphish.com", "Clash", "--role", "MANAGER", "--admin")
    assert not User.objects.filter(email="clash@dphish.com").exists()


@pytest.mark.django_db
def test_an_inactive_user_can_see_nothing():
    run("later@dphish.com", "Starts Monday", "--inactive")
    user = User.objects.get(email="later@dphish.com")

    assert user.is_active is False
    assert Ability(user).accounts().count() == 0
    assert Ability(user).capabilities == frozenset()


@pytest.mark.django_db
def test_the_new_senior_csm_gets_the_access_the_level_implies():
    """The point of the command: a level, not a database account."""
    from tests.factories import AccountFactory, UserFactory

    AccountFactory(client_code="SOMEONE_ELSES", csm=UserFactory(csm=True))
    run("senior@dphish.com", "Senior Person", "--senior")

    ability = Ability(User.objects.get(email="senior@dphish.com"))
    assert ability.level_label == "CSM Senior"
    # A senior CSM sees every account, including one they do not own.
    assert ability.accounts().filter(client_code="SOMEONE_ELSES").exists()
    assert "health.view" in ability.capabilities
