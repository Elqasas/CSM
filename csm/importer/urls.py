from __future__ import annotations

from django.urls import path

from csm.importer import views

app_name = "importer"

urlpatterns = [
    path("", views.ImportListView.as_view(), name="list"),
    path("upload/", views.ImportUploadView.as_view(), name="upload"),
    path("template.xlsx", views.ImportTemplateView.as_view(), name="template"),
    path(
        "template/<str:kind>.<str:fmt>",
        views.ImportTemplateView.as_view(),
        name="template_for",
    ),
    path("<int:pk>/", views.ImportPreviewView.as_view(), name="preview"),
    path("<int:pk>/apply/", views.ImportApplyView.as_view(), name="apply"),
    path("export/", views.ExportPageView.as_view(), name="export"),
    path("export/<str:kind>.xlsx", views.ExportDownloadView.as_view(), name="export_download"),
]
