from __future__ import annotations

from django.urls import path
from django.views.generic import RedirectView

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
    # The export page became the Reports page. Kept as redirects so a bookmark
    # to either still lands somewhere, and the scope and capability checks are
    # the Reports page's own - a redirect grants nothing.
    path("export/", RedirectView.as_view(pattern_name="reports:index"), name="export"),
    path(
        "export/<slug:key>.xlsx",
        RedirectView.as_view(pattern_name="reports:download"),
        name="export_download",
    ),
]
