"""Whether Microsoft 365 is connected, read from the integrations page.

One source only: the ``TeamsConnection`` row an administrator saved. Unlike
Jira there is no environment fallback - there was never a deployment that
configured Teams in ``.env``, so there is nothing to stay compatible with.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class TeamsConfig:
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    count_attendance: bool = True
    enabled: bool = False

    @property
    def missing(self) -> list[str]:
        labels = {
            "tenant_id": "tenant ID",
            "client_id": "application (client) ID",
            "client_secret": "client secret",
        }
        return [label for name, label in labels.items() if not getattr(self, name)]

    @property
    def is_configured(self) -> bool:
        return self.enabled and not self.missing


def current() -> TeamsConfig:
    from csm.integrations.models import TeamsConnection

    row = TeamsConnection.objects.first()
    if row is None:
        return TeamsConfig()
    return TeamsConfig(
        tenant_id=row.tenant_id.strip(),
        client_id=row.client_id.strip(),
        client_secret=row.client_secret,
        count_attendance=row.count_attendance,
        enabled=row.enabled,
    )


def is_configured() -> bool:
    return current().is_configured


def client_for(config: TeamsConfig):
    """A live client for ``config``, switched on or not - the Test button uses it."""
    if config.missing:
        raise ImproperlyConfigured(
            "Microsoft 365 is missing its " + ", ".join(config.missing) + "."
        )
    from csm.integrations.teams.http import HttpGraphClient

    return HttpGraphClient(
        tenant_id=config.tenant_id,
        client_id=config.client_id,
        client_secret=config.client_secret,
    )


def get_client():
    config = current()
    if not config.enabled:
        raise ImproperlyConfigured("Microsoft 365 is switched off on the integrations page.")
    return client_for(config)
