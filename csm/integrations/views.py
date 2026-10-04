"""The integrations page: connect Jira, test it, and see what the sync missed.

Admin-only, through ``integration.manage``. Three buttons share one form:

* **Save** stores the connection. Ticking "use Jira" is what withdraws manual
  ticket entry, and the form refuses it without a site, email and token.
* **Test** tries what is in the form *without saving it*, so a wrong token is
  found before it is the one in force.
* **Sync now** runs an incremental sync with the saved connection.

The list worth reading is the organizations that matched no account: each is a
client whose tickets are not reaching their health score, and the fix is a
rename on one side or the other.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured
from django.shortcuts import redirect
from django.views.generic import TemplateView

from csm.core.access import capabilities as cap
from csm.core.enums import IntegrationProvider, TicketSource
from csm.core.views import CapabilityRequiredMixin
from csm.engagement.models import Ticket
from csm.integrations.forms import JiraConnectionForm
from csm.integrations.jira import settings as jira
from csm.integrations.jira.client import JiraError
from csm.integrations.jira.sync import run
from csm.integrations.models import JiraConnection, SyncState


def jira_context(request):
    """Context processor: whether tickets come from Jira, and how to link one."""
    config = jira.current()
    return {"jira_enabled": config.is_configured, "jira_browse_base": jira.browse_base(config)}


class IntegrationStatusView(CapabilityRequiredMixin, TemplateView):
    required_capability = cap.INTEGRATION_MANAGE
    template_name = "integrations/status.html"

    def _form(self, data=None) -> JiraConnectionForm:
        connection = JiraConnection.objects.first()
        if connection is not None:
            return JiraConnectionForm(data, instance=connection)
        # Nothing saved yet: start from whatever the environment holds, so
        # moving a .env setup onto the page is a matter of pressing Save (and
        # typing the token, which is never shown).
        env = jira.current()
        initial = {
            "base_url": env.base_url,
            "email": env.email,
            "project_keys": ", ".join(env.project_keys),
            "bug_issue_types": ", ".join(env.bug_issue_types),
            "organizations_field": env.organizations_field,
            "backfill_days": env.backfill_days,
            "enabled": env.enabled,
        }
        return JiraConnectionForm(data, initial=initial)

    def get_context_data(self, form=None, **kwargs):
        context = super().get_context_data(**kwargs)
        config = jira.current()
        connection = JiraConnection.objects.first()
        context["nav"] = "settings"
        context["form"] = form or self._form()
        context["config"] = config
        context["configured"] = config.is_configured
        context["connection"] = connection
        context["has_token"] = bool(connection and connection.has_token)
        context["state"] = SyncState.objects.filter(provider=IntegrationProvider.JIRA).first()
        context["synced_tickets"] = Ticket.objects.filter(source=TicketSource.JIRA).count()
        context["open_synced_tickets"] = (
            Ticket.objects.open().filter(source=TicketSource.JIRA).count()
        )
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action", "save")
        if action == "sync":
            return self._sync(request)
        if action == "forget_token":
            connection = JiraConnection.objects.first()
            if connection is not None:
                connection.api_token = ""
                connection.enabled = False
                connection.updated_by = request.user
                connection.save()
                messages.success(request, "Token removed. Jira is switched off.")
            return redirect("integrations:status")

        form = self._form(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        if action == "test":
            self._test(request, form)
            return self.render_to_response(self.get_context_data(form=form))

        form.save(user=request.user)
        if form.cleaned_data["enabled"]:
            messages.success(
                request, "Saved. Jira is on: tickets now come from Jira and sync every 15 minutes."
            )
        else:
            messages.success(request, "Saved. Jira is off; tickets are still entered by hand.")
        return redirect("integrations:status")

    def _test(self, request, form) -> None:
        try:
            result = jira.client_for(form.config()).check()
        except ImproperlyConfigured as exc:
            messages.error(request, str(exc))
        except JiraError as exc:
            messages.error(request, f"Test failed: {exc}")
        else:
            messages.success(
                request,
                f"Connected as {result['user']}. Organizations are in "
                f"{result['organizations_field']}. Nothing has been saved yet.",
            )

    def _sync(self, request):
        """Incremental, so it is quick; ``--full`` stays a command."""
        if not jira.is_configured():
            messages.error(request, "Jira is not switched on.")
            return redirect("integrations:status")
        try:
            report = run(jira.get_client())
        except (JiraError, ImproperlyConfigured) as exc:
            messages.error(request, f"Sync failed: {exc}")
        else:
            messages.success(
                request,
                f"Synced {report['read']} issues: {report['created']} new, "
                f"{report['updated']} changed.",
            )
        return redirect("integrations:status")
