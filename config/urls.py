from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from csm.core import views as core_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("csm.dashboard.urls")),
    path("accounts/", include("csm.accounts.urls")),
    path("contacts/", include("csm.contacts.urls")),
    path("activities/", include("csm.activities.urls")),
    path("calendar/", include("csm.schedule.urls")),
    path("settings/", core_views.SettingsView.as_view(), name="settings"),
    path("tasks/", include("csm.tasks.urls")),
    path("health/", include("csm.health.urls")),
    path("import/", include("csm.importer.urls")),
    path("reports/", include("csm.reports.urls")),
    path("engagement/", include("csm.engagement.urls")),
    path("notifications/", include("csm.notifications.urls")),
    path("integrations/", include("csm.integrations.urls")),
    path("auth/", include("csm.users.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
