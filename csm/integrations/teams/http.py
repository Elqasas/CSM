"""Microsoft Graph over HTTPS, with the standard library and nothing else.

App-only: the client-credentials grant against the tenant's token endpoint, then
``/users/{mailbox}/...`` for each platform user. The Entra ID app registration
needs these *application* permissions, admin-consented:

* ``Calendars.Read`` - ``calendarView``, every occurrence in a window, recurring
  meetings already expanded;
* ``User.Read.All`` - an organizer's object id, which the online-meeting
  endpoints want instead of an address;
* ``OnlineMeetings.Read.All`` and ``OnlineMeetingArtifact.Read.All`` - finding a
  Teams meeting by its join URL and reading its attendance report. These two
  also need a Teams *application access policy* granted to the app
  (``New-CsApplicationAccessPolicy`` / ``Grant-CsApplicationAccessPolicy``);
  without one Graph answers 403 and attendance is simply never counted.

Times are asked for in UTC (``Prefer: outlook.timezone="UTC"``) so nothing here
has to know a mailbox's own timezone.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator

from csm.core import clock
from csm.integrations.teams.client import (
    Attendee,
    GraphError,
    GraphEvent,
    GraphForbidden,
    GraphNotFound,
)

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
PAGE_SIZE = 100
TIMEOUT_SECONDS = 30

EVENT_FIELDS = ",".join([
    "id", "iCalUId", "subject", "start", "end", "isAllDay", "isCancelled",
    "sensitivity", "isOnlineMeeting", "onlineMeetingProvider", "onlineMeeting",
    "webLink", "location", "organizer", "attendees", "responseStatus",
])

#: Outlook sensitivities that keep the subject to its owner.
PRIVATE = {"private", "confidential"}

#: (method, url, headers, body) -> parsed JSON. Injected by tests; urllib otherwise.
Transport = Callable[[str, str, dict[str, str], bytes | None], object]


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> object:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code == 404:
            raise GraphNotFound(f"Not found: {url}") from exc
        if exc.code == 403:
            raise GraphForbidden(f"Microsoft refused access to {url}{detail}") from exc
        hint = " (check the tenant ID, client ID and secret)" if exc.code in (400, 401) else ""
        raise GraphError(f"Microsoft answered {exc.code} for {url}{hint}{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GraphError(f"Could not reach Microsoft at {url}: {exc}") from exc
    except ValueError as exc:
        raise GraphError(f"Microsoft returned something that is not JSON from {url}") from exc


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Graph and the token endpoint both explain themselves; keep the explanation."""
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (ValueError, OSError):
        return ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f": {error['message']}"
        if payload.get("error_description"):
            return ": " + str(payload["error_description"]).splitlines()[0]
    return ""


def parse_graph_time(value: dict | None) -> dt.datetime:
    """``{"dateTime": "2026-10-05T09:00:00.0000000", "timeZone": "UTC"}`` -> aware UTC.

    Graph writes seven fractional digits; Python reads at most six.
    """
    if not value or not value.get("dateTime"):
        raise GraphError("An event arrived without a start or end time.")
    text = value["dateTime"]
    if "." in text:
        head, fraction = text.split(".", 1)
        text = f"{head}.{fraction[:6]}"
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise GraphError(f"Unreadable Graph time {value['dateTime']!r}") from exc
    return moment.replace(tzinfo=dt.UTC) if moment.tzinfo is None else moment.astimezone(dt.UTC)


def parse_event(raw: dict) -> GraphEvent:
    try:
        private = (raw.get("sensitivity") or "").lower() in PRIVATE
        organizer = (raw.get("organizer") or {}).get("emailAddress") or {}
        attendees = tuple(
            Attendee(
                email=(a.get("emailAddress") or {}).get("address", "").strip().lower(),
                name=(a.get("emailAddress") or {}).get("name", "") or "",
                response=(a.get("status") or {}).get("response", "") or "",
            )
            for a in raw.get("attendees") or []
            if (a.get("emailAddress") or {}).get("address")
        )
        teams = bool(raw.get("isOnlineMeeting")) and raw.get("onlineMeetingProvider") == "teamsForBusiness"
        return GraphEvent(
            event_id=raw["id"],
            ical_uid=raw.get("iCalUId") or raw["id"],
            subject="" if private else raw.get("subject") or "",
            starts_at=parse_graph_time(raw.get("start")),
            ends_at=parse_graph_time(raw.get("end")),
            is_all_day=bool(raw.get("isAllDay")),
            is_cancelled=bool(raw.get("isCancelled")),
            is_private=private,
            is_teams=teams,
            join_url=((raw.get("onlineMeeting") or {}).get("joinUrl") or "") if teams else "",
            web_link=raw.get("webLink") or "",
            location="" if private else (raw.get("location") or {}).get("displayName", "") or "",
            organizer_email=(organizer.get("address") or "").strip().lower(),
            organizer_name=organizer.get("name") or "",
            response=(raw.get("responseStatus") or {}).get("response", "") or "",
            attendees=attendees,
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise GraphError(f"Unexpected event shape for {raw.get('id', '?')}") from exc


class HttpGraphClient:
    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        transport: Transport | None = None,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._transport = transport or _urllib_transport
        self._token = ""
        self._token_expires = 0.0
        self._user_ids: dict[str, str] = {}

    # -- plumbing -----------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires:
            return self._token
        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode("ascii")
        url = f"{LOGIN}/{urllib.parse.quote(self.tenant_id, safe='')}/oauth2/v2.0/token"
        answer = self._transport(
            "POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, body
        )
        if not isinstance(answer, dict) or not answer.get("access_token"):
            raise GraphError("Microsoft issued no access token. Check the tenant ID, client ID and secret.")
        self._token = answer["access_token"]
        # A minute early, so a token never expires halfway through a page.
        self._token_expires = time.monotonic() + max(int(answer.get("expires_in", 3600)) - 60, 0)
        return self._token

    def _get(self, url: str, *, prefer_utc: bool = False) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}
        if prefer_utc:
            headers["Prefer"] = 'outlook.timezone="UTC"'
        page = self._transport("GET", url, headers, None)
        if not isinstance(page, dict):
            raise GraphError(f"Graph's answer from {url} was not an object.")
        return page

    def _pages(self, url: str, *, prefer_utc: bool = False) -> Iterator[dict]:
        while url:
            page = self._get(url, prefer_utc=prefer_utc)
            yield from page.get("value") or []
            url = page.get("@odata.nextLink") or ""

    @staticmethod
    def _user(address: str) -> str:
        return f"{GRAPH}/users/{urllib.parse.quote(address, safe='@')}"

    # -- the GraphClient interface ------------------------------------------

    def calendar_view(self, mailbox, start, end) -> list[GraphEvent]:
        params = urllib.parse.urlencode({
            "startDateTime": start.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$select": EVENT_FIELDS,
            "$top": str(PAGE_SIZE),
        })
        url = f"{self._user(mailbox)}/calendarView?{params}"
        return [parse_event(raw) for raw in self._pages(url, prefer_utc=True)]

    def user_id(self, address: str) -> str:
        if address not in self._user_ids:
            me = self._get(f"{self._user(address)}?$select=id")
            if not me.get("id"):
                raise GraphError(f"Graph returned no id for {address}")
            self._user_ids[address] = me["id"]
        return self._user_ids[address]

    def attendance(self, organizer, join_url) -> list[str] | None:
        try:
            base = f"{GRAPH}/users/{self.user_id(organizer)}/onlineMeetings"
            escaped = join_url.replace("'", "''")
            query = urllib.parse.urlencode({"$filter": f"JoinWebUrl eq '{escaped}'"})
            found = list(self._pages(f"{base}?{query}"))
            if not found:
                return None
            meeting_url = f"{base}/{urllib.parse.quote(found[0]['id'], safe='')}"
            reports = list(self._pages(f"{meeting_url}/attendanceReports"))
        except GraphNotFound:
            # The organizer is outside this tenant, or the meeting is gone.
            return None
        if not reports:
            return None
        emails: set[str] = set()
        for report in reports:
            records_url = (
                f"{meeting_url}/attendanceReports/"
                f"{urllib.parse.quote(report['id'], safe='')}/attendanceRecords"
            )
            for record in self._pages(records_url):
                address = (record.get("emailAddress") or "").strip().lower()
                if address:
                    emails.add(address)
        return sorted(emails)

    # -- the connection test ------------------------------------------------

    def check(self, mailbox: str) -> int:
        """Sign in, then read today in ``mailbox``. Returns how many events it holds."""
        now = clock.now()
        return len(self.calendar_view(mailbox, now - dt.timedelta(hours=12), now + dt.timedelta(hours=12)))
