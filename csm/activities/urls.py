from __future__ import annotations

from django.urls import path

from csm.activities import views

app_name = "activities"

urlpatterns = [
    path("", views.ActivityListView.as_view(), name="list"),
    path(
        "account/<int:account_pk>/",
        views.AccountTimelineView.as_view(),
        name="timeline",
    ),
    path("account/<int:account_pk>/new/", views.ActivityCreateView.as_view(), name="create"),
    path("<int:pk>/void/", views.ActivityVoidView.as_view(), name="void"),
]
