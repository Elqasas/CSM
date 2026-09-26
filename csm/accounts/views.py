"""Account list, detail and edit.

Every queryset here starts from ``ability.accounts()``. None of them starts from
``Account.objects``, including the search, because a CSM searching a colleague's
client by name must find nothing.

Accounts reach the template wrapped in ``AccountPresenter``. The presenter is
what makes an ACM's page provably health-free: it raises rather than returning
None, so a template that asks for a score it may not have fails loudly in
development instead of quietly rendering an empty cell in production.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View

from csm.accounts.filters import (
    RENEWAL_PARAM,
    apply_renewal_quarter,
    apply_sort,
    renewal_quarter_choices,
)
from csm.accounts.forms import AccountForm, CustomFieldDefinitionForm
from csm.accounts.models import AccountCustomFieldDefinition
from csm.contacts.grids import build_grid, interest_labels
from csm.contacts.models import Contact
from csm.core.access import capabilities as cap
from csm.core.access.presenters import AccountPresenter
from csm.core.clock import now, today
from csm.core.enums import AccountClass, HealthStatus, LicenseType, LifecycleStage
from csm.core.views import (
    AbilityMixin,
    AccountScopedMixin,
    CapabilityRequiredMixin,
    safe_next,
)


class AccountListView(CapabilityRequiredMixin, ListView):
    required_capability = cap.ACCOUNT_VIEW
    template_name = "accounts/account_list.html"
    context_object_name = "accounts"
    paginate_by = 25

    def get_queryset(self):
        qs = self.ability.accounts().select_related("csm", "acm", "partner")

        term = self.request.GET.get("q", "").strip()
        if term:
            qs = qs.search(term)

        for param, field in (
            ("status", "effective_health_status"),
            ("class", "account_class"),
            ("stage", "lifecycle_stage"),
            ("country", "country"),
            ("license", "license_type"),
        ):
            value = self.request.GET.get(param, "").strip()
            if not value:
                continue
            # Health is a filterable column only for users who may see it.
            # Without this an ACM could infer a status by comparing result
            # counts across filter values.
            if field == "effective_health_status" and not self.ability.can(cap.HEALTH_VIEW):
                continue
            qs = qs.filter(**{field: value})

        managed = self.request.GET.get("managed", "").strip()
        if managed in ("1", "0"):
            qs = qs.filter(is_managed_service=managed == "1")

        qs = apply_renewal_quarter(qs, self.request.GET.get(RENEWAL_PARAM))

        return apply_sort(
            qs,
            self.request.GET.get("sort"),
            shows_health=self.ability.can(cap.HEALTH_VIEW),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "accounts"
        context["presented"] = [
            AccountPresenter(account, self.ability) for account in context["accounts"]
        ]
        context["shows_health"] = self.ability.can(cap.HEALTH_VIEW)
        context["health_statuses"] = HealthStatus.choices
        context["account_classes"] = AccountClass.choices
        context["lifecycle_stages"] = LifecycleStage.choices
        context["license_types"] = LicenseType.choices
        # From the unfiltered scope, so choosing a quarter does not leave the
        # control offering only the quarter already chosen.
        context["renewal_quarters"] = renewal_quarter_choices(self.ability.accounts())
        context["filters"] = {
            "q": self.request.GET.get("q", ""),
            "renewal": self.request.GET.get(RENEWAL_PARAM, ""),
            "status": self.request.GET.get("status", ""),
            "class": self.request.GET.get("class", ""),
            "stage": self.request.GET.get("stage", ""),
            "license": self.request.GET.get("license", ""),
            "managed": self.request.GET.get("managed", ""),
            "sort": self.request.GET.get("sort", "name"),
        }
        context["has_filters"] = any(
            value for key, value in context["filters"].items() if key != "sort"
        )
        return context


class AccountDetailView(AbilityMixin, DetailView):
    template_name = "accounts/account_detail.html"
    context_object_name = "account"

    def get_queryset(self):
        return self.ability.accounts().select_related("csm", "acm", "partner")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account = self.object
        context["nav"] = "accounts"
        context["presented"] = AccountPresenter(account, self.ability)
        # The override actions sit on the health card, so the card needs to know
        # whether this reader may use them. The status itself comes through the
        # presenter; only the permission is asked here.
        context["can_override"] = self.ability.can_override_health(account)

        contacts = list(
            Contact.objects.filter(account=account).active().with_last_contacted()
        )
        context["contacts"] = contacts
        # The same grid partial the dedicated stakeholder page renders, built
        # from the same helper, so the two views cannot show different grids.
        context["rows"] = build_grid(contacts)
        context["interest_labels"] = interest_labels()
        context["needs_triage"] = [c for c in contacts if not c.is_assessed]
        context["triage_count"] = len(context["needs_triage"])
        # Events and digital content, which sit where the activity log used to:
        # the log is the timeline's business, and a slice of it here was a third
        # place to read the same rows.
        context["engagement_items"] = account.engagement_items.order_by("-occurred_on")[:12]
        context["activities"] = (
            self.ability.visible_activities(account.activities)
            .select_related("contact", "created_by")
            .filter(is_voided=False)
            .order_by("-occurred_on", "-id")[:15]
        )
        context["tasks"] = (
            self.ability.visible_tasks()
            .filter(account=account)
            .open()
            .select_related("contact")
            .order_by("due_date")
        )
        return context


class AccountUpdateView(AccountScopedMixin, UpdateView):
    """Editing, narrowed to the fields this user may change.

    The form's field list comes from ``ability.editable_account_fields``, so an
    ACM is served a form that physically lacks every health-adjacent input
    rather than one that rejects them on submit. A field absent from the form
    cannot be posted into the model.
    """

    required_capability = cap.ACCOUNT_EDIT
    template_name = "accounts/account_form.html"
    form_class = AccountForm
    context_object_name = "account"

    def get_queryset(self):
        return self.ability.accounts()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["ability"] = self.ability
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "accounts"
        context["next"] = safe_next(self.request)
        return context

    def form_valid(self, form):
        messages.success(self.request, f"Saved {form.instance.client_name}.")
        return super().form_valid(form)

    def get_success_url(self):
        return safe_next(self.request) or reverse("accounts:detail", args=[self.object.pk])


class AccountCreateView(CapabilityRequiredMixin, CreateView):
    """Create an account by hand, for a client that never came through Excel.

    The form is field-scoped exactly as the edit form is, so an ACM who may
    create nothing sees nothing, and nobody can post a system-written column.

    The scoring baseline is set to today for the same reason the importer sets
    it: an account with no history behind it must not read as months of neglect
    on its first evening.
    """

    required_capability = cap.ACCOUNT_CREATE
    template_name = "accounts/account_form.html"
    form_class = AccountForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["ability"] = self.ability
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "accounts"
        context["is_create"] = True
        return context

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        form.instance.scoring_baseline_date = today()
        response = super().form_valid(form)
        messages.success(self.request, f"Created {form.instance.client_name}.")
        return response

    def get_success_url(self):
        return reverse("accounts:detail", args=[self.object.pk])


class AccountDeleteView(AccountScopedMixin, View):
    """Soft deletion. A CSM may delete their own portfolio's accounts; row
    scope is what lets a senior CSM's reach extend to everyone else's.

    The specification asks for a cascade to every activity, contact and task.
    That is unrecoverable and almost always a mistake at this portfolio size, so
    the row is marked deleted instead and disappears from every list. The data
    survives for the day somebody asks for it back.
    """

    required_capability = cap.ACCOUNT_DELETE
    template_name = "accounts/account_confirm_delete.html"

    #: The same confirmation, as the inside of a dialog. One view serves both so
    #: the page and the pop-up cannot come to say different things about what a
    #: deletion does.
    fragment_template = "accounts/_delete_confirm.html"

    def get(self, request, pk):
        from django.shortcuts import render

        return render(
            request,
            self.fragment_template if request.headers.get("HX-Request") else self.template_name,
            {
                "account": self.account,
                "ability": self.ability,
                "nav": "accounts",
                # Carried through the confirmation so Cancel puts the reader
                # back in the list they were working down, filters and all.
                "next": safe_next(request),
            },
        )

    def post(self, request, pk):
        account = self.account
        account.deleted_at = now()
        account.deleted_by = request.user
        account.save(update_fields=["deleted_at", "deleted_by"])
        messages.success(
            request,
            f"Deleted {account.client_name}. Its history is kept and it can be restored.",
        )
        return redirect(safe_next(request) or reverse("accounts:list"))


class CustomFieldListView(CapabilityRequiredMixin, ListView):
    """Every custom field definition, active or not, with a form to add one.

    Admin-only: naming a field is a schema-level decision that shows up on
    every account, unlike filling one in, which any editor may do through the
    account form itself.
    """

    required_capability = cap.CUSTOM_FIELD_MANAGE
    template_name = "accounts/custom_field_list.html"
    context_object_name = "definitions"

    def get_queryset(self):
        return AccountCustomFieldDefinition.objects.all()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "custom_fields"
        context["form"] = kwargs.get("form") or CustomFieldDefinitionForm()
        return context

    def post(self, request):
        form = CustomFieldDefinitionForm(request.POST)
        if form.is_valid():
            definition = form.save(commit=False)
            definition.created_by = request.user
            definition.save()
            messages.success(request, f"Added the field {definition.name!r}.")
            return redirect("accounts:custom_fields")
        self.object_list = self.get_queryset()
        return self.render_to_response(self.get_context_data(form=form))


class CustomFieldToggleView(CapabilityRequiredMixin, View):
    """Deactivate or reactivate a field. Never deletes: past values on
    accounts that already carry one must stay readable."""

    required_capability = cap.CUSTOM_FIELD_MANAGE

    def post(self, request, pk):
        from django.shortcuts import get_object_or_404

        definition = get_object_or_404(AccountCustomFieldDefinition, pk=pk)
        definition.is_active = not definition.is_active
        definition.save(update_fields=["is_active", "updated_at"])
        messages.success(
            request,
            f"{'Reactivated' if definition.is_active else 'Deactivated'} "
            f"{definition.name!r}.",
        )
        return redirect("accounts:custom_fields")
