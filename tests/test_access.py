"""Authorization tests.

The strictest rule in the specification is that an ACM never sees health
information anywhere, so most of this file is about that.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest
from django.core.exceptions import PermissionDenied

from csm.activities.models import Activity
from csm.core.access import capabilities as cap
from csm.core.access.abilities import Ability
from csm.core.access.presenters import AccountPresenter
from csm.core.enums import ActivitySource, TaskType
from csm.tasks.models import Task
from tests.factories import AccountFactory, ActivityFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def portfolio(csm_user, senior_csm_user, acm_user, manager_user):
    """Two accounts owned by different people, plus one owned by nobody."""
    other_csm = UserFactory(csm=True, email="other@dphish.com", full_name="Robin Vale")
    other_acm = UserFactory(acm=True, email="otheracm@dphish.com", full_name="Jordan Kim")
    mine = AccountFactory(client_code="MINE", csm=csm_user, acm=acm_user)
    theirs = AccountFactory(client_code="THEIRS", csm=other_csm, acm=other_acm)
    orphan = AccountFactory(client_code="ORPHAN", csm=None, acm=None)
    return {"mine": mine, "theirs": theirs, "orphan": orphan}


def codes(ability) -> set[str]:
    return set(ability.accounts().values_list("client_code", flat=True))


# ---------------------------------------------------------------------------
# Row scope
# ---------------------------------------------------------------------------


def test_a_csm_sees_only_their_own_accounts(portfolio, csm_user):
    assert codes(Ability(csm_user)) == {"MINE"}


def test_a_senior_csm_sees_every_account(portfolio, senior_csm_user):
    assert codes(Ability(senior_csm_user)) == {"MINE", "THEIRS", "ORPHAN"}


def test_an_acm_sees_only_the_accounts_assigned_to_them(portfolio, acm_user):
    assert codes(Ability(acm_user)) == {"MINE"}


def test_a_manager_sees_every_account(portfolio, manager_user):
    assert codes(Ability(manager_user)) == {"MINE", "THEIRS", "ORPHAN"}


def test_an_inactive_user_sees_nothing(portfolio, csm_user):
    csm_user.is_active = False
    csm_user.save(update_fields=["is_active"])
    assert codes(Ability(csm_user)) == set()


def test_scope_and_search_agree(portfolio, csm_user):
    """Search must respect scope or it becomes a way around it."""
    ability = Ability(csm_user)
    assert ability.accounts().search("THEIRS").count() == 0
    assert ability.accounts().search("MINE").count() == 1


def test_a_soft_deleted_account_disappears_for_everyone(portfolio, senior_csm_user):
    from csm.core import clock

    account = portfolio["mine"]
    account.deleted_at = clock.now()
    account.save(update_fields=["deleted_at"])
    assert "MINE" not in codes(Ability(senior_csm_user))


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_an_acm_never_holds_the_health_capability(acm_user):
    ability = Ability(acm_user)
    assert not ability.can(cap.HEALTH_VIEW)
    assert not ability.can(cap.HEALTH_OVERRIDE)
    assert not ability.can_view_health()


def test_an_acm_holds_neither_account_edit_nor_csat(acm_user):
    ability = Ability(acm_user)
    assert not ability.can(cap.ACCOUNT_EDIT)
    assert not ability.can(cap.CSAT_RECORD)


@pytest.mark.parametrize("fixture_name", ["csm_user", "senior_csm_user", "manager_user"])
def test_everyone_except_an_acm_may_see_health(request, fixture_name):
    user = request.getfixturevalue(fixture_name)
    assert Ability(user).can(cap.HEALTH_VIEW)


def test_a_csm_may_delete_their_own_account_but_not_a_manager(
    portfolio, csm_user, senior_csm_user, manager_user
):
    account = portfolio["mine"]
    assert Ability(csm_user).can_delete_account(account)
    assert Ability(senior_csm_user).can_delete_account(account)
    assert not Ability(manager_user).can_delete_account(account)


def test_any_csm_or_acm_may_run_the_import_but_not_a_manager(
    csm_user, senior_csm_user, acm_user, manager_user
):
    assert Ability(csm_user).can(cap.IMPORT_RUN)
    assert Ability(senior_csm_user).can(cap.IMPORT_RUN)
    assert Ability(acm_user).can(cap.IMPORT_RUN)
    assert not Ability(manager_user).can(cap.IMPORT_RUN)


def test_an_acm_may_only_import_contacts_not_accounts(
    csm_user, senior_csm_user, acm_user
):
    """The importer writes accounts with no ``account.edit`` check of its own,
    so the ACM's read-only hold on the account record still has to be enforced
    here rather than by the bare capability."""
    from csm.importer.models import ImportKind

    assert Ability(acm_user).importable_kinds() == {ImportKind.CONTACTS}
    assert Ability(csm_user).importable_kinds() == {
        ImportKind.ACCOUNTS,
        ImportKind.CONTACTS,
    }
    assert Ability(senior_csm_user).importable_kinds() == {
        ImportKind.ACCOUNTS,
        ImportKind.CONTACTS,
    }


def test_an_acm_may_now_export(acm_user):
    assert Ability(acm_user).can(cap.EXPORT_RUN)


def test_a_manager_can_edit_nothing(portfolio, manager_user):
    ability = Ability(manager_user)
    account = portfolio["mine"]
    assert not ability.can_edit_account(account)
    assert not ability.can_log_activity(account)
    assert not ability.can(cap.TASK_CREATE)
    assert not ability.can(cap.ACCOUNT_CREATE)


def test_a_senior_csm_may_act_on_an_account_that_is_not_theirs(portfolio, senior_csm_user):
    assert Ability(senior_csm_user).can_edit_account(portfolio["theirs"])


def test_a_csm_may_not_act_on_a_colleagues_account(portfolio, csm_user):
    assert not Ability(csm_user).can_edit_account(portfolio["theirs"])


def test_an_acm_may_not_edit_even_their_own_account(portfolio, acm_user):
    assert not Ability(acm_user).can_edit_account(portfolio["mine"])


def test_an_unknown_capability_is_an_error_not_a_silent_no(csm_user):
    with pytest.raises(ValueError):
        Ability(csm_user).can("account.destroy_everything")


# ---------------------------------------------------------------------------
# Field scope
# ---------------------------------------------------------------------------


ALL_FIELDS = [
    "client_name",
    "contract_end_date",
    "renewal_count",
    "partner",
    "lifecycle_stage",
    "is_managed_service",
    "account_class",
    "required_meeting_frequency",
    "effective_health_status",
]


def test_an_acm_may_not_edit_the_account_at_all(acm_user):
    assert not Ability(acm_user).can(cap.ACCOUNT_EDIT)
    assert Ability(acm_user).editable_account_fields(ALL_FIELDS) == frozenset()


def test_a_csm_may_edit_the_lifecycle_stage(csm_user):
    assert "lifecycle_stage" in Ability(csm_user).editable_account_fields(ALL_FIELDS)


def test_no_role_may_edit_a_system_written_field(csm_user, senior_csm_user, acm_user):
    for user in (csm_user, senior_csm_user, acm_user):
        assert "effective_health_status" not in Ability(user).editable_account_fields(ALL_FIELDS)


def test_a_manager_may_edit_nothing_at_all(manager_user):
    assert Ability(manager_user).editable_account_fields(ALL_FIELDS) == frozenset()


# ---------------------------------------------------------------------------
# The presenter boundary
# ---------------------------------------------------------------------------


def test_the_presenter_raises_rather_than_returning_none_for_an_acm(portfolio, acm_user):
    presenter = AccountPresenter(portfolio["mine"], Ability(acm_user))

    assert presenter.client_name  # ordinary fields still work
    for attribute in ("health_status", "health_score", "health_max", "red_since"):
        with pytest.raises(PermissionDenied):
            getattr(presenter, attribute)


def test_the_presenter_serves_health_to_a_csm(portfolio, csm_user):
    presenter = AccountPresenter(portfolio["mine"], Ability(csm_user))
    assert presenter.health_status is None or isinstance(presenter.health_status, str)
    assert presenter.shows_health is True


def test_the_presenter_dict_for_an_acm_carries_no_health_key(portfolio, acm_user):
    data = AccountPresenter(portfolio["mine"], Ability(acm_user)).as_dict()
    assert "health_status" not in data
    assert "health_score" not in data
    assert not any("health" in key for key in data)


def test_the_presenter_dict_for_a_csm_carries_health(portfolio, csm_user):
    data = AccountPresenter(portfolio["mine"], Ability(csm_user)).as_dict()
    assert "health_status" in data


def test_the_presenter_refuses_to_pass_through_an_unlisted_field(portfolio, acm_user):
    """A field is exposed on purpose or not at all, so nothing leaks by default."""
    presenter = AccountPresenter(portfolio["mine"], Ability(acm_user))
    for hidden in ("effective_health_status", "computed_health_score"):
        with pytest.raises(AttributeError):
            getattr(presenter, hidden)


def test_no_safe_field_mentions_health():
    assert not any("health" in name for name in AccountPresenter.SAFE_FIELDS)


# ---------------------------------------------------------------------------
# Task visibility
# ---------------------------------------------------------------------------


def test_an_acm_does_not_see_health_alerts_even_on_their_own_accounts(portfolio, acm_user, csm_user):
    account = portfolio["mine"]
    Task.objects.create(
        account=account,
        task_type=TaskType.AUTO_HEALTH_ALERT,
        title="At Risk",
        due_date=account.created_at.date(),
    )
    Task.objects.create(
        account=account,
        task_type=TaskType.AUTO_RENEWAL,
        title="Renewal",
        due_date=account.created_at.date(),
    )

    acm_tasks = Ability(acm_user).visible_tasks()
    assert acm_tasks.count() == 1
    assert acm_tasks.get().task_type == TaskType.AUTO_RENEWAL

    assert Ability(csm_user).visible_tasks().count() == 2


# ---------------------------------------------------------------------------
# Activity visibility
# ---------------------------------------------------------------------------


def test_an_acm_sees_only_system_logged_activities(portfolio, acm_user, csm_user):
    account = portfolio["mine"]
    ActivityFactory(account=account, source=ActivitySource.MANUAL)
    ActivityFactory(account=account, source=ActivitySource.OUTLOOK)

    acm_activities = Ability(acm_user).visible_activities(Activity.objects.all())
    assert acm_activities.count() == 1
    assert acm_activities.get().source == ActivitySource.OUTLOOK

    csm_activities = Ability(csm_user).visible_activities(Activity.objects.all())
    assert csm_activities.count() == 2


# ---------------------------------------------------------------------------
# The structural guarantee
# ---------------------------------------------------------------------------


def test_only_the_access_package_interprets_role_or_seniority():
    """Role interpretation has exactly one home.

    A reference outside it means an authorization decision is being made
    somewhere that the access tests do not cover.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "csm"
    allowed_prefixes = (
        root / "core" / "access",
        root / "users",  # defines the fields
    )
    offenders = []

    for path in root.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        if any(str(path).startswith(str(prefix)) for prefix in allowed_prefixes):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "is_senior":
                offenders.append(f"{path.relative_to(root)}:{node.lineno} is_senior")
    assert offenders == [], "\n".join(offenders)


def test_the_routing_module_does_not_import_the_access_package():
    from csm.notifications import routing

    source = inspect.getsource(routing)
    tree = ast.parse(source)
    modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(m.startswith("csm.core.access") for m in modules)
