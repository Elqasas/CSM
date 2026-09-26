from __future__ import annotations

from django.urls import path

from csm.schedule import views

app_name = "schedule"

urlpatterns = [
    path("", views.CalendarView.as_view(), name="calendar"),
    path("settings/", views.CalendarSettingsView.as_view(), name="settings"),
]
