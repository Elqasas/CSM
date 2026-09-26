from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "csm.users"
    label = "users"
    verbose_name = "Users"
