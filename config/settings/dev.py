from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]


# MailHog if docker-compose is up, console otherwise.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

Q_CLUSTER = {
    "name": "csm",
    "orm": "default",
    "workers": 2,
    "timeout": 300,
    "retry": 360,
    "catch_up": False,
}
