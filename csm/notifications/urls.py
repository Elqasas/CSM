from __future__ import annotations

from django.urls import path

from csm.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="list"),
    path("<int:pk>/read/", views.NotificationReadView.as_view(), name="read"),
    path("read-all/", views.NotificationReadAllView.as_view(), name="read_all"),
]
