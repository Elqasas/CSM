from django.apps import AppConfig


class ImporterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "csm.importer"
    label = "importer"
    verbose_name = "Importer"
