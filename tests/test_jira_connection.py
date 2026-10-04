"""The Jira connection, entered on the integrations page.

What matters: the token never sits readable in the database and never comes
back to the page; a blank token field keeps the stored one; Jira cannot be
switched on half-configured; whatever reaches JQL is held to Jira's formats;
Test tries the form without saving it; and once saved, the page - not .env - is
the connection in force.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from csm.integrations import crypto
from csm.integrations.forms import JiraConnectionForm
from csm.integrations.jira import settings as jira
from csm.integrations.jira.client import JiraError
from csm.integrations.models import JiraConnection
from tests.factories import UserFactory
from tests.test_views import PASSWORD, login

SECRET = "ATATT3xFfGF0-very-secret-token"


def form_data(**overrides):
    data = {
        "base_url": "https://dphish.atlassian.net",
        "email": "bot@dphish.com",
        "api_token": SECRET,
        "project_keys": "SUP",
        "bug_issue_types": "Bug",
        "organizations_field": "",
        "backfill_days": 90,
        "enabled": "on",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


@pytest.fixture
def admin(db):
    user = UserFactory(admin=True, email="conn-admin@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    return user


def save(client, **overrides):
    return client.post(reverse("integrations:status"), form_data(action="save", **overrides))


# ---------------------------------------------------------------------------
# The secret
# ---------------------------------------------------------------------------


def test_encryption_round_trips_and_a_rotated_key_reads_as_empty(settings):
    stored = crypto.encrypt(SECRET)
    assert SECRET not in stored
    assert crypto.decrypt(stored) == SECRET

    settings.SECRET_KEY = "a-different-key-" * 4
    assert crypto.decrypt(stored) == ""


def test_the_token_is_stored_encrypted_and_never_rendered(client, admin):
    login(client, admin)
    save(client)

    row = JiraConnection.objects.get()
    assert SECRET not in row.api_token_encrypted
    assert row.api_token == SECRET
    assert row.updated_by == admin

    page = client.get(reverse("integrations:status")).content.decode()
    assert SECRET not in page
    assert "A token is stored" in page


def test_a_blank_token_keeps_the_stored_one(client, admin):
    login(client, admin)
    save(client)
    save(client, api_token="", project_keys="SUP, OPS")

    row = JiraConnection.objects.get()
    assert row.api_token == SECRET
    assert row.project_keys == "SUP, OPS"


def test_removing_the_token_switches_jira_off(client, admin):
    login(client, admin)
    save(client)
    client.post(reverse("integrations:status"), {"action": "forget_token"})

    row = JiraConnection.objects.get()
    assert not row.has_token
    assert not row.enabled
    assert not jira.is_configured()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_jira_cannot_be_switched_on_without_a_token(db):
    form = JiraConnectionForm(form_data(api_token=""))
    assert not form.is_valid()
    assert "API token" in str(form.non_field_errors())


def test_it_can_be_saved_off_while_incomplete(db):
    form = JiraConnectionForm(form_data(api_token="", enabled=None))
    assert form.is_valid(), form.errors


@pytest.mark.parametrize(
    "typed, stored",
    [("dphish.atlassian.net", "https://dphish.atlassian.net"),
     ("https://dphish.atlassian.net/jira/servicedesk/projects/SUP", "https://dphish.atlassian.net")],
)
def test_the_site_is_reduced_to_its_origin(db, typed, stored):
    form = JiraConnectionForm(form_data(base_url=typed))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["base_url"] == stored


def test_a_plain_http_site_is_refused(db):
    assert "base_url" in JiraConnectionForm(form_data(base_url="http://jira.local")).errors


@pytest.mark.parametrize("keys", ['SUP") OR project in ("X', "sup-1", "1ABC"])
def test_project_keys_are_held_to_jiras_format(db, keys):
    # They are written into JQL; anything else would be a query of our own.
    assert "project_keys" in JiraConnectionForm(form_data(project_keys=keys)).errors


def test_project_keys_are_upper_cased_and_deduplicated(db):
    form = JiraConnectionForm(form_data(project_keys="sup, ops\nSUP"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["project_keys"] == "SUP, OPS"


def test_the_organizations_field_must_look_like_a_custom_field(db):
    assert "organizations_field" in JiraConnectionForm(
        form_data(organizations_field="Organizations")
    ).errors
    assert JiraConnectionForm(form_data(organizations_field="customfield_10002")).is_valid()


# ---------------------------------------------------------------------------
# Which connection is in force
# ---------------------------------------------------------------------------


def test_the_saved_page_overrides_the_environment(settings, client, admin):
    settings.JIRA_BASE_URL = "https://from-env.atlassian.net"
    settings.JIRA_EMAIL = "env@dphish.com"
    settings.JIRA_API_TOKEN = "env-token"
    assert jira.current().source == jira.SOURCE_ENVIRONMENT
    assert jira.is_configured()

    login(client, admin)
    save(client, enabled=None)

    config = jira.current()
    assert config.source == jira.SOURCE_PAGE
    assert config.base_url == "https://dphish.atlassian.net"
    assert not jira.is_configured()
    with pytest.raises(Exception, match="switched off"):
        jira.get_client()


def test_the_unsaved_page_starts_from_the_environment(settings, client, admin):
    settings.JIRA_BASE_URL = "https://from-env.atlassian.net"
    settings.JIRA_EMAIL = "env@dphish.com"
    settings.JIRA_API_TOKEN = "env-token"
    login(client, admin)
    page = client.get(reverse("integrations:status")).content.decode()
    assert 'value="https://from-env.atlassian.net"' in page
    assert "env-token" not in page


def test_switching_on_withdraws_manual_ticket_entry(client, admin):
    from tests.factories import AccountFactory

    account = AccountFactory()
    login(client, admin)
    assert client.get(reverse("engagement:ticket_create", args=[account.pk])).status_code == 200
    save(client)
    assert client.get(reverse("engagement:ticket_create", args=[account.pk])).status_code == 404


def test_the_saved_bug_types_reach_the_sync(client, admin):
    login(client, admin)
    save(client, bug_issue_types="Bug, Incident")
    assert jira.current().bug_issue_types == ("Bug", "Incident")


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------


class _CheckingClient:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def check(self):
        if self.error:
            raise self.error
        return self.result


def test_test_tries_the_form_without_saving_it(client, admin, monkeypatch):
    seen = {}

    def fake_client_for(config):
        seen["config"] = config
        return _CheckingClient({"user": "CSM Bot", "organizations_field": "customfield_10002"})

    monkeypatch.setattr(jira, "client_for", fake_client_for)
    login(client, admin)
    response = client.post(reverse("integrations:status"), form_data(action="test"), follow=True)

    body = response.content.decode()
    assert "Connected as CSM Bot" in body
    assert "customfield_10002" in body
    assert seen["config"].api_token == SECRET
    assert not JiraConnection.objects.exists()


def test_test_reports_a_refused_token(client, admin, monkeypatch):
    monkeypatch.setattr(
        jira, "client_for",
        lambda config: _CheckingClient(error=JiraError("Jira answered 401 (check the token)")),
    )
    login(client, admin)
    body = client.post(reverse("integrations:status"), form_data(action="test")).content.decode()
    assert "Test failed: Jira answered 401" in body


def test_test_uses_the_stored_token_when_the_field_is_blank(client, admin, monkeypatch):
    login(client, admin)
    save(client)
    seen = {}
    monkeypatch.setattr(
        jira, "client_for",
        lambda config: seen.setdefault("c", config) and _CheckingClient(
            {"user": "x", "organizations_field": "customfield_1"}
        ),
    )
    client.post(reverse("integrations:status"), form_data(action="test", api_token=""))
    assert seen["c"].api_token == SECRET


def test_the_http_check_asks_who_we_are_and_finds_the_field():
    from csm.integrations.jira.http import HttpJiraClient

    def transport(url, headers):
        if url.endswith("/myself"):
            return {"displayName": "CSM Bot"}
        return [{"id": "customfield_10002",
                 "schema": {"custom": "com.atlassian.servicedesk:sd-customer-organizations"}}]

    client = HttpJiraClient(base_url="https://x.atlassian.net", email="a", api_token="b",
                            transport=transport)
    assert client.check() == {"user": "CSM Bot", "organizations_field": "customfield_10002"}


def test_the_page_is_for_admins_only(client, db):
    csm = UserFactory(csm=True, email="conn-csm@dphish.com")
    csm.set_password(PASSWORD)
    csm.save(update_fields=["password"])
    login(client, csm)
    assert save(client).status_code == 403
    assert not JiraConnection.objects.exists()
