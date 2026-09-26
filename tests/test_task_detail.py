"""Reading one task, and being asked before deleting it.

Two behaviours that are easy to get subtly wrong: a row that opens the editor
when the reader only wanted to look, and a confirmation that either never
appears or never stops appearing.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from csm.core.enums import TaskCategory, TaskStatus, TaskType
from csm.tasks.models import Task
from tests.factories import AccountFactory, ContactFactory, UserFactory
from tests.test_views import PASSWORD, login


@pytest.fixture
def account(db):
    csm = UserFactory(csm=True, email="det-csm@dphish.com", full_name="Casey Morgan")
    csm.set_password(PASSWORD)
    csm.save(update_fields=["password"])
    return AccountFactory(client_code="DET1", client_name="Detail Co", csm=csm, acm=None)


@pytest.fixture
def task(account):
    return Task.objects.create(
        account=account, task_type=TaskType.CALL, title="Ring the CISO",
        description="Ask about the renewal", due_date=dt.date.today(),
        category=TaskCategory.CLIENT_FACING, created_by=account.csm,
    )


# ---------------------------------------------------------------------------
# Reading one
# ---------------------------------------------------------------------------


def test_a_row_opens_the_task_to_read_not_to_edit(client, account, task):
    """Most of the time the question is "what is this and when is it due", not
    "change its wording"."""
    login(client, account.csm)
    body = client.get(reverse("activities:list")).content.decode()

    assert f'href="{reverse("tasks:detail", args=[task.pk])}"' in body


def test_the_page_shows_the_task_and_offers_both_actions(client, account, task):
    contact = ContactFactory(account=account, full_name="Dana Farouk")
    task.contact = contact
    task.save(update_fields=["contact"])

    login(client, account.csm)
    body = client.get(reverse("tasks:detail", args=[task.pk])).content.decode()

    assert "Ring the CISO" in body
    assert "Ask about the renewal" in body
    assert "Dana Farouk" in body
    assert reverse("tasks:edit", args=[task.pk]) in body
    assert reverse("tasks:delete", args=[task.pk]) in body
    assert reverse("tasks:complete", args=[task.pk]) in body


def test_a_colleagues_task_is_a_404(client, account, task):
    stranger = UserFactory(csm=True, email="det-other@dphish.com")
    stranger.set_password(PASSWORD)
    stranger.save(update_fields=["password"])

    login(client, stranger)
    assert client.get(reverse("tasks:detail", args=[task.pk])).status_code == 404


def test_a_completed_task_shows_what_it_recorded(client, account, task):
    """The record it wrote, as opposed to the obligation it was."""
    from csm.activities.subscribers import log_completed_task
    from csm.tasks.services import complete_task

    complete_task(task, actor=account.csm)
    log_completed_task(task, actor=account.csm)

    login(client, account.csm)
    body = client.get(reverse("tasks:detail", args=[task.pk])).content.decode()

    assert "What it recorded" in body
    assert "Happened" in body


# ---------------------------------------------------------------------------
# Being asked first
# ---------------------------------------------------------------------------


def test_deleting_asks_before_it_deletes(client, account, task):
    login(client, account.csm)
    body = client.get(reverse("activities:list")).content.decode()

    assert f'hx-get="{reverse("tasks:delete", args=[task.pk])}' in body

    dialog = client.get(
        reverse("tasks:delete", args=[task.pk]), headers={"hx-request": "true"}
    ).content.decode()

    assert "<html" not in dialog, "a fragment, not a page"
    assert "Ring the CISO" in dialog
    assert 'name="suppress"' in dialog
    assert Task.objects.filter(pk=task.pk).exists(), "asking deletes nothing"


def test_the_dialog_says_a_generated_task_is_cancelled(client, account):
    """A hard delete frees its idempotency key and the task reappears the next
    morning looking like a bug."""
    generated = Task.objects.create(
        account=account, task_type=TaskType.AUTO_RENEWAL, title="Renewal in 90 days",
        due_date=dt.date.today(), auto_key="renewal:det:2026-12-31:90",
    )
    login(client, account.csm)

    dialog = client.get(
        reverse("tasks:delete", args=[generated.pk]), headers={"hx-request": "true"}
    ).content.decode()

    assert "cancelled rather than" in dialog
    assert "Cancel it" in dialog


def test_the_dialog_warns_that_a_repeat_carries_on(client, account):
    from csm.core.enums import RepeatEnd, RepeatFrequency
    from csm.tasks import recurrence
    from csm.tasks.models import TaskSeries

    series = TaskSeries.objects.create(
        account=account, task_type=TaskType.MEETING, title="Weekly sync",
        frequency=RepeatFrequency.WEEKLY, ends=RepeatEnd.NEVER, created_by=account.csm,
    )
    occurrence = recurrence.start(series, first_due=dt.date.today())

    login(client, account.csm)
    dialog = client.get(
        reverse("tasks:delete", args=[occurrence.pk]), headers={"hx-request": "true"}
    ).content.decode()

    assert "The repeat carries on" in dialog


def test_not_asking_again_lasts_exactly_today(client, account, task):
    """Stored as a date rather than a flag, so it expires overnight without
    anything having to clean it up."""
    import datetime

    login(client, account.csm)
    client.post(
        reverse("tasks:delete", args=[task.pk]),
        {"suppress": "1", "next": reverse("activities:list")},
    )

    assert client.session["skip_delete_confirm_on"] == dt.date.today().isoformat()

    second = Task.objects.create(
        account=account, task_type=TaskType.CALL, title="Another call",
        due_date=dt.date.today(), created_by=account.csm,
    )
    body = client.get(reverse("activities:list")).content.decode()
    assert f'hx-get="{reverse("tasks:delete", args=[second.pk])}' not in body, (
        "asked once, not asked again today"
    )

    # Tomorrow's session holds yesterday's date, which no longer matches.
    session = client.session
    session["skip_delete_confirm_on"] = (
        dt.date.today() - datetime.timedelta(days=1)
    ).isoformat()
    session.save()

    body = client.get(reverse("activities:list")).content.decode()
    assert f'hx-get="{reverse("tasks:delete", args=[second.pk])}' in body, (
        "the next working day asks again"
    )


def test_deleting_without_the_box_leaves_the_asking_on(client, account, task):
    login(client, account.csm)
    client.post(
        reverse("tasks:delete", args=[task.pk]), {"next": reverse("activities:list")}
    )

    assert "skip_delete_confirm_on" not in client.session
    assert not Task.objects.filter(pk=task.pk, status=TaskStatus.PENDING).exists()


def test_the_delete_link_still_works_without_javascript(client, account, task):
    """If HTMX never loads, the same href opens the confirmation page."""
    login(client, account.csm)
    page = client.get(reverse("tasks:delete", args=[task.pk]))

    assert page.status_code == 200
    assert "<html" in page.content.decode()
    assert Task.objects.filter(pk=task.pk).exists()


# ---------------------------------------------------------------------------
# A long title does not stretch the table
# ---------------------------------------------------------------------------


def test_a_long_title_is_clipped_rather_than_widening_the_column(client, account):
    """Clipped by CSS rather than by words: a title with no spaces in it
    stretches a column that word-truncation would have left alone."""
    long_title = "MEETINGWITHTHEWHOLEBOARD" * 8
    Task.objects.create(
        account=account, task_type=TaskType.MEETING, title=long_title,
        due_date=dt.date.today(), created_by=account.csm,
    )
    login(client, account.csm)
    body = client.get(reverse("activities:list")).content.decode()

    assert "col-subject" in body
    assert "cell-clip" in body
    assert f'title="{long_title}"' in body, "and the whole thing is in the tooltip"
