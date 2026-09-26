"""View-layer plumbing for the authorization rules.

Every view in the platform goes through one of these. The point is that a view
never reads ``role`` or ``is_senior`` and never starts a query from
``Account.objects``: it asks the ``Ability`` built here, exactly as
``core/access`` requires.

``AccountScopedMixin`` is the load-bearing one. It resolves the account from
``ability.accounts()`` rather than from the default manager, so a user asking for
a colleague's account by primary key gets a 404 rather than a page. A 404 rather
than a 403 is deliberate: a 403 confirms the account exists, which is itself a
disclosure.
"""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from csm.core.access.abilities import Ability

#: What "plan something" offers, and the icon each gets. Here rather than in a
#: template so the navbar, the activity page and a client's timeline cannot come
#: to offer three different lists.
ADD_KINDS = (
    ("MANUAL_NEXT_ACTION", "Task", "bi-check2-square"),
    ("MEETING", "Meeting", "bi-calendar-event"),
    ("CALL", "Call", "bi-telephone"),
)


def safe_next(request) -> str:
    """Where to return after acting, when the action was started somewhere else.

    Actions are launched from lists - the accounts table, the dashboard card,
    a contact page - and dropping the reader on a detail page afterwards loses
    the filters, the sort and their place in it.

    ``next`` arrives in the request, so it is checked against this host. An
    unchecked one is an open redirect: a link that looks like this application
    and ends on somebody else's login page.
    """
    from django.utils.http import url_has_allowed_host_and_scheme

    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return ""


class AbilityMixin(LoginRequiredMixin):
    """Puts an ``Ability`` on the view and in the template context."""

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.ability = Ability(request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["ability"] = self.ability
        return context


class CapabilityRequiredMixin(AbilityMixin):
    """Refuses the view unless the user holds ``required_capability``.

    This is the coarse gate. Row scope is a separate question answered by
    ``ability.accounts()``, and holding a capability never implies reach.
    """

    required_capability: str | None = None

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            capability = self.required_capability
            if capability is not None and not self.ability.can(capability):
                raise PermissionDenied(
                    f"This user does not hold {capability!r}."
                )
        return super().dispatch(request, *args, **kwargs)


class AccountScopedMixin(CapabilityRequiredMixin):
    """Resolves ``self.account`` from the user's own row scope.

    Views that hang off an account (contacts, activities, the health panel) use
    this instead of looking the account up themselves, so the scoping cannot be
    forgotten in one place and remembered in another.
    """

    account_url_kwarg = "pk"

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self._account = None

    @property
    def account(self):
        if self._account is None:
            self._account = get_object_or_404(
                self.ability.accounts(), pk=self.kwargs[self.account_url_kwarg]
            )
        return self._account

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["account"] = self.account
        return context


class SettingsView(LoginRequiredMixin, TemplateView):
    """Everything a reader can change, on one page.

    A page rather than the dropdown this replaced: a menu of settings makes
    somebody choose before they can see what the choices are, and hides the
    ones they did not already know existed. Each card is gated on its own
    capability, so the page shows what this reader can actually do.
    """

    template_name = "core/settings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "settings"
        return context


def ui_state(request):
    """Small facts every page needs about this browser's own preferences.

    ``skip_delete_confirm`` is the "do not ask again today" answer, stored as a
    date in the session and compared against the business date - so it expires
    overnight without anything having to clean it up, and the next working day
    asks again.
    """
    from csm.core.clock import today

    session = getattr(request, "session", None)
    if session is None:
        return {"skip_delete_confirm": False}
    return {
        "skip_delete_confirm": session.get("skip_delete_confirm_on") == today().isoformat()
    }


def ability(request):
    """Context processor, so base templates can gate navigation.

    Django templates cannot pass arguments to a method, so they test membership
    instead: ``{% if "health.view" in ability.capabilities %}``. Either way the
    template asks the Ability rather than testing a role, which keeps the
    single-home rule true of the template layer too.
    """
    user = getattr(request, "user", None)
    if user is None:
        return {}
    # ADD_KINDS rides along so every "plan something" control - the navbar, the
    # activity page, a client's timeline - offers the same three in the same
    # order. Three templates each holding their own list is three lists.
    return {"ability": Ability(user), "add_kinds": ADD_KINDS}
