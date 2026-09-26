from __future__ import annotations

from django.urls import path

from csm.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="index"),
    path("analytics/", views.AnalyticsDashboardView.as_view(), name="analytics"),
]
