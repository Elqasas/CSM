from __future__ import annotations

from django.urls import path

from csm.health import views

app_name = "health"

urlpatterns = [
    path("account/<int:account_pk>/", views.HealthPanelView.as_view(), name="panel"),
    path(
        "account/<int:account_pk>/override/",
        views.HealthOverrideView.as_view(),
        name="override",
    ),
    path(
        "account/<int:account_pk>/override/clear/",
        views.HealthOverrideClearView.as_view(),
        name="override_clear",
    ),
]
