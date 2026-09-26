from django.apps import AppConfig


class HealthConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "csm.health"
    label = "health"
    verbose_name = "Health"

    def ready(self) -> None:
        from csm.health import signals  # noqa: F401
