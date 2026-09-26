"""In-app notifications.

Scoped to the signed-in user and nobody else - not by row scope over accounts,
but by ownership of the notification itself. That distinction matters: a senior
CSM can act on every account yet is notified only on their own, and reading
another person's notifications through this list would quietly undo the rule
``notifications/routing.py`` exists to guarantee.

Only the in-app channel is listed. Email rows are the outbox, drained by
``send_due_notifications``, and showing them here would double every alert.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import ListView, View

from csm.core.clock import now
from csm.core.enums import NotificationChannel
from csm.core.views import AbilityMixin
from csm.notifications.models import Notification


def _mine(user):
    return Notification.objects.filter(
        user=user, channel=NotificationChannel.IN_APP
    ).select_related("account", "task")


class NotificationListView(AbilityMixin, ListView):
    template_name = "notifications/notification_list.html"
    context_object_name = "notifications"
    paginate_by = 40

    def get_queryset(self):
        qs = _mine(self.request.user).order_by("-created_at")
        if self.request.GET.get("show") != "all":
            qs = qs.unread()
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "notifications"
        context["showing_all"] = self.request.GET.get("show") == "all"
        context["unread_count"] = _mine(self.request.user).unread().count()
        return context


class NotificationReadView(AbilityMixin, View):
    """Mark one notification read and follow it to wherever it points."""

    def post(self, request, pk):
        notification = get_object_or_404(_mine(request.user), pk=pk)
        if notification.read_at is None:
            notification.read_at = now()
            notification.save(update_fields=["read_at"])

        target = notification.url or (
            f"/accounts/{notification.account_id}/" if notification.account_id else None
        )
        return redirect(target or "notifications:list")


class NotificationReadAllView(AbilityMixin, View):
    def post(self, request):
        marked = _mine(request.user).unread().update(read_at=now())
        messages.success(request, f"Marked {marked} notification{'' if marked == 1 else 's'} read.")
        return redirect("notifications:list")


def unread_count(request):
    """Context processor for the navigation badge.

    Counted per request rather than cached: the number is small, the query is
    indexed, and a stale badge on a health alert is worse than a query.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"unread_notifications": _mine(user).unread().count()}
