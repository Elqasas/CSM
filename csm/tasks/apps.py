from django.apps import AppConfig


class TasksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "csm.tasks"
    label = "tasks"
    verbose_name = "Tasks"

    def ready(self) -> None:
        from csm.tasks.generators import register_subscribers
        from csm.tasks.recurrence import register_subscribers as register_recurrence

        register_subscribers()
        register_recurrence()
