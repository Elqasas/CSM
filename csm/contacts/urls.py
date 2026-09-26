from __future__ import annotations

from django.urls import path

from csm.contacts import views

app_name = "contacts"

urlpatterns = [
    path("", views.ContactAttentionListView.as_view(), name="attention"),
    path("triage/", views.ContactTriageListView.as_view(), name="triage"),
    path("new/", views.ContactCreateAnywhereView.as_view(), name="create_anywhere"),
    path("<int:pk>/", views.ContactDetailView.as_view(), name="detail"),
    path("<int:pk>/assess/", views.ContactAssessView.as_view(), name="assess"),
    path(
        "<int:pk>/focal-point/",
        views.ContactToggleFocalPointView.as_view(),
        name="toggle_focal_point",
    ),
    path("<int:pk>/edit/", views.ContactUpdateView.as_view(), name="edit"),
    path("<int:pk>/delete/", views.ContactDeleteView.as_view(), name="delete"),
    path("account/<int:account_pk>/grid/", views.ContactGridView.as_view(), name="grid"),
    path("account/<int:account_pk>/new/", views.ContactCreateView.as_view(), name="create"),
]
