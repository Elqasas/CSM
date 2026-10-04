"""Where the Jira connection comes from, and whether it is switched on.

Two sources, one winner. Once an administrator has saved the form on
``/integrations/``, that row is the connection and the ``JIRA_*`` environment
variables are ignored. Until then the environment is read, so a deployment that
configured Jira in ``.env`` keeps working. Two sources merged field by field
would make "which token is it using?" unanswerable, so they never mix.

Jira counts as configured only when the site, the account email and the API
token are all present - and, for the page, when "use Jira" is ticked, so the
connection can be entered and tested before manual ticket entry is withdrawn.
In the environment, two of three is a mistake rather than a choice, so it is
refused by ``get_client()`` instead of quietly read as "off".
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

SOURCE_PAGE = "page"
SOURCE_ENVIRONMENT = "environment"


@dataclass(frozen=True)
class JiraConfig:
    base_url: str = ""
    email: str = ""
    api_token: str = ""
    project_keys: tuple[str, ...] = ()
    bug_issue_types: tuple[str, ...] = ("Bug",)
    organizations_field: str = ""
    backfill_days: int = 90
    enabled: bool = False
    source: str = SOURCE_ENVIRONMENT

    @property
    def missing(self) -> list[str]:
        labels = {"base_url": "site URL", "email": "account email", "api_token": "API token"}
        return [label for name, label in labels.items() if not getattr(self, name)]

    @property
    def is_configured(self) -> bool:
        return self.enabled and not self.missing


def split_list(text: str) -> tuple[str, ...]:
    """``"SUP, ops ,"`` -> ``("SUP", "ops")``. Commas or new lines."""
    return tuple(part.strip() for part in text.replace("\n", ",").split(",") if part.strip())


def _from_environment() -> JiraConfig:
    def value(name: str) -> str:
        return (getattr(settings, name, "") or "").strip()

    base_url, email, token = value("JIRA_BASE_URL"), value("JIRA_EMAIL"), value("JIRA_API_TOKEN")
    return JiraConfig(
        base_url=base_url,
        email=email,
        api_token=token,
        project_keys=tuple(settings.JIRA_PROJECT_KEYS),
        bug_issue_types=tuple(settings.JIRA_BUG_ISSUE_TYPES),
        organizations_field=settings.JIRA_ORGANIZATIONS_FIELD,
        backfill_days=settings.JIRA_BACKFILL_DAYS,
        # In the environment, setting all three *is* switching it on.
        enabled=bool(base_url and email and token),
        source=SOURCE_ENVIRONMENT,
    )


def current() -> JiraConfig:
    """The connection in force right now."""
    from csm.integrations.models import JiraConnection

    row = JiraConnection.objects.first()
    if row is None:
        return _from_environment()
    return JiraConfig(
        base_url=row.base_url.strip(),
        email=row.email.strip(),
        api_token=row.api_token,
        project_keys=split_list(row.project_keys),
        bug_issue_types=split_list(row.bug_issue_types) or ("Bug",),
        organizations_field=row.organizations_field.strip(),
        backfill_days=row.backfill_days,
        enabled=row.enabled,
        source=SOURCE_PAGE,
    )


def is_configured() -> bool:
    """Jira is the source of tickets. Manual ticket entry is withdrawn."""
    return current().is_configured


def browse_base(config: JiraConfig | None = None) -> str:
    """Prefix of an issue's page: ``{{ jira_browse_base }}/SUP-12``. Empty when unset."""
    base = (config or current()).base_url.rstrip("/")
    return f"{base}/browse" if base else ""


def client_for(config: JiraConfig):
    """A live client for ``config``, switched on or not - the Test button uses it."""
    if config.missing:
        raise ImproperlyConfigured("Jira is missing its " + ", ".join(config.missing) + ".")

    from csm.integrations.jira.http import HttpJiraClient

    return HttpJiraClient(
        base_url=config.base_url,
        email=config.email,
        api_token=config.api_token,
        project_keys=config.project_keys,
        organizations_field=config.organizations_field,
    )


def get_client():
    """The live client. Raises when Jira is missing, half-configured or off."""
    config = current()
    if config.source == SOURCE_PAGE and not config.enabled:
        raise ImproperlyConfigured("Jira is switched off on the integrations page.")
    return client_for(config)
