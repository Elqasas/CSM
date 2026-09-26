"""Administering staff, and changing your own password.

Two audiences in one module. Everything under ``user.manage`` is for an
administrator; the password change at the bottom is for everybody, because an
account whose password only somebody else can change is not really yours.

Deleting a user is deliberately hard. ``Account.csm`` and ``Account.acm`` are
PROTECT, so anyone holding a portfolio cannot be removed at all - and that is
the right answer rather than an obstacle. Deactivation is the normal action: it
stops the sign-in, empties the capability set, and leaves every activity, task
and score history row still attributable to the person who did the work.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, FormView, ListView, UpdateView, View

from csm.core.access import capabilities as cap
from csm.core.enums import Role
from csm.core.views import AbilityMixin, CapabilityRequiredMixin
from csm.users.forms import (
    AdminSetPasswordForm,
    DisplayPreferenceForm,
    SelfPasswordChangeForm,
    StaffUserCreateForm,
    StaffUserForm,
)
from csm.users.management.commands.create_user import generate_password
from csm.users.models import DisplayPreference, User


class DisplaySettingsView(LoginRequiredMixin, FormView):
    """Your clock, and the country beside it.

    No capability: it is nobody's business but the reader's own, and it changes
    nothing anybody else can see. Signed in is the whole requirement.
    """

    template_name = "users/display_settings.html"
    form_class = DisplayPreferenceForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = DisplayPreference.for_user(self.request.user)
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "dashboard"
        context["business_timezone"] = settings.APP_TIME_ZONE
        return context

    def form_valid(self, form):
        form.save()
        messages.success(self.request, "Clock settings saved.")
        return redirect("dashboard:index")


class StaffListView(CapabilityRequiredMixin, ListView):
    required_capability = cap.USER_MANAGE
    template_name = "users/staff_list.html"
    context_object_name = "staff"
    paginate_by = 50

    def get_queryset(self):
        return User.objects.annotate(
            owned_accounts=Count("csm_accounts", distinct=True)
            + Count("acm_accounts", distinct=True)
        ).order_by("-is_active", "full_name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "staff"
        # Counted by role, not by the Django flag: Admin is an access level of
        # this application, and the flag only says who can reach Django's own
        # admin site.
        context["admin_count"] = User.objects.filter(
            role=Role.ADMIN, is_active=True
        ).count()
        return context


class StaffCreateView(CapabilityRequiredMixin, CreateView):
    required_capability = cap.USER_MANAGE
    template_name = "users/staff_form.html"
    form_class = StaffUserCreateForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "staff"
        context["is_create"] = True
        return context

    def form_valid(self, form):
        password = generate_password()
        user = form.save(commit=False)
        user.set_password(password)
        user.save()

        messages.success(
            self.request,
            f"Created {user.email} as {user.effective_level_label}.",
        )
        # Shown once. Nothing stores it in readable form, here or anywhere.
        messages.warning(
            self.request,
            f"One-time password for {user.email}: {password} — hand it over out "
            "of band and have them change it. It will not be shown again.",
        )
        return redirect("users:staff_list")


class StaffUpdateView(CapabilityRequiredMixin, UpdateView):
    required_capability = cap.USER_MANAGE
    template_name = "users/staff_form.html"
    form_class = StaffUserForm
    queryset = User.objects.all()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["editing_self"] = self.object == self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "staff"
        context["is_create"] = False
        context["editing_self"] = self.object == self.request.user
        context["owned"] = (
            self.object.csm_accounts.count() + self.object.acm_accounts.count()
        )
        return context

    def form_valid(self, form):
        messages.success(self.request, f"Saved {form.instance.email}.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("users:staff_list")


class StaffSetPasswordView(CapabilityRequiredMixin, FormView):
    """Set somebody else's password, for the day they forget theirs."""

    required_capability = cap.USER_MANAGE
    template_name = "users/staff_set_password.html"
    form_class = AdminSetPasswordForm

    def get_target(self):
        return get_object_or_404(User, pk=self.kwargs["pk"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.get_target()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "staff"
        context["target"] = self.get_target()
        return context

    def form_valid(self, form):
        target = form.save()
        messages.success(
            self.request,
            f"Password set for {target.email}. They stay signed in on any device "
            "they are already using until that session expires.",
        )
        return redirect("users:staff_list")


class StaffDeleteView(CapabilityRequiredMixin, View):
    """Deactivate, or delete outright when the person holds nothing.

    A user attached to an account cannot be deleted - the foreign keys are
    PROTECT - so the choice is made here rather than left to an IntegrityError.
    Deactivating is almost always what was meant anyway: the work they did stays
    attributed to them.
    """

    required_capability = cap.USER_MANAGE
    template_name = "users/staff_confirm_delete.html"

    def get_target(self):
        return get_object_or_404(User, pk=self.kwargs["pk"])

    def get(self, request, pk):
        from django.shortcuts import render

        target = self.get_target()
        return render(
            request,
            self.template_name,
            {
                "nav": "staff",
                "ability": self.ability,
                "target": target,
                "owned": target.csm_accounts.count() + target.acm_accounts.count(),
                "is_self": target == request.user,
            },
        )

    def post(self, request, pk):
        target = self.get_target()

        if target == request.user:
            messages.error(request, "You cannot remove your own account.")
            return redirect("users:staff_list")

        last_admin = (
            target.role == Role.ADMIN
            and not User.objects.filter(role=Role.ADMIN, is_active=True)
            .exclude(pk=target.pk)
            .exists()
        )
        if last_admin:
            messages.error(
                request,
                "That is the only administrator left. Promote somebody else first.",
            )
            return redirect("users:staff_list")

        owned = target.csm_accounts.count() + target.acm_accounts.count()
        if owned:
            target.is_active = False
            target.save(update_fields=["is_active"])
            messages.success(
                request,
                f"Deactivated {target.email}. They hold {owned} account"
                f"{'' if owned == 1 else 's'}, so the record is kept and their "
                "past work stays attributed to them. Reassign those accounts, "
                "then delete if you still want to.",
            )
        else:
            email = target.email
            target.delete()
            messages.success(request, f"Deleted {email}. They held no accounts.")

        return redirect("users:staff_list")


class SelfPasswordChangeView(AbilityMixin, LoginRequiredMixin, FormView):
    """Change your own password. Available to every role."""

    template_name = "users/password_change.html"
    form_class = SelfPasswordChangeForm
    success_url = reverse_lazy("dashboard:index")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "account"
        return context

    def form_valid(self, form):
        form.save()
        # Changing a password rotates the session hash, which would otherwise
        # sign you out of the browser you just used to change it.
        update_session_auth_hash(self.request, form.user)
        messages.success(self.request, "Password changed.")
        return super().form_valid(form)
