"""The management commands themselves.

The services underneath are thoroughly tested; these wrappers were not tested at
all, which is the wrong way round for the three that run unattended every night.
A command that raises on startup - a renamed argument, an import that moved, a
field that no longer exists - stops the daily half of the dual trigger, and
nothing anywhere says so. The failure is a portfolio whose numbers quietly stop
moving.

``seed_demo`` earns its place here for the same reason in miniature: it broke
silently when a field was removed, and was only caught by somebody running it by
hand.
"""

from __future__ import annotations

import datetime as dt
from io import StringIO

import pytest
from django.core.management import call_command

from csm.accounts.models import Account
from csm.contacts.models import Contact
from csm.core.enums import NotificationChannel, NotificationKind, TaskType
from csm.engagement.models import EngagementItem
from csm.notifications.models import Notification
from csm.tasks.models import Task
from tests.factories import AccountFactory, UserFactory


def run(name, *args) -> str:
    out = StringIO()
    call_command(name, *args, stdout=out, stderr=out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# recalculate_health
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_recalculate_health_runs_and_scores_the_portfolio():
    account = AccountFactory(
        client_code="CMD1", scoring_baseline_date=dt.date.today() - dt.timedelta(days=365)
    )
    Account.objects.filter(pk=account.pk).update(health_calculated_at=None)

    output = run("recalculate_health")

    account.refresh_from_db()
    assert account.health_calculated_at is not None
    assert account.effective_health_status
    assert output


@pytest.mark.django_db
def test_recalculate_health_is_safe_on_an_empty_portfolio():
    """It runs nightly whether or not anybody has imported anything yet."""
    assert Account.objects.count() == 0
    run("recalculate_health")


@pytest.mark.django_db
def test_recalculate_health_skips_unscored_stages_without_failing():
    AccountFactory(client_code="CMD2", churned=True)
    AccountFactory(client_code="CMD3", poc=True)

    run("recalculate_health")

    for code in ("CMD2", "CMD3"):
        account = Account.objects.get(client_code=code)
        assert account.computed_health_score is None


# ---------------------------------------------------------------------------
# generate_tasks
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_generate_tasks_creates_the_renewal_checkpoint():
    AccountFactory(
        client_code="CMD4",
        contract_end_date=dt.date.today() + dt.timedelta(days=30),
    )

    output = run("generate_tasks")

    assert Task.objects.filter(task_type=TaskType.AUTO_RENEWAL).exists()
    assert output


@pytest.mark.django_db
def test_generate_tasks_twice_creates_nothing_the_second_time():
    """It runs every night. The idempotency key is what makes that safe."""
    AccountFactory(
        client_code="CMD5",
        contract_end_date=dt.date.today() + dt.timedelta(days=60),
    )

    run("generate_tasks")
    before = Task.objects.count()
    run("generate_tasks")

    assert Task.objects.count() == before


@pytest.mark.django_db
def test_generate_tasks_is_safe_on_an_empty_portfolio():
    run("generate_tasks")


# ---------------------------------------------------------------------------
# send_due_notifications
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_send_due_notifications_runs_with_nothing_pending():
    run("send_due_notifications")


@pytest.mark.django_db
def test_send_due_notifications_drains_the_email_outbox():
    from django.core import mail

    user = UserFactory(csm=True, email="cmd-csm@dphish.com")
    account = AccountFactory(client_code="CMD6", csm=user)
    Notification.objects.create(
        user=user,
        account=account,
        kind=NotificationKind.HEALTH_ALERT,
        channel=NotificationChannel.EMAIL,
        title="Health alert: CMD6",
        body="This account has gone red.",
    )

    run("send_due_notifications")

    assert len(mail.outbox) >= 1
    Notification.objects.filter(channel=NotificationChannel.EMAIL).get().refresh_from_db()


@pytest.mark.django_db
def test_send_due_notifications_does_not_resend_what_it_already_sent():
    """It runs every morning; a second run must not repeat yesterday's post."""
    from django.core import mail

    user = UserFactory(csm=True, email="cmd-csm2@dphish.com")
    account = AccountFactory(client_code="CMD7", csm=user)
    Notification.objects.create(
        user=user, account=account, kind=NotificationKind.HEALTH_ALERT,
        channel=NotificationChannel.EMAIL, title="Health alert: CMD7",
    )

    run("send_due_notifications")
    first = len(mail.outbox)
    run("send_due_notifications")

    assert len(mail.outbox) == first


# ---------------------------------------------------------------------------
# seed_demo
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_seed_demo_builds_a_working_portfolio():
    """A smoke test, because it broke silently when a field was removed.

    Small on purpose: this asserts the command still runs end to end against
    the current schema, not that the demo data has any particular shape.
    """
    run("seed_demo", "--accounts", "3", "--seed", "1")

    accounts = Account.objects.all()
    assert accounts.count() == 3
    assert Contact.objects.exists()
    assert EngagementItem.objects.exists(), "card 11 would have nothing to plot"

    # Every account in a scorable stage comes out with a status. The others
    # are left alone on purpose: `recalculate_all` visits scorable accounts
    # only, so a churned account generates no daily history row. Its N/A is
    # written by the event path instead, which needs a commit a test never
    # reaches.
    for account in accounts:
        assert account.scoring_baseline_date is not None
    for account in Account.objects.scorable():
        assert account.effective_health_status is not None
        assert account.computed_health_score is not None


@pytest.mark.django_db
def test_seed_demo_reset_clears_what_it_made_before():
    run("seed_demo", "--accounts", "2", "--seed", "1")
    run("seed_demo", "--accounts", "2", "--seed", "1", "--reset")

    assert Account.objects.count() == 2


@pytest.mark.django_db
def test_seed_demo_leaves_contacts_for_the_triage_tray():
    """The grid and the tray are both exercised only if the seed spreads them."""
    run("seed_demo", "--accounts", "8", "--seed", "1")

    assessed = [c for c in Contact.objects.all() if c.is_assessed]
    assert assessed, "nothing would appear on the grid"
    assert Contact.objects.needs_triage().exists() or len(assessed) == Contact.objects.count()
