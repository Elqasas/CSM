"""The health panel, its history, and the manual override.

Every view here requires ``health.view``, so an ACM is refused at the door
rather than served an empty panel. The account itself still comes from
``ability.accounts()``: holding the capability says nothing about reach.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.views.generic import FormView, TemplateView

from csm.core.access import capabilities as cap
from csm.core.access.presenters import AccountPresenter
from csm.core.clock import today
from csm.core.enums import FactorId
from csm.core.views import AccountScopedMixin
from csm.health.forms import OverrideForm
from csm.health.models import HealthScoreHistory
from csm.health.services import OverrideNotAllowed, active_override, apply_override, clear_override


class HealthPanelView(AccountScopedMixin, TemplateView):
    """The score, its factor breakdown, and why it is what it is."""

    required_capability = cap.HEALTH_VIEW
    template_name = "health/health_panel.html"
    account_url_kwarg = "account_pk"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account = self.account
        latest = (
            HealthScoreHistory.objects.filter(account=account)
            .order_by("-calculated_at")
            .first()
        )
        context["nav"] = "accounts"
        context["presented"] = AccountPresenter(account, self.ability)
        context["latest"] = latest
        context["factors"] = self._factors(latest)
        context["capped_by_red_factors"] = bool(
            (latest.breakdown or {}).get("capped_by_red_factors")
        ) if latest else False
        context["override"] = active_override(account)
        context["can_override"] = self.ability.can_override_health(account)
        # The three inputs the engine reads, shown beside the factors they
        # move. Read here rather than linked to, because "why is this yellow"
        # and "what are the open tickets" are one question.
        context["tickets"] = account.tickets.order_by("status", "-opened_on")[:30]
        context["open_tickets"] = account.tickets.open().count()
        context["csat"] = account.csat_responses.order_by("-recorded_on")[:20]
        context["adoption"] = account.adoption_events.order_by("-occurred_on")[:20]
        context["ticket_weight"] = sum(
            3 if ticket.ticket_kind == "BUG" else 1
            for ticket in account.tickets.open()
        )
        context["today"] = today()
        context["history"] = (
            HealthScoreHistory.objects.filter(account=account)
            .order_by("-calculated_at")[:30]
        )
        return context

    @staticmethod
    def _factors(latest) -> list:
        """Flatten the stored breakdown into rows a template can loop over.

        Reads the persisted blob rather than recomputing. The blob records the
        engine version that produced it, so an old row stays readable after a
        threshold changes, which a recomputation would silently rewrite.
        """
        if latest is None:
            return []
        factors = (latest.breakdown or {}).get("factors", {})
        rows = []
        for name, data in factors.items():
            # The enum already carries the display label. Title-casing the key
            # instead turns CSAT into "Csat".
            try:
                label = FactorId(name).label
            except ValueError:
                label = name.replace("_", " ").title()
            rows.append(
                {
                    "name": label,
                    "outcome": data.get("outcome"),
                    "color": (data.get("color") or "").lower(),
                    "points": data.get("points"),
                    "contribution": data.get("contribution"),
                    "reason": data.get("reason"),
                    "detail": data.get("detail"),
                }
            )
        return rows


class HealthOverrideView(AccountScopedMixin, FormView):
    required_capability = cap.HEALTH_OVERRIDE
    template_name = "health/override_form.html"
    form_class = OverrideForm
    account_url_kwarg = "account_pk"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "accounts"
        context["override"] = active_override(self.account)
        return context

    def form_valid(self, form):
        try:
            apply_override(
                self.account,
                status=form.cleaned_data["status"],
                reason=form.cleaned_data["reason"],
                actor=self.request.user,
            )
        except (ValidationError, OverrideNotAllowed) as exc:
            form.add_error(None, getattr(exc, "messages", [str(exc)]))
            return self.form_invalid(form)

        messages.success(self.request, "Health status overridden.")
        return redirect("health:panel", account_pk=self.account.pk)


class HealthOverrideClearView(AccountScopedMixin, TemplateView):
    required_capability = cap.HEALTH_OVERRIDE
    template_name = "health/override_confirm_clear.html"
    account_url_kwarg = "account_pk"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "accounts"
        context["override"] = active_override(self.account)
        return context

    def post(self, request, *args, **kwargs):
        clear_override(self.account, actor=request.user)
        messages.success(request, "Override cleared; the computed status applies again.")
        return redirect("health:panel", account_pk=self.account.pk)
