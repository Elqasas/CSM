"""Sign in and out.

Django's own auth views. ``django-axes`` is installed and wraps authentication
at the backend level, so lockout needs nothing here.
"""

from __future__ import annotations

from django.contrib.auth import views as auth_views
from django.urls import path

from csm.users import views

app_name = "users"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="users/login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/", views.SelfPasswordChangeView.as_view(), name="password_change"),
    path("display/", views.DisplaySettingsView.as_view(), name="display_settings"),
    path("staff/", views.StaffListView.as_view(), name="staff_list"),
    path("staff/new/", views.StaffCreateView.as_view(), name="staff_create"),
    path("staff/<int:pk>/", views.StaffUpdateView.as_view(), name="staff_edit"),
    path(
        "staff/<int:pk>/password/",
        views.StaffSetPasswordView.as_view(),
        name="staff_set_password",
    ),
    path("staff/<int:pk>/remove/", views.StaffDeleteView.as_view(), name="staff_delete"),
]
