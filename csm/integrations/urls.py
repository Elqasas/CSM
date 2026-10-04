from __future__ import annotations

from django.urls import path

from csm.integrations import views

app_name = "integrations"

urlpatterns = [
    path("", views.IntegrationStatusView.as_view(), name="status"),
]
