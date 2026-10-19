from __future__ import annotations

from django.urls import path

from csm.reports import views

app_name = "reports"

urlpatterns = [
    path("", views.ReportsView.as_view(), name="index"),
    path("<slug:key>.xlsx", views.ReportDownloadView.as_view(), name="download"),
]
