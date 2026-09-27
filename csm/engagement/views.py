"""The scoring inputs, entered by hand.

Tickets, CSAT ratings and adoption events are what the health engine reads.
Until now they existed only in the Django admin, which meant the score could not
be moved by the people responsible for it.

Each capability is separate on purpose and the split is not arbitrary: an ACM
records CSAT because they speak to the client commercially, but never touches
tickets or adoption, because those feed the health picture they are kept clear
of. The registry of who holds what is in ``core/access``; nothing here knows.

No view calls the scoring engine. Saving one of these rows fires the model hook
in ``health/signals.py`` and the account rescores after the transaction commits.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import FormView, TemplateView, View

from csm.core.access import capabilities as cap
from csm.core.clock import today
from csm.core.views import AccountScopedMixin, safe_next
from csm.engagement.forms import (
    AdoptionEventForm,
    CsatForm,
    EngagementItemForm,
    TicketForm,
)
from csm.engagement.models import AdoptionEvent, CsatResponse, EngagementItem, Ticket


class EngagementPanelView(AccountScopedMixin, TemplateView):
    """Everything feeding the score for one account, in one place."""

    required_capability = cap.ACCOUNT_VIEW
    template_name = "engagement/panel.html"
    account_url_kwarg = "account_pk"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account = self.account
        context["nav"] = "accounts"
        context["today"] = today()

        context["tickets"] = account.tickets.order_by("status", "-opened_on")[:30]
        context["open_tickets"] = account.tickets.open().count()
        context["csat"] = (
            account.csat_responses.select_related("contact").order_by("-recorded_on")[:20]
        )
        context["adoption"] = account.adoption_events.order_by("-occurred_on")[:20]
        context["items"] = account.engagement_items.order_by("-occurred_on")[:20]

        # The weighted ticket score, shown because the weighting surprises
        # people: one bug is yellow, two are red, and six general tickets are
        # the same as two bugs.
        context["ticket_weight"] = (
            account.tickets.open().filter(ticket_kind="BUG").count() * 3
            + account.tickets.open().filter(ticket_kind="GENERAL").count()
        )
        return context


class _EngagementCreateView(AccountScopedMixin, FormView):
    """Shared plumbing: stamp the account and the author, then return."""

    account_url_kwarg = "account_pk"
    template_name = "engagement/form.html"
    heading = ""
    blurb = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "accounts"
        context["heading"] = self.heading
        context["blurb"] = self.blurb
        context["next"] = safe_next(self.request)
        return context

    def form_valid(self, form):
        record = form.save(commit=False)
        record.account = self.account
        record.created_by = self.request.user
        record.save()
        messages.success(self.request, f"{self.heading} recorded.")
        return redirect(
            safe_next(self.request)
            or reverse("engagement:panel", args=[self.account.pk])
        )


class TicketCreateView(_EngagementCreateView):
    required_capability = cap.TICKET_EDIT
    form_class = TicketForm
    heading = "Ticket"
    blurb = (
        "Open tickets are weighted: a bug counts three, a general ticket one. "
        "Zero is green, one to three is yellow, four and above is red."
    )


class CsatCreateView(_EngagementCreateView):
    required_capability = cap.CSAT_RECORD
    form_class = CsatForm
    heading = "CSAT rating"
    blurb = (
        "A missing or stale rating scores fifty rather than zero or a hundred: "
        "zero would redline the portfolio, a hundred would hide real risk."
    )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["account"] = self.account
        return kwargs

    def get_initial(self):
        # Started from a contact's own menu, the rating is theirs. The form's
        # queryset is this account's active contacts, so a pk from anywhere
        # else simply selects nothing.
        initial = super().get_initial()
        contact = self.request.GET.get("contact", "")
        if contact.isdigit():
            initial["contact"] = contact
        return initial


class AdoptionCreateView(_EngagementCreateView):
    required_capability = cap.ADOPTION_RECORD
    form_class = AdoptionEventForm
    heading = "Adoption event"
    blurb = (
        "Monthly Adoption counts days since the most recent phishing campaign "
        "or course assignment. Under fifteen days is green, up to thirty is "
        "yellow, beyond that is red."
    )


class EngagementItemCreateView(_EngagementCreateView):
    required_capability = cap.ENGAGEMENT_EDIT
    form_class = EngagementItemForm
    heading = "Event or content"
    blurb = (
        "These carry a date and appear on the dashboard trend. They do not feed "
        "the health score."
    )


class TicketCloseView(AccountScopedMixin, View):
    """Close a ticket from the panel, without opening the edit form.

    Closing is the only edit anybody makes in a hurry, and it is the one that
    moves the score, so it gets a button rather than a round trip through a form.
    """

    required_capability = cap.TICKET_EDIT
    account_url_kwarg = "account_pk"

    def post(self, request, account_pk, pk):
        ticket = get_object_or_404(Ticket, pk=pk, account=self.account)
        if ticket.status != "CLOSED":
            ticket.status = "CLOSED"
            ticket.closed_on = today()
            ticket.save(update_fields=["status", "closed_on", "updated_at"])
            messages.success(request, f"Closed: {ticket.title}")
        return redirect("engagement:panel", account_pk=account_pk)


class EngagementDeleteView(AccountScopedMixin, View):
    """Remove a mistyped row.

    These are correcting a data-entry error rather than recording that something
    stopped being true, so unlike an activity they are deleted outright. The
    account rescores on the next recalculation either way.
    """

    account_url_kwarg = "account_pk"

    MODELS = {
        "ticket": (Ticket, cap.TICKET_EDIT),
        "csat": (CsatResponse, cap.CSAT_RECORD),
        "adoption": (AdoptionEvent, cap.ADOPTION_RECORD),
        "item": (EngagementItem, cap.ENGAGEMENT_EDIT),
    }

    @property
    def required_capability(self):
        return self.MODELS[self.kwargs["what"]][1]

    def post(self, request, account_pk, what, pk):
        model, _ = self.MODELS[what]
        record = get_object_or_404(model, pk=pk, account=self.account)
        record.delete()
        messages.success(request, "Deleted.")
        # Back to the page the row was deleted from. Without one, to the page
        # that lists it: tickets, CSAT and adoption sit on the health page,
        # beside the factors they move, and only an item lives on Events.
        home = "engagement:panel" if what == "item" else "health:panel"
        return redirect(safe_next(request) or reverse(home, args=[account_pk]))
