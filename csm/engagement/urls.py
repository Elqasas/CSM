from __future__ import annotations

from django.urls import path, re_path

from csm.engagement import views

app_name = "engagement"

urlpatterns = [
    path("account/<int:account_pk>/", views.EngagementPanelView.as_view(), name="panel"),
    path(
        "account/<int:account_pk>/ticket/new/",
        views.TicketCreateView.as_view(),
        name="ticket_create",
    ),
    path(
        "account/<int:account_pk>/ticket/<int:pk>/close/",
        views.TicketCloseView.as_view(),
        name="ticket_close",
    ),
    path("account/<int:account_pk>/csat/new/", views.CsatCreateView.as_view(), name="csat_create"),
    path(
        "account/<int:account_pk>/adoption/new/",
        views.AdoptionCreateView.as_view(),
        name="adoption_create",
    ),
    path(
        "account/<int:account_pk>/item/new/",
        views.EngagementItemCreateView.as_view(),
        name="item_create",
    ),
    # The kind is constrained in the URL rather than checked in the view, so an
    # unknown one is a 404 before any capability is looked up.
    re_path(
        r"^account/(?P<account_pk>\d+)/(?P<what>ticket|csat|adoption|item)/(?P<pk>\d+)/delete/$",
        views.EngagementDeleteView.as_view(),
        name="delete",
    ),
]
