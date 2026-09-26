"""Contacts: the grid, the triage tray and the portfolio attention list.

The attention list is the first account-spanning query in the interface, so it
derives from ``ability.accounts()`` rather than ``Contact.objects``. A CSM must
not see a colleague's stakeholders through it any more than through search.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import FormView, ListView, TemplateView, View

from csm.contacts.forms import ContactAnywhereForm, ContactAssessmentForm, ContactForm
from csm.contacts.grids import build_grid, interest_labels
from csm.contacts.models import Contact
from csm.core.access import capabilities as cap
from csm.core.clock import today
from csm.core.views import AccountScopedMixin, CapabilityRequiredMixin, safe_next


class ContactGridView(AccountScopedMixin, TemplateView):
    """The nine-cell grid plus the triage tray, for one account."""

    required_capability = cap.CONTACT_VIEW
    template_name = "contacts/contact_grid.html"
    account_url_kwarg = "account_pk"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contacts = list(
            Contact.objects.filter(account=self.account).active().with_last_contacted()
        )
        context["nav"] = "accounts"
        context["rows"] = build_grid(contacts)
        context["interest_labels"] = interest_labels()
        context["needs_triage"] = [c for c in contacts if not c.is_assessed]
        context["today"] = today()
        return context


class ContactDetailView(CapabilityRequiredMixin, TemplateView):
    """What we know about one stakeholder, and what to do about them next.

    Clicking a name used to open the edit form, which is the wrong default: most
    of the time the question is "who is this and when did we last speak", not
    "change their job title". Editing is one click from here.

    Adding a task or a CSAT from here is a link to the real view, the same as
    every other quick-add menu in the platform - not a second, simpler form
    embedded on the page that would drift from it.
    """

    required_capability = cap.CONTACT_VIEW
    template_name = "contacts/contact_detail.html"

    def get_contact(self):
        return get_object_or_404(
            Contact.objects.filter(account__in=self.ability.accounts())
            .select_related("account")
            .with_last_contacted(),
            pk=self.kwargs["pk"],
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contact = self.get_contact()
        context["nav"] = "contacts"
        context["contact"] = contact
        context["account"] = contact.account
        context["today"] = today()
        context["channels"] = [c.get_channel_display() for c in contact.channels.all()]
        context["activities"] = (
            self.ability.visible_activities(contact.activities)
            .filter(is_voided=False)
            .select_related("created_by")
            .order_by("-occurred_on", "-id")[:10]
        )
        return context


class ContactAttentionListView(CapabilityRequiredMixin, ListView):
    """Portfolio-wide: who across this user's book has gone quiet.

    Ranks rather than filters. A low-influence contact who has not been spoken
    to in a year still appears, below the people who matter more, because the
    specification asks for neglected contacts "especially those with High
    Influence" and that is emphasis in an ordering, not a filter.
    """

    required_capability = cap.CONTACT_VIEW
    template_name = "contacts/attention_list.html"
    context_object_name = "contacts"
    paginate_by = 50

    def get_queryset(self):
        qs = Contact.objects.filter(account__in=self.ability.accounts())
        term = self.request.GET.get("q", "").strip()
        if term:
            # Scoped first, searched second. The other order would search the
            # whole table and then hide the rows, which is the same mistake as
            # filtering in the template.
            qs = qs.search(term)
        return qs.select_related("account").by_attention()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "contacts"
        context["today"] = today()
        context["q"] = self.request.GET.get("q", "")
        context["triage_total"] = (
            Contact.objects.filter(account__in=self.ability.accounts())
            .needs_triage()
            .count()
        )
        return context


class ContactTriageListView(CapabilityRequiredMixin, ListView):
    """Everyone nobody has placed on the grid yet, across the portfolio."""

    required_capability = cap.CONTACT_VIEW
    template_name = "contacts/triage_list.html"
    context_object_name = "contacts"
    paginate_by = 50

    def get_queryset(self):
        qs = Contact.objects.filter(account__in=self.ability.accounts())
        term = self.request.GET.get("q", "").strip()
        if term:
            qs = qs.search(term)
        return qs.select_related("account").needs_triage()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "contacts"
        context["q"] = self.request.GET.get("q", "")
        context["assessment_form"] = ContactAssessmentForm()
        return context


class ContactAssessView(CapabilityRequiredMixin, FormView):
    """Set influence and interest from the triage tray.

    Answers HTMX with the contact's new grid position so a row can be swapped
    out in place, which is the whole reason triage is quick enough to actually
    get done.
    """

    required_capability = cap.CONTACT_EDIT
    form_class = ContactAssessmentForm
    template_name = "contacts/_assessed_row.html"

    def get_contact(self):
        return get_object_or_404(
            Contact.objects.filter(account__in=self.ability.accounts()),
            pk=self.kwargs["pk"],
        )

    def form_valid(self, form):
        contact = self.get_contact()
        contact.influence = form.cleaned_data["influence"]
        contact.interest = form.cleaned_data["interest"]
        contact.save(update_fields=["influence", "interest", "updated_at"])

        if self.request.headers.get("HX-Request"):
            return self.render_to_response({"contact": contact, "ability": self.ability})
        messages.success(self.request, f"Assessed {contact.full_name}.")
        return redirect("contacts:triage")

    def form_invalid(self, form):
        if self.request.headers.get("HX-Request"):
            return self.render_to_response(
                {"contact": self.get_contact(), "form": form, "ability": self.ability},
                status=400,
            )
        return redirect("contacts:triage")


class ContactToggleFocalPointView(CapabilityRequiredMixin, View):
    """Set or clear focal-point status from wherever a contact is shown.

    A round trip through the full edit form is a lot of ceremony for one
    checkbox, and the account page - the grid, the triage tray - is where a
    CSM actually decides who the client's focal points are.
    """

    required_capability = cap.CONTACT_EDIT

    def get_contact(self):
        return get_object_or_404(
            Contact.objects.filter(account__in=self.ability.accounts()),
            pk=self.kwargs["pk"],
        )

    def post(self, request, pk):
        contact = self.get_contact()
        try:
            contact.set_focal_point(not contact.is_focal_point)
        except ValidationError as exc:
            messages.error(request, exc.message)
        else:
            contact.save(update_fields=["is_focal_point", "focal_rank", "updated_at"])
            messages.success(
                request,
                f"{contact.full_name} is "
                f"{'now' if contact.is_focal_point else 'no longer'} a focal point.",
            )
        return redirect(
            safe_next(request) or reverse("accounts:detail", args=[contact.account_id])
        )


class ContactDeleteView(CapabilityRequiredMixin, View):
    """Remove a contact outright.

    Unlike an account, a contact has no soft-delete columns and nothing of
    consequence cascades from one: every FK onto ``Contact`` elsewhere
    (``Activity``, ``Task``, ``CsatResponse``) is ``SET_NULL``, so a call
    logged with someone stays on the record after they leave, just no longer
    naming them. Only ``ContactChannel`` cascades, which is exactly the
    per-contact bookkeeping it exists to hold.
    """

    required_capability = cap.CONTACT_EDIT
    template_name = "contacts/contact_confirm_delete.html"
    fragment_template = "contacts/_delete_confirm.html"

    def get_contact(self):
        return get_object_or_404(
            Contact.objects.filter(account__in=self.ability.accounts())
            .select_related("account"),
            pk=self.kwargs["pk"],
        )

    def get(self, request, pk):
        from django.shortcuts import render

        return render(
            request,
            self.fragment_template if request.headers.get("HX-Request") else self.template_name,
            {
                "contact": self.get_contact(),
                "ability": self.ability,
                "nav": "contacts",
                "next": safe_next(request),
            },
        )

    def post(self, request, pk):
        contact = self.get_contact()
        name = contact.full_name
        account_id = contact.account_id
        contact.delete()
        messages.success(request, f"Deleted {name}.")
        return redirect(safe_next(request) or reverse("accounts:detail", args=[account_id]))


class ContactCreateView(AccountScopedMixin, FormView):
    required_capability = cap.CONTACT_EDIT
    template_name = "contacts/contact_form.html"
    form_class = ContactForm
    account_url_kwarg = "account_pk"

    def get_form_kwargs(self):
        # The account has to be on the instance before validation, not after:
        # the focal-point rank is assigned per account, and the form's clean()
        # has no other way to know which account a not-yet-saved contact
        # belongs to.
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = Contact(account=self.account)
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "accounts"
        context["is_create"] = True
        context["next"] = safe_next(self.request)
        return context

    def form_valid(self, form):
        contact = form.save()
        messages.success(self.request, f"Added {contact.full_name}.")
        return redirect(
            safe_next(self.request) or reverse("accounts:detail", args=[self.account.pk])
        )


class ContactUpdateView(CapabilityRequiredMixin, FormView):
    required_capability = cap.CONTACT_EDIT
    template_name = "contacts/contact_form.html"
    form_class = ContactForm

    def get_contact(self):
        return get_object_or_404(
            Contact.objects.filter(account__in=self.ability.accounts()),
            pk=self.kwargs["pk"],
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.get_contact()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contact = self.get_contact()
        context["nav"] = "accounts"
        context["contact"] = contact
        context["account"] = contact.account
        context["is_create"] = False
        return context

    def form_valid(self, form):
        contact = form.save()
        messages.success(self.request, f"Saved {contact.full_name}.")
        return redirect("accounts:detail", pk=contact.account_id)

    def get_success_url(self):
        return reverse("accounts:detail", args=[self.get_contact().account_id])


class ContactCreateAnywhereView(CapabilityRequiredMixin, FormView):
    """Add a contact from the contacts section, picking the account.

    The same form as the per-account one, with the account promoted from a URL
    segment to a field. Starting from an account page is still the shorter
    route; this exists because you do not always know which account you want
    until you are looking at the list of them.
    """

    required_capability = cap.CONTACT_EDIT
    template_name = "contacts/contact_form.html"
    form_class = ContactAnywhereForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["accounts"] = self.ability.accounts()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "contacts"
        context["is_create"] = True
        context["pick_account"] = True
        return context

    def form_valid(self, form):
        contact = form.save(commit=False)
        contact.save()
        form.sync_channels(contact)
        messages.success(
            self.request, f"Added {contact.full_name} to {contact.account.client_name}."
        )
        return redirect("accounts:detail", pk=contact.account_id)
