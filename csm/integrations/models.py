"""What the integrations remember between runs.

One row per provider. It holds the cursor a polling sync resumes from and the
outcome of the last run, including the organizations that matched no account -
which is the one thing an administrator has to act on, so it is kept rather
than only logged.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from csm.core.enums import IntegrationProvider


class SyncState(models.Model):
    provider = models.CharField(
        max_length=16, choices=IntegrationProvider.choices, unique=True
    )

    #: Where the next incremental run resumes. Advanced only by a run that
    #: finished, so a failed run is retried from the same place rather than
    #: skipping whatever it failed to read.
    cursor = models.DateTimeField(null=True, blank=True)
    last_full_sweep_at = models.DateTimeField(null=True, blank=True)

    last_started_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    #: The last successful run's report: counts, plus the organization names
    #: that matched no account or more than one.
    last_report = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "integration_sync_state"

    def __str__(self) -> str:
        return f"{self.get_provider_display()} sync"


class JiraConnection(models.Model):
    """The Jira site this platform reads, as an administrator entered it.

    One row. Saving it does not switch anything on: ``enabled`` does, so the
    connection can be entered and tested before manual ticket entry is
    withdrawn. The API token is stored encrypted (``integrations/crypto.py``)
    and never rendered back to the page.
    """

    enabled = models.BooleanField(
        default=False,
        help_text="Tickets come from Jira and manual ticket entry is withdrawn.",
    )
    base_url = models.URLField(blank=True, default="")
    email = models.EmailField(blank=True, default="")
    api_token_encrypted = models.TextField(blank=True, default="")
    project_keys = models.CharField(max_length=255, blank=True, default="")
    bug_issue_types = models.CharField(max_length=255, blank=True, default="Bug")
    organizations_field = models.CharField(max_length=64, blank=True, default="")
    backfill_days = models.PositiveSmallIntegerField(default=90)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        db_table = "integration_jira_connection"

    def __str__(self) -> str:
        return self.base_url or "Jira (not set)"

    @property
    def api_token(self) -> str:
        from csm.integrations.crypto import decrypt

        return decrypt(self.api_token_encrypted)

    @api_token.setter
    def api_token(self, value: str) -> None:
        from csm.integrations.crypto import encrypt

        self.api_token_encrypted = encrypt(value) if value else ""

    @property
    def has_token(self) -> bool:
        return bool(self.api_token_encrypted)
