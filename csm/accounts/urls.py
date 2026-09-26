from __future__ import annotations

from django.urls import path

from csm.accounts import views

app_name = "accounts"

urlpatterns = [
    path("", views.AccountListView.as_view(), name="list"),
    path("new/", views.AccountCreateView.as_view(), name="create"),
    path("<int:pk>/", views.AccountDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.AccountUpdateView.as_view(), name="edit"),
    path("<int:pk>/delete/", views.AccountDeleteView.as_view(), name="delete"),
    path(
        "custom-fields/", views.CustomFieldListView.as_view(), name="custom_fields"
    ),
    path(
        "custom-fields/<int:pk>/toggle/",
        views.CustomFieldToggleView.as_view(),
        name="custom_field_toggle",
    ),
]
