"""Jira Cloud over HTTPS, with the standard library and nothing else.

Two endpoints matter:

* ``GET /rest/api/3/field`` once per client, to find the JSM Organizations
  custom field. Its id (``customfield_10002`` on one site, something else on the
  next) differs per site; its schema type does not.
* ``GET /rest/api/3/search/jql``, the paged search that replaced
  ``/rest/api/3/search`` in 2025. It pages by ``nextPageToken``, not offset.

"Updated since" is written as a *relative* JQL date (``-95m``) rather than a
timestamp. JQL reads an absolute date in the timezone of the API user's Jira
profile, which nothing here knows; a relative one means the same thing
everywhere.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator

from csm.core import clock
from csm.integrations.jira.client import JiraError, JiraIssue

ORGANIZATIONS_SCHEMA = "com.atlassian.servicedesk:sd-customer-organizations"
PAGE_SIZE = 100
TIMEOUT_SECONDS = 30

#: (url, headers) -> parsed JSON. Injected by tests; urllib otherwise.
Transport = Callable[[str, dict[str, str]], object]


def _urllib_transport(url: str, headers: dict[str, str]) -> object:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 401/403 are the ones somebody can fix, so they say what to check.
        hint = " (check JIRA_EMAIL and JIRA_API_TOKEN)" if exc.code in (401, 403) else ""
        raise JiraError(f"Jira answered {exc.code} for {url}{hint}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise JiraError(f"Could not reach Jira at {url}: {exc}") from exc
    except ValueError as exc:
        raise JiraError(f"Jira returned something that is not JSON from {url}") from exc


def parse_datetime(value: str | None) -> dt.datetime | None:
    """Jira's ``2026-09-30T10:15:00.000+0300``, as an aware instant."""
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise JiraError(f"Unreadable Jira timestamp {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment


class HttpJiraClient:
    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        project_keys: tuple[str, ...] = (),
        organizations_field: str = "",
        transport: Transport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{email}:{api_token}".encode()).decode("ascii")
        self.headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self.project_keys = project_keys
        self._organizations_field = organizations_field
        self._transport = transport or _urllib_transport

    # -- plumbing -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, str] | None = None) -> object:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._transport(url, self.headers)

    @property
    def organizations_field(self) -> str:
        if not self._organizations_field:
            fields = self._get("/rest/api/3/field")
            if not isinstance(fields, list):
                raise JiraError("Jira's field list was not a list.")
            for entry in fields:
                if (entry.get("schema") or {}).get("custom") == ORGANIZATIONS_SCHEMA:
                    self._organizations_field = entry["id"]
                    break
            else:
                raise JiraError(
                    "This Jira site has no Organizations field. Is Jira Service "
                    "Management installed, and can the API user see it?"
                )
        return self._organizations_field

    def _scope(self) -> str:
        # cf[10002], not "Organizations": a site can rename the field's label,
        # and a custom field's numeric id is the one spelling that cannot drift.
        number = self.organizations_field.removeprefix("customfield_")
        clause = f"cf[{number}] is not EMPTY"
        if self.project_keys:
            clause += " AND project in (" + ", ".join(f'"{k}"' for k in self.project_keys) + ")"
        return clause

    def _search(self, jql: str) -> Iterator[JiraIssue]:
        org_field = self.organizations_field
        fields = ",".join(
            ["summary", "issuetype", "status", "created", "updated", "resolutiondate", org_field]
        )
        token = ""
        while True:
            params = {"jql": jql, "fields": fields, "maxResults": str(PAGE_SIZE)}
            if token:
                params["nextPageToken"] = token
            page = self._get("/rest/api/3/search/jql", params)
            if not isinstance(page, dict):
                raise JiraError("Jira's search response was not an object.")
            for raw in page.get("issues", []):
                yield self._issue(raw, org_field)
            token = page.get("nextPageToken") or ""
            if page.get("isLast", True) or not token:
                return

    def _issue(self, raw: dict, org_field: str) -> JiraIssue:
        try:
            f = raw["fields"]
            status = f.get("status") or {}
            return JiraIssue(
                key=raw["key"],
                issue_type=(f.get("issuetype") or {}).get("name", ""),
                status=status.get("name", ""),
                status_category=(status.get("statusCategory") or {}).get("key", ""),
                summary=f.get("summary") or "",
                created=parse_datetime(f["created"]),
                updated=parse_datetime(f["updated"]),
                resolved=parse_datetime(f.get("resolutiondate")),
                organizations=tuple(
                    org["name"] for org in (f.get(org_field) or []) if org.get("name")
                ),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise JiraError(f"Unexpected issue shape for {raw.get('key', '?')}") from exc

    # -- the connection test ------------------------------------------------

    def check(self) -> dict[str, str]:
        """Who Jira thinks we are, and which field holds the organizations.

        The two things a sync needs that a wrong setting breaks: the
        credentials (``/myself`` refuses bad ones with a 401) and Jira Service
        Management being installed and visible to that user.
        """
        me = self._get("/rest/api/3/myself")
        if not isinstance(me, dict):
            raise JiraError("Jira's answer about the API user was not an object.")
        return {
            "user": me.get("displayName") or me.get("emailAddress") or "unknown",
            "organizations_field": self.organizations_field,
        }

    # -- the JiraClient interface -------------------------------------------

    def issues_updated_since(self, since: dt.datetime) -> Iterator[JiraIssue]:
        minutes = max(1, math.ceil((clock.now() - since).total_seconds() / 60))
        return self._search(f'{self._scope()} AND updated >= "-{minutes}m" ORDER BY updated ASC')

    def open_issues(self) -> Iterator[JiraIssue]:
        return self._search(f"{self._scope()} AND statusCategory != Done ORDER BY key ASC")
