"""Jira Service Management into tickets.

What matters, in order: an issue lands on the client its organization names and
on no other; it scores as the right kind; re-reading it changes nothing; an
issue that leaves Jira's open set stops counting; and a failed read writes
nothing and loses no ground. Then the edges people see: manual entry withdrawn
once Jira is on, synced tickets not closable here, the admin page.
"""

from __future__ import annotations

import datetime as dt
from io import StringIO
from urllib.parse import parse_qs, urlparse

import pytest
import time_machine
from django.core.management import call_command
from django.urls import reverse

from csm.core.enums import IntegrationProvider, TicketKind, TicketSource, TicketStatus
from csm.engagement.models import Ticket
from csm.integrations.jira import settings as jira_settings
from csm.integrations.jira import sync
from csm.integrations.jira.client import (
    CATEGORY_DONE,
    CATEGORY_IN_PROGRESS,
    CATEGORY_TODO,
    FakeJiraClient,
    JiraError,
    JiraIssue,
)
from csm.integrations.jira.http import HttpJiraClient
from csm.integrations.models import SyncState
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login

NOW = dt.datetime(2026, 9, 30, 9, 0, tzinfo=dt.UTC)


def issue(key="SUP-1", *, orgs=("Acme Corp",), kind="Task", category=CATEGORY_TODO,
          status="Open", updated=None, created=None, resolved=None, summary=None):
    updated = updated or NOW - dt.timedelta(hours=1)
    return JiraIssue(
        key=key,
        issue_type=kind,
        status=status,
        status_category=category,
        summary=summary if summary is not None else f"Issue {key}",
        created=created or NOW - dt.timedelta(days=3),
        updated=updated,
        resolved=resolved,
        organizations=tuple(orgs),
    )


@pytest.fixture(autouse=True)
def _frozen():
    with time_machine.travel(NOW, tick=False):
        yield


@pytest.fixture
def acme(db):
    return AccountFactory(client_name="Acme Corp", client_code="ACME")


@pytest.fixture
def jira_on(settings):
    settings.JIRA_BASE_URL = "https://dphish.atlassian.net"
    settings.JIRA_EMAIL = "bot@dphish.com"
    settings.JIRA_API_TOKEN = "token"
    return settings


def jira_tickets():
    return Ticket.objects.filter(source=TicketSource.JIRA)


# ---------------------------------------------------------------------------
# Mapping one issue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, expected",
    [("Bug", TicketKind.BUG), ("bug", TicketKind.BUG), ("Task", TicketKind.GENERAL),
     ("Service Request", TicketKind.GENERAL), ("Incident", TicketKind.GENERAL)],
)
def test_only_bug_types_score_as_bugs(kind, expected):
    assert sync.ticket_values(issue(kind=kind))["ticket_kind"] == expected


def test_the_bug_types_are_configurable():
    values = sync.ticket_values(issue(kind="Incident"), ("Bug", "Incident"))
    assert values["ticket_kind"] == TicketKind.BUG


@pytest.mark.parametrize("category", [CATEGORY_TODO, CATEGORY_IN_PROGRESS])
def test_any_category_but_done_is_open(category):
    values = sync.ticket_values(issue(category=category))
    assert values["status"] == TicketStatus.OPEN
    assert values["closed_on"] is None


def test_done_closes_on_the_business_date_of_resolution():
    # 22:30 UTC on 30 September is 01:30 on 1 October in Cairo (+03:00).
    resolved = dt.datetime(2026, 9, 30, 22, 30, tzinfo=dt.UTC)
    values = sync.ticket_values(issue(category=CATEGORY_DONE, status="Resolved", resolved=resolved))
    assert values["status"] == TicketStatus.CLOSED
    assert values["closed_on"] == dt.date(2026, 10, 1)
    assert values["external_status"] == "Resolved"


def test_done_without_a_resolution_date_closes_when_it_last_changed():
    updated = dt.datetime(2026, 9, 28, 8, 0, tzinfo=dt.UTC)
    values = sync.ticket_values(issue(category=CATEGORY_DONE, updated=updated))
    assert values["closed_on"] == dt.date(2026, 9, 28)


def test_a_long_summary_is_cut_to_fit_and_an_empty_one_uses_the_key():
    assert len(sync.ticket_values(issue(summary="x" * 400))["title"]) == 255
    assert sync.ticket_values(issue(summary=""))["title"] == "SUP-1"


# ---------------------------------------------------------------------------
# Matching organizations to clients
# ---------------------------------------------------------------------------


def test_an_issue_lands_on_the_client_its_organization_names(acme):
    sync.run(FakeJiraClient([issue(orgs=["  ACME   corp "])]))

    ticket = jira_tickets().get()
    assert ticket.account == acme
    assert ticket.external_key == "SUP-1"
    assert ticket.created_by is None


def test_an_unknown_organization_is_reported_never_guessed(acme):
    report = sync.run(FakeJiraClient([issue(orgs=["Acme Corporation"])]))

    assert not jira_tickets().exists()
    assert report["unmatched_organizations"] == {"Acme Corporation": 1}


def test_two_clients_with_one_name_get_nothing_and_are_reported(acme):
    AccountFactory(client_name="acme corp", client_code="ACME2")
    report = sync.run(FakeJiraClient([issue()]))

    assert not jira_tickets().exists()
    assert report["ambiguous_organizations"] == {"Acme Corp": 1}


def test_an_issue_under_two_organizations_counts_for_both(acme):
    globex = AccountFactory(client_name="Globex", client_code="GLBX")
    sync.run(FakeJiraClient([issue(orgs=["Acme Corp", "Globex"])]))

    assert set(jira_tickets().values_list("account_id", flat=True)) == {acme.pk, globex.pk}


def test_a_deleted_account_receives_no_tickets(acme):
    acme.deleted_at = NOW
    acme.save(update_fields=["deleted_at"])

    report = sync.run(FakeJiraClient([issue()]))

    assert not jira_tickets().exists()
    assert report["unmatched_organizations"] == {"Acme Corp": 1}


def test_an_issue_moved_to_another_organization_moves_with_it(acme):
    globex = AccountFactory(client_name="Globex", client_code="GLBX")
    client = FakeJiraClient([issue()])
    sync.run(client)

    client.issues = [issue(orgs=["Globex"], updated=NOW)]
    report = sync.run(client, full=True)

    assert list(jira_tickets().values_list("account_id", flat=True)) == [globex.pk]
    assert report["removed"] == 1


# ---------------------------------------------------------------------------
# Re-running, sweeping, failing
# ---------------------------------------------------------------------------


def test_reading_the_same_issue_twice_changes_nothing(acme):
    client = FakeJiraClient([issue(), issue("SUP-2", kind="Bug")])
    sync.run(client)

    with time_machine.travel(NOW + dt.timedelta(minutes=15), tick=False):
        report = sync.run(client, full=True)

    assert jira_tickets().count() == 2
    assert report["created"] == 0
    assert report["updated"] == 0


def test_a_change_in_jira_updates_the_ticket(acme):
    client = FakeJiraClient([issue()])
    sync.run(client)

    later = NOW + dt.timedelta(minutes=15)
    client.issues = [issue(category=CATEGORY_DONE, status="Done", updated=later, resolved=later)]
    with time_machine.travel(later + dt.timedelta(minutes=1), tick=False):
        report = sync.run(client)

    ticket = jira_tickets().get()
    assert ticket.status == TicketStatus.CLOSED
    assert ticket.external_status == "Done"
    assert report["updated"] == 1


def test_the_sweep_closes_a_ticket_jira_no_longer_has_open(acme, csm_user):
    manual = Ticket.objects.create(
        account=acme, ticket_kind=TicketKind.BUG, title="Typed in by hand",
        opened_on=dt.date(2026, 9, 1), created_by=csm_user,
    )
    client = FakeJiraClient([issue(), issue("SUP-2")])
    sync.run(client)

    client.issues = [issue()]  # SUP-2 deleted in Jira
    report = sync.run(client, full=True)

    gone = jira_tickets().get(external_key="SUP-2")
    assert gone.status == TicketStatus.CLOSED
    assert gone.external_status == sync.GONE_STATUS
    assert report["closed_by_sweep"] == 1
    manual.refresh_from_db()
    assert manual.status == TicketStatus.OPEN


def test_an_incremental_run_does_not_sweep_until_a_day_has_passed(acme):
    client = FakeJiraClient([issue("SUP-2")])
    sync.run(client)
    client.issues = []

    with time_machine.travel(NOW + dt.timedelta(hours=1), tick=False):
        assert sync.run(client)["swept"] is False
    assert jira_tickets().get().status == TicketStatus.OPEN

    with time_machine.travel(NOW + dt.timedelta(hours=25), tick=False):
        assert sync.run(client)["swept"] is True
    assert jira_tickets().get().status == TicketStatus.CLOSED


def test_the_first_run_backfills_and_later_runs_resume_from_the_cursor(acme, settings):
    settings.JIRA_BACKFILL_DAYS = 30
    old_closed = issue("SUP-OLD", category=CATEGORY_DONE, updated=NOW - dt.timedelta(days=60))
    recent_closed = issue("SUP-NEW", category=CATEGORY_DONE, updated=NOW - dt.timedelta(days=5))
    ancient_open = issue("SUP-OPEN", updated=NOW - dt.timedelta(days=400))
    sync.run(FakeJiraClient([old_closed, recent_closed, ancient_open]))

    assert set(jira_tickets().values_list("external_key", flat=True)) == {"SUP-NEW", "SUP-OPEN"}
    assert SyncState.objects.get(provider=IntegrationProvider.JIRA).cursor == NOW


def test_a_failed_read_writes_nothing_and_keeps_the_cursor(acme):
    client = FakeJiraClient([issue()])
    sync.run(client)

    client.issues = [issue("SUP-2", updated=NOW + dt.timedelta(minutes=30))]
    client.error = JiraError("Jira answered 503")
    with (
        time_machine.travel(NOW + dt.timedelta(hours=1), tick=False),
        pytest.raises(JiraError),
    ):
        sync.run(client)

    state = SyncState.objects.get(provider=IntegrationProvider.JIRA)
    assert state.cursor == NOW
    assert state.last_error == "Jira answered 503"
    assert jira_tickets().count() == 1

    client.error = None
    with time_machine.travel(NOW + dt.timedelta(hours=2), tick=False):
        sync.run(client)
    state.refresh_from_db()
    assert state.last_error == ""
    assert jira_tickets().count() == 2


def test_unmatched_organizations_survive_quiet_runs_until_a_sweep(acme):
    client = FakeJiraClient([issue(orgs=["Initech"])])
    sync.run(client)

    with time_machine.travel(NOW + dt.timedelta(hours=1), tick=False):
        quiet = sync.run(FakeJiraClient([]))
    assert quiet["unmatched_organizations"] == {"Initech": 1}

    with time_machine.travel(NOW + dt.timedelta(hours=2), tick=False):
        swept = sync.run(FakeJiraClient([]), full=True)
    assert swept["unmatched_organizations"] == {}


# ---------------------------------------------------------------------------
# The score moves
# ---------------------------------------------------------------------------


def test_a_synced_bug_rescores_the_account(acme, django_capture_on_commit_callbacks):
    """Through the model hook, once, after commit - no code here calls the engine."""
    from csm.health import signals

    # The account fixture queued a recalculation that never commits; a real
    # commit would have cleared it.
    signals._pending.clear()
    with django_capture_on_commit_callbacks(execute=True):
        sync.run(FakeJiraClient([issue(kind="Bug"), issue("SUP-2", kind="Bug")]))

    acme.refresh_from_db()
    assert acme.health_calculated_at is not None
    from csm.health.inputs import gather_inputs

    scoring = gather_inputs(acme, as_of=dt.date(2026, 9, 30))
    assert scoring.open_bug_tickets == 2


# ---------------------------------------------------------------------------
# The HTTP client, against a recorded transport
# ---------------------------------------------------------------------------


def _raw(key, org="Acme Corp"):
    return {
        "key": key,
        "fields": {
            "summary": f"Summary {key}",
            "issuetype": {"name": "Bug"},
            "status": {"name": "Waiting for support", "statusCategory": {"key": "indeterminate"}},
            "created": "2026-09-27T10:15:00.000+0300",
            "updated": "2026-09-30T11:00:00.000+0300",
            "resolutiondate": None,
            "customfield_10002": [{"id": "7", "name": org}],
        },
    }


class RecordedJira:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        if url.endswith("/rest/api/3/field"):
            return [
                {"id": "summary", "schema": {"type": "string"}},
                {"id": "customfield_10002",
                 "schema": {"custom": "com.atlassian.servicedesk:sd-customer-organizations"}},
            ]
        return self.pages.pop(0)


def test_the_http_client_discovers_the_field_and_follows_every_page():
    transport = RecordedJira([
        {"issues": [_raw("SUP-1")], "nextPageToken": "abc", "isLast": False},
        {"issues": [_raw("SUP-2", org="Globex")], "isLast": True},
    ])
    client = HttpJiraClient(
        base_url="https://dphish.atlassian.net/", email="bot@dphish.com", api_token="t",
        project_keys=("SUP",), transport=transport,
    )

    issues = list(client.issues_updated_since(NOW - dt.timedelta(minutes=95)))

    assert [i.key for i in issues] == ["SUP-1", "SUP-2"]
    assert issues[1].organizations == ("Globex",)
    assert issues[0].created == dt.datetime(2026, 9, 27, 7, 15, tzinfo=dt.UTC)
    first = parse_qs(urlparse(transport.calls[1]).query)
    assert first["jql"] == [
        'cf[10002] is not EMPTY AND project in ("SUP") AND updated >= "-95m" ORDER BY updated ASC'
    ]
    assert "customfield_10002" in first["fields"][0]
    assert parse_qs(urlparse(transport.calls[2]).query)["nextPageToken"] == ["abc"]


def test_a_site_without_jsm_is_an_error_not_an_empty_sync():
    client = HttpJiraClient(
        base_url="https://x.atlassian.net", email="a", api_token="b",
        transport=lambda url, headers: [{"id": "summary", "schema": {}}],
    )
    with pytest.raises(JiraError, match="no Organizations field"):
        list(client.open_issues())


def test_a_configured_field_id_skips_discovery():
    transport = RecordedJira([{"issues": [], "isLast": True}])
    client = HttpJiraClient(
        base_url="https://x.atlassian.net", email="a", api_token="b",
        organizations_field="customfield_10002", transport=transport,
    )
    list(client.open_issues())
    assert len(transport.calls) == 1
    assert "statusCategory+%21%3D+Done" in transport.calls[0]


# ---------------------------------------------------------------------------
# Configuration and the command
# ---------------------------------------------------------------------------


def test_jira_is_off_until_all_three_settings_are_present(settings, db):
    assert not jira_settings.is_configured()
    settings.JIRA_BASE_URL = "https://dphish.atlassian.net"
    settings.JIRA_EMAIL = "bot@dphish.com"
    assert not jira_settings.is_configured()
    with pytest.raises(Exception, match="API token"):
        jira_settings.get_client()
    settings.JIRA_API_TOKEN = "token"
    assert jira_settings.is_configured()
    assert isinstance(jira_settings.get_client(), HttpJiraClient)


def test_the_command_is_quiet_when_jira_is_off(db):
    out = StringIO()
    call_command("sync_jira", stdout=out)
    assert "not configured" in out.getvalue()
    assert not SyncState.objects.exists()


def test_the_command_syncs_and_names_what_it_missed(jira_on, acme, monkeypatch):
    fake = FakeJiraClient([issue(), issue("SUP-9", orgs=["Initech"])])
    monkeypatch.setattr(jira_settings, "get_client", lambda: fake)
    out = StringIO()
    call_command("sync_jira", "--full", stdout=out)

    assert "1 created" in out.getvalue()
    assert "No account named 'Initech'" in out.getvalue()


def test_the_sync_is_on_the_schedule():
    from csm.core.management.commands.setup_schedules import MINUTE_JOBS

    assert ("csm.sync_jira", "csm.core.jobs.sync_jira", 15) in MINUTE_JOBS


# ---------------------------------------------------------------------------
# What people see
# ---------------------------------------------------------------------------


@pytest.fixture
def csm(acme):
    user = acme.csm
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    return user


@pytest.fixture
def admin(db):
    user = UserFactory(admin=True, email="jira-admin@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    return user


def test_manual_entry_is_withdrawn_once_jira_is_on(client, csm, acme, jira_on):
    login(client, csm)
    url = reverse("engagement:ticket_create", args=[acme.pk])
    assert client.get(url).status_code == 404

    page = client.get(reverse("health:panel", args=[acme.pk])).content.decode()
    assert url not in page
    assert "From Jira" in page


def test_manual_entry_stays_while_jira_is_off(client, csm, acme):
    login(client, csm)
    assert client.get(reverse("engagement:ticket_create", args=[acme.pk])).status_code == 200


def test_a_synced_ticket_cannot_be_closed_or_deleted_here(client, csm, acme, jira_on):
    sync.run(FakeJiraClient([issue()]))
    ticket = jira_tickets().get()
    login(client, csm)

    close = client.post(reverse("engagement:ticket_close", args=[acme.pk, ticket.pk]))
    delete = client.post(reverse("engagement:delete", args=[acme.pk, "ticket", ticket.pk]))

    assert (close.status_code, delete.status_code) == (404, 404)
    ticket.refresh_from_db()
    assert ticket.status == TicketStatus.OPEN


def test_a_synced_ticket_links_to_its_issue(client, csm, acme, jira_on):
    sync.run(FakeJiraClient([issue()]))
    login(client, csm)
    page = client.get(reverse("health:panel", args=[acme.pk])).content.decode()
    assert 'href="https://dphish.atlassian.net/browse/SUP-1"' in page


def test_an_old_manual_ticket_can_still_be_closed_after_jira_is_on(client, csm, acme, jira_on):
    ticket = Ticket.objects.create(
        account=acme, ticket_kind=TicketKind.BUG, title="From before Jira",
        opened_on=dt.date(2026, 9, 1), created_by=csm,
    )
    login(client, csm)
    client.post(reverse("engagement:ticket_close", args=[acme.pk, ticket.pk]))
    ticket.refresh_from_db()
    assert ticket.status == TicketStatus.CLOSED


def test_the_integrations_page_is_for_admins_only(client, csm, admin):
    login(client, csm)
    assert client.get(reverse("integrations:status")).status_code == 403
    login(client, admin)
    assert client.get(reverse("integrations:status")).status_code == 200


def test_the_integrations_page_lists_unmatched_organizations(client, admin, acme, jira_on):
    sync.run(FakeJiraClient([issue(orgs=["Initech"])]))
    login(client, admin)
    page = client.get(reverse("integrations:status")).content.decode()
    assert "Initech" in page


def test_sync_now_runs_a_sync(client, admin, acme, jira_on, monkeypatch):
    monkeypatch.setattr(jira_settings, "get_client", lambda: FakeJiraClient([issue()]))
    login(client, admin)
    client.post(reverse("integrations:status"), {"action": "sync"})
    assert jira_tickets().count() == 1


def test_sync_now_reports_a_failure(client, admin, acme, jira_on, monkeypatch):
    broken = FakeJiraClient()
    broken.error = JiraError("Jira answered 401")
    monkeypatch.setattr(jira_settings, "get_client", lambda: broken)
    login(client, admin)
    response = client.post(reverse("integrations:status"), {"action": "sync"}, follow=True)
    assert "Sync failed: Jira answered 401" in response.content.decode()
