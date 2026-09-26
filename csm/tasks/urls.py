from __future__ import annotations

from django.urls import path

from csm.tasks import views

app_name = "tasks"

urlpatterns = [
    path("", views.TaskListView.as_view(), name="list"),
    path("new/", views.TaskCreateAnywhereView.as_view(), name="create_anywhere"),
    path("contact-options/", views.TaskContactOptionsView.as_view(), name="contact_options"),
    path("<int:pk>/", views.TaskDetailView.as_view(), name="detail"),
    path("<int:pk>/complete/", views.TaskCompleteView.as_view(), name="complete"),
    path("<int:pk>/edit/", views.TaskUpdateView.as_view(), name="edit"),
    path("<int:pk>/delete/", views.TaskDeleteView.as_view(), name="delete"),
    path("account/<int:account_pk>/new/", views.TaskCreateView.as_view(), name="create"),
]
