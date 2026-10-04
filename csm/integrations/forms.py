"""The Jira connection, as an administrator enters it.

Two values end up inside JQL - the project keys and the Organizations field id -
so both are held to Jira's own formats here rather than quoted and hoped for.
The token is write-only: the field is never filled back in, and leaving it
blank on a later save keeps the one already stored.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from django import forms

from csm.integrations.jira.settings import SOURCE_PAGE, JiraConfig, split_list
from csm.integrations.models import JiraConnection, TeamsConnection
from csm.integrations.teams.settings import TeamsConfig

PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")
CUSTOM_FIELD = re.compile(r"^customfield_\d+$")


class JiraConnectionForm(forms.ModelForm):
    base_url = forms.URLField(
        label="Jira site",
        required=False,
        assume_scheme="https",
        help_text="Your Atlassian site, e.g. https://dphish.atlassian.net",
    )
    email = forms.EmailField(
        label="Account email",
        required=False,
        help_text="The Atlassian account the platform signs in as. It needs to see the service desk.",
    )
    api_token = forms.CharField(
        label="API token",
        required=False,
        strip=True,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text=(
            "Create one at id.atlassian.com/manage-profile/security/api-tokens "
            "while signed in as that account."
        ),
    )
    project_keys = forms.CharField(
        label="Projects",
        required=False,
        help_text="Service desk project keys, comma-separated, e.g. SUP. Blank reads every project.",
    )
    bug_issue_types = forms.CharField(
        label="Issue types that count as Bug",
        required=False,
        help_text="These score three in Open Tickets; every other type scores one.",
    )
    organizations_field = forms.CharField(
        label="Organizations field id",
        required=False,
        help_text="Leave blank: it is found automatically. Only set it if Test picks the wrong one.",
    )
    backfill_days = forms.IntegerField(
        label="First sync reads closed issues from the last",
        min_value=1,
        max_value=3650,
        help_text="Days. Open issues are read whatever their age.",
    )
    enabled = forms.BooleanField(
        label="Use Jira for tickets",
        required=False,
        help_text=(
            "Tickets then come only from Jira, and the Add ticket option is "
            "withdrawn. Test the connection before ticking this."
        ),
    )

    class Meta:
        model = JiraConnection
        fields = [
            "base_url",
            "email",
            "api_token",
            "project_keys",
            "bug_issue_types",
            "organizations_field",
            "backfill_days",
            "enabled",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = "form-check-input" if isinstance(field, forms.BooleanField) else "form-control"
            field.widget.attrs.setdefault("class", css)

    def clean_base_url(self) -> str:
        value = self.cleaned_data["base_url"]
        if not value:
            return ""
        parts = urlsplit(value)
        if parts.scheme != "https":
            raise forms.ValidationError("Jira Cloud is served over https.")
        # The site, not a page on it: a pasted board or issue link would
        # otherwise become the prefix of every API call.
        return f"https://{parts.netloc}"

    def clean_project_keys(self) -> str:
        keys = [key.upper() for key in split_list(self.cleaned_data["project_keys"])]
        bad = [key for key in keys if not PROJECT_KEY.match(key)]
        if bad:
            raise forms.ValidationError(
                "Not a project key: " + ", ".join(bad) + ". Keys are letters and digits, e.g. SUP."
            )
        return ", ".join(dict.fromkeys(keys))

    def clean_bug_issue_types(self) -> str:
        return ", ".join(split_list(self.cleaned_data["bug_issue_types"])) or "Bug"

    def clean_organizations_field(self) -> str:
        value = self.cleaned_data["organizations_field"].strip()
        if value and not CUSTOM_FIELD.match(value):
            raise forms.ValidationError("A custom field id looks like customfield_10002.")
        return value

    def token(self) -> str:
        """The token to use: the one just typed, else the one already stored."""
        return self.cleaned_data.get("api_token") or self.instance.api_token

    def clean(self):
        cleaned = super().clean()
        config = self.config()
        if cleaned.get("enabled") and config.missing:
            raise forms.ValidationError(
                "Jira can only be switched on with a site, an account email and an "
                "API token. Missing: " + ", ".join(config.missing) + "."
            )
        return cleaned

    def config(self) -> JiraConfig:
        """What the form describes, saved or not - so Test can try it first."""
        data = self.cleaned_data
        return JiraConfig(
            base_url=data.get("base_url", ""),
            email=data.get("email", ""),
            api_token=self.token(),
            project_keys=split_list(data.get("project_keys", "")),
            bug_issue_types=split_list(data.get("bug_issue_types", "")) or ("Bug",),
            organizations_field=data.get("organizations_field", ""),
            backfill_days=data.get("backfill_days") or 90,
            enabled=bool(data.get("enabled")),
            source=SOURCE_PAGE,
        )

    def save(self, commit=True, *, user=None):
        connection = super().save(commit=False)
        typed = self.cleaned_data.get("api_token")
        if typed:
            connection.api_token = typed
        connection.updated_by = user
        if commit:
            connection.save()
        return connection


GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
TENANT_DOMAIN = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


class TeamsConnectionForm(forms.ModelForm):
    """The Entra ID app registration, as an administrator enters it.

    The tenant reaches a URL and the client id a token request, so both are
    held to Microsoft's formats. The secret is write-only, like Jira's token.
    """

    tenant_id = forms.CharField(
        label="Directory (tenant) ID",
        required=False,
        help_text="From the app registration's Overview page, or the tenant's primary domain.",
    )
    client_id = forms.CharField(
        label="Application (client) ID",
        required=False,
        help_text="From the same Overview page.",
    )
    client_secret = forms.CharField(
        label="Client secret",
        required=False,
        strip=True,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="The secret's Value (not its ID), from Certificates & secrets.",
    )
    count_attendance = forms.BooleanField(
        label="Count attended Teams meetings towards health",
        required=False,
        help_text=(
            "When a Teams meeting's attendance report shows a client contact - "
            "matched by the email stored on the contact - joined, log a Meeting "
            "activity. Invited but absent never counts."
        ),
    )
    enabled = forms.BooleanField(
        label="Read Outlook calendars",
        required=False,
        help_text="Each person's meetings then appear on their own calendar, every 15 minutes.",
    )

    class Meta:
        model = TeamsConnection
        fields = ["tenant_id", "client_id", "client_secret", "count_attendance", "enabled"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = "form-check-input" if isinstance(field, forms.BooleanField) else "form-control"
            field.widget.attrs.setdefault("class", css)

    def clean_tenant_id(self) -> str:
        value = self.cleaned_data["tenant_id"].strip()
        if value and not (GUID.match(value) or TENANT_DOMAIN.match(value)):
            raise forms.ValidationError(
                "A tenant ID looks like 72f988bf-86f1-41af-91ab-2d7cd011db47, or a domain such as dphish.com."
            )
        return value

    def clean_client_id(self) -> str:
        value = self.cleaned_data["client_id"].strip()
        if value and not GUID.match(value):
            raise forms.ValidationError("A client ID looks like 72f988bf-86f1-41af-91ab-2d7cd011db47.")
        return value

    def secret(self) -> str:
        return self.cleaned_data.get("client_secret") or self.instance.client_secret

    def clean(self):
        cleaned = super().clean()
        config = self.config()
        if cleaned.get("enabled") and config.missing:
            raise forms.ValidationError(
                "Microsoft 365 can only be switched on with a tenant ID, a client ID "
                "and a secret. Missing: " + ", ".join(config.missing) + "."
            )
        return cleaned

    def config(self) -> TeamsConfig:
        data = self.cleaned_data
        return TeamsConfig(
            tenant_id=data.get("tenant_id", ""),
            client_id=data.get("client_id", ""),
            client_secret=self.secret(),
            count_attendance=bool(data.get("count_attendance")),
            enabled=bool(data.get("enabled")),
        )

    def save(self, commit=True, *, user=None):
        connection = super().save(commit=False)
        typed = self.cleaned_data.get("client_secret")
        if typed:
            connection.client_secret = typed
        connection.updated_by = user
        if commit:
            connection.save()
        return connection
