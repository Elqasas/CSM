"""Administering staff, and changing your own password.

Admin is a role, and the only one that may create a user. These tests pin both
halves of that: an Admin holds every capability including ``user.manage``, and
no other level holds ``user.manage`` at all - not a senior CSM, who sees every
account, and not a manager, who reads every account.

The rest is lockout. Every way the last administrator could remove their own
ability to administer is refused, because the recovery is a shell on the server.
"""

from __future__ import annotations

import html

import pytest
from django.urls import reverse

from csm.core.access.abilities import Ability
from csm.core.enums import Role
from csm.users.models import User
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login


def make(email, **kwargs):
    user = UserFactory(email=email, **kwargs)
    user.set_password(PASSWORD)
    user.save()
    return user


@pytest.fixture
def admin(db):
    return make("admin@dphish.com", admin=True, full_name="The Admin")


@pytest.fixture
def plain(db):
    return make("plain@dphish.com", csm=True, full_name="Plain Person")


# ---------------------------------------------------------------------------
# Administration is not an access level
# ---------------------------------------------------------------------------


def test_an_administrator_holds_every_capability(admin):
    """Written as the whole vocabulary rather than a list, so a capability
    added tomorrow is the administrator's by default."""
    from csm.core.access import capabilities as cap

    ability = Ability(admin)
    assert ability.is_admin is True
    assert ability.capabilities == cap.ALL
    assert ability.level_label == "Admin"


def test_only_an_administrator_may_manage_users(db):
    """The point of making it a role: one level answers "who may add staff".

    A senior CSM sees every account in the company and still cannot create a
    login; a manager reads everything and cannot either.
    """
    for trait in ("csm", "senior_csm", "acm", "manager"):
        user = make(f"{trait}-nm@dphish.com", **{trait: True})
        assert "user.manage" not in Ability(user).capabilities, trait
        assert Ability(user).is_admin is False, trait


def test_an_administrator_sees_every_account_and_owns_none(admin, db):
    """Running the platform is the job, not carrying a portfolio."""
    mine = AccountFactory(client_code="OWNED", csm=make("owner@dphish.com", csm=True))

    assert admin.pk not in {mine.csm_id, mine.acm_id}
    assert Ability(admin).accounts().filter(pk=mine.pk).exists(), "sees it"
    assert admin.csm_accounts.count() == 0, "owns none"
    assert admin.acm_accounts.count() == 0


def test_seniority_alone_grants_nothing_administrative(db):
    senior = make("senior@dphish.com", senior_csm=True)
    ability = Ability(senior)

    assert ability.is_admin is False
    assert "user.manage" not in ability.capabilities
    assert "import.run" in ability.capabilities, "still a senior in other respects"


def test_an_inactive_administrator_administers_nothing(admin):
    admin.is_active = False
    admin.save(update_fields=["is_active"])

    ability = Ability(admin)
    assert ability.is_admin is False
    assert ability.capabilities == frozenset()


def test_the_staff_pages_are_refused_without_the_capability(client, plain):
    login(client, plain)

    assert client.get(reverse("users:staff_list")).status_code == 403
    assert client.get(reverse("users:staff_create")).status_code == 403


# ---------------------------------------------------------------------------
# Creating and editing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email, role, senior, label",
    [
        ("csm@dphish.com", Role.CSM, False, "CSM"),
        ("sen@dphish.com", Role.CSM, True, "CSM Senior"),
        ("acm@dphish.com", Role.ACM, False, "ACM"),
        ("man@dphish.com", Role.MANAGER, False, "Manager"),
        ("adm@dphish.com", Role.ADMIN, False, "Admin"),
    ],
)
def test_creating_each_of_the_levels(client, admin, email, role, senior, label):
    login(client, admin)
    data = {
        "email": email,
        "full_name": email.split("@")[0],
        "phone": "",
        "role": role,
        "is_active": "on",
    }
    if senior:
        data["is_senior"] = "on"
    client.post(reverse("users:staff_create"), data)

    user = User.objects.get(email=email)
    assert user.effective_level_label == label
    # Django's own admin site follows the role rather than being a separate
    # switch: an administrator who reaches one surface and not the other is a
    # puzzle nobody enjoys.
    assert user.is_superuser is (role == Role.ADMIN)


def test_the_one_time_password_is_shown_once_and_works(client, admin):
    login(client, admin)
    response = client.post(
        reverse("users:staff_create"),
        {
            "email": "new@dphish.com", "full_name": "New Person", "phone": "",
            "role": Role.CSM, "is_active": "on",
        },
        follow=True,
    )

    # Unescaped, because the generated alphabet includes & and # and the page
    # renders them as entities. What a person copies out of the browser is the
    # real password; what is in the markup is the escaped form.
    body = html.unescape(response.content.decode())
    assert "One-time password for new@dphish.com" in body
    password = body.split("One-time password for new@dphish.com: ")[1].split(" ")[0]
    assert User.objects.get(email="new@dphish.com").check_password(password)


def test_seniority_is_refused_on_anything_but_a_csm(client, admin):
    login(client, admin)
    response = client.post(
        reverse("users:staff_create"),
        {
            "email": "bad@dphish.com", "full_name": "Bad", "phone": "",
            "role": Role.ACM, "is_senior": "on", "is_active": "on",
        },
    )

    assert response.status_code == 200
    assert "applies to a CSM only" in response.content.decode()
    assert not User.objects.filter(email="bad@dphish.com").exists()


def test_promoting_someone_to_administrator(client, admin, plain):
    login(client, admin)
    client.post(
        reverse("users:staff_edit", args=[plain.pk]),
        {
            "email": plain.email, "full_name": plain.full_name, "phone": "",
            "role": Role.ADMIN, "is_active": "on",
        },
    )
    plain.refresh_from_db()

    assert plain.role == Role.ADMIN
    assert plain.is_superuser and plain.is_staff
    assert "user.manage" in Ability(plain).capabilities


def test_an_admin_is_not_offered_as_an_account_owner(admin, db):
    """An account whose "CSM" runs the platform is an account nobody works.

    Row scope already gives an Admin every account; being named on one would
    only make the table say something untrue about who is looking after it.
    """
    from csm.accounts.forms import AccountForm

    form = AccountForm(ability=Ability(admin))
    owners = set(form.fields["csm"].queryset.values_list("email", flat=True))
    managers = set(form.fields["acm"].queryset.values_list("email", flat=True))

    assert admin.email not in owners
    assert admin.email not in managers
    make("real-csm@dphish.com", csm=True)
    form = AccountForm(ability=Ability(admin))
    assert "real-csm@dphish.com" in set(
        form.fields["csm"].queryset.values_list("email", flat=True)
    )


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


def test_you_cannot_demote_yourself_out_of_admin(client, admin):
    login(client, admin)
    response = client.post(
        reverse("users:staff_edit", args=[admin.pk]),
        {
            "email": admin.email, "full_name": admin.full_name, "phone": "",
            "role": Role.CSM, "is_senior": "on", "is_active": "on",
        },
    )

    assert "cannot demote yourself out of Admin" in response.content.decode()
    admin.refresh_from_db()
    assert admin.role == Role.ADMIN


def test_you_cannot_deactivate_yourself(client, admin):
    login(client, admin)
    response = client.post(
        reverse("users:staff_edit", args=[admin.pk]),
        {
            "email": admin.email, "full_name": admin.full_name, "phone": "",
            "role": Role.ADMIN,
        },
    )

    assert "cannot deactivate your own account" in response.content.decode()
    admin.refresh_from_db()
    assert admin.is_active is True


def test_one_of_two_administrators_can_be_demoted(client, admin):
    second = make("second@dphish.com", admin=True)
    login(client, admin)

    client.post(
        reverse("users:staff_edit", args=[second.pk]),
        {
            "email": second.email, "full_name": second.full_name, "phone": "",
            "role": Role.CSM, "is_active": "on",
        },
    )
    second.refresh_from_db()
    assert second.role == Role.CSM
    assert second.is_superuser is False, "and loses the Django site with it"


def test_the_last_administrator_cannot_be_demoted(client, admin):
    """Recovery from this is a shell on the server, so it is refused."""
    second = make("second2@dphish.com", admin=True)
    login(client, second)

    # Demote the other one, leaving `second` as the only administrator.
    client.post(
        reverse("users:staff_edit", args=[admin.pk]),
        {
            "email": admin.email, "full_name": admin.full_name, "phone": "",
            "role": Role.CSM, "is_senior": "on", "is_active": "on",
        },
    )
    admin.refresh_from_db()
    assert admin.role == Role.CSM

    # Now `second` is the last one, and cannot be removed even by themselves.
    response = client.post(
        reverse("users:staff_edit", args=[second.pk]),
        {
            "email": second.email, "full_name": second.full_name, "phone": "",
            "role": Role.CSM, "is_active": "on",
        },
    )
    assert "cannot demote yourself out of Admin" in response.content.decode()
    second.refresh_from_db()
    assert second.role == Role.ADMIN


def test_you_cannot_remove_your_own_account(client, admin):
    login(client, admin)
    client.post(reverse("users:staff_delete", args=[admin.pk]))

    assert User.objects.filter(pk=admin.pk).exists()


# ---------------------------------------------------------------------------
# Removal: deactivate when they hold a portfolio
# ---------------------------------------------------------------------------


def test_someone_holding_accounts_is_deactivated_not_deleted(client, admin, plain):
    """The foreign keys are PROTECT, and their past work stays theirs."""
    AccountFactory(client_code="HELD", csm=plain)
    login(client, admin)

    client.post(reverse("users:staff_delete", args=[plain.pk]))
    plain.refresh_from_db()

    assert plain.is_active is False
    assert User.objects.filter(pk=plain.pk).exists()


def test_someone_holding_nothing_is_deleted_outright(client, admin, plain):
    login(client, admin)
    client.post(reverse("users:staff_delete", args=[plain.pk]))

    assert not User.objects.filter(pk=plain.pk).exists()


def test_the_confirm_page_says_which_will_happen(client, admin, plain):
    login(client, admin)
    body = client.get(reverse("users:staff_delete", args=[plain.pk])).content.decode()
    assert "deletes the record outright" in body

    AccountFactory(client_code="HELD2", csm=plain)
    body = client.get(reverse("users:staff_delete", args=[plain.pk])).content.decode()
    assert "deactivates rather than deletes" in body


# ---------------------------------------------------------------------------
# Your own password
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trait", ["csm", "acm", "manager", "senior_csm"])
def test_every_role_can_change_their_own_password(client, db, trait):
    """An account whose password only somebody else can change is not yours."""
    user = make(f"{trait}-pw@dphish.com", **{trait: True})
    login(client, user)

    response = client.post(
        reverse("users:password_change"),
        {
            "old_password": PASSWORD,
            "new_password1": "a-brand-new-password-4321",
            "new_password2": "a-brand-new-password-4321",
        },
    )

    assert response.status_code == 302
    user.refresh_from_db()
    assert user.check_password("a-brand-new-password-4321")


def test_changing_your_password_does_not_sign_you_out(client, plain):
    login(client, plain)
    client.post(
        reverse("users:password_change"),
        {
            "old_password": PASSWORD,
            "new_password1": "another-good-password-9999",
            "new_password2": "another-good-password-9999",
        },
    )

    # Still signed in on the browser that made the change.
    assert client.get(reverse("dashboard:index")).status_code == 200


def test_the_current_password_is_required(client, plain):
    """Otherwise an unattended browser is enough to take the account over."""
    login(client, plain)
    response = client.post(
        reverse("users:password_change"),
        {
            "old_password": "not-the-right-one",
            "new_password1": "whatever-12345678",
            "new_password2": "whatever-12345678",
        },
    )

    assert response.status_code == 200
    plain.refresh_from_db()
    assert plain.check_password(PASSWORD)


def test_an_administrator_can_set_someone_elses_password(client, admin, plain):
    login(client, admin)
    client.post(
        reverse("users:staff_set_password", args=[plain.pk]),
        {
            "new_password1": "reset-by-the-admin-77",
            "new_password2": "reset-by-the-admin-77",
        },
    )
    plain.refresh_from_db()

    assert plain.check_password("reset-by-the-admin-77")


def test_a_non_administrator_cannot_set_someone_elses_password(client, plain):
    other = make("victim@dphish.com", csm=True)
    login(client, plain)

    response = client.post(
        reverse("users:staff_set_password", args=[other.pk]),
        {
            "new_password1": "hijacked-password-123",
            "new_password2": "hijacked-password-123",
        },
    )

    assert response.status_code == 403
    other.refresh_from_db()
    assert other.check_password(PASSWORD)
