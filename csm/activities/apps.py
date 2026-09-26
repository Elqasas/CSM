from django.apps import AppConfig


class ActivitiesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "csm.activities"
    label = "activities"
    verbose_name = "Activities"

    def ready(self):
        """Completing a plan writes the record of it.

        Registered here rather than imported at module level, for the same
        reason every other subscriber is: the event bus has to exist and the
        models have to be loaded before anything can listen.
        """
        from csm.activities.subscribers import register_subscribers

        register_subscribers()
