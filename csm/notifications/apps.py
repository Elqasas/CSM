from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "csm.notifications"
    label = "notifications"
    verbose_name = "Notifications"

    def ready(self) -> None:
        from csm.notifications.dispatcher import register_subscribers

        register_subscribers()
