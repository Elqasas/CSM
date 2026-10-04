"""Settings shared by every environment.

Environment-specific modules (dev, test, prod) import * from here and override.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    APP_TIME_ZONE=(str, "Africa/Cairo"),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key-override-in-production")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "django_countries",
    # The scheduler lives here rather than in the environment modules so the
    # schedules are registrable and testable in every one of them. Without a
    # running `qcluster` it is inert, which is the dev default.
    "django_q",
]

LOCAL_APPS = [
    "csm.core",
    "csm.users",
    "csm.accounts",
    "csm.contacts",
    "csm.activities",
    "csm.engagement",
    "csm.health",
    "csm.tasks",
    "csm.notifications",
    "csm.dashboard",
    "csm.schedule",
    "csm.importer",
    "csm.integrations",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "csm.core.modal.ModalMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Navigation and every template-level permission test go through
                # the Ability, so no template reads a role.
                "csm.core.views.ability",
                "csm.core.views.ui_state",
                "csm.core.modal.base_template",
                "csm.notifications.views.unread_count",
                "csm.integrations.views.jira_context",
            ],
        },
    },
]

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
# PostgreSQL 16 is the production target. DATABASE_URL is unset by default so a
# fresh clone runs on SQLite without a database server; SQLite supports the
# partial unique indexes, generated columns and JSON this schema relies on.

DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

AUTH_USER_MODEL = "users.User"
LOGIN_URL = "users:login"
LOGIN_REDIRECT_URL = "dashboard:index"
LOGOUT_REDIRECT_URL = "users:login"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------
# Internationalisation and time
# --------------------------------------------------------------------------
# Storage is UTC. APP_TIME_ZONE is the single business timezone in which every
# day-count scoring boundary is evaluated; csm.core.clock is the only place
# that conversion happens.

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

APP_TIME_ZONE = env("APP_TIME_ZONE")

# --------------------------------------------------------------------------
# Static and media
# --------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

ACCOUNT_LOGO_MAX_BYTES = 2 * 1024 * 1024

# --------------------------------------------------------------------------
# Scheduled jobs
# --------------------------------------------------------------------------
# The cluster is inert until `manage.py qcluster` runs, so this is safe in every
# environment. Register the schedules themselves with `manage.py setup_schedules`.
#
# retry must exceed timeout. Left on the defaults django-q warns, and rightly:
# a task retried before the first attempt has finished means two recalculations
# of the same account racing, which the row lock would serialise into one
# arbitrarily winning.

Q_CLUSTER = {
    "name": "csm",
    "orm": "default",
    "workers": 2,
    "timeout": 600,
    "retry": 900,
    "catch_up": False,
}

# --------------------------------------------------------------------------
# Jira Service Management
# --------------------------------------------------------------------------
# Tickets come from JSM once all three of the site, the API user's email and an
# API token are set (csm/integrations/jira/settings.py). Until then tickets are
# entered by hand; once they are, manual entry is withdrawn. Leave all three
# empty for "off" - two of three is refused rather than read as off.

JIRA_BASE_URL = env("JIRA_BASE_URL", default="")
JIRA_EMAIL = env("JIRA_EMAIL", default="")
JIRA_API_TOKEN = env("JIRA_API_TOKEN", default="")
# Limit the sync to these projects. Empty means every project the API user sees.
JIRA_PROJECT_KEYS = env.list("JIRA_PROJECT_KEYS", default=[])
# The Organizations custom field id, e.g. customfield_10002. Discovered from the
# site when empty; set it only if discovery picks the wrong field.
JIRA_ORGANIZATIONS_FIELD = env("JIRA_ORGANIZATIONS_FIELD", default="")
# Jira issue types that score as a Bug (x3). Every other type scores x1.
JIRA_BUG_ISSUE_TYPES = env.list("JIRA_BUG_ISSUE_TYPES", default=["Bug"])
# How far back the very first sync reads closed issues. Open ones are read
# whatever their age.
JIRA_BACKFILL_DAYS = env.int("JIRA_BACKFILL_DAYS", default=90)

# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="csm-platform@dphish.com")
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# One digest email per user per morning, plus an immediate standalone email for
# every health alert. Set to False for strict per-task emails.
NOTIFICATION_DIGEST_ENABLED = True
NOTIFICATION_OVERDUE_NUDGE_DAYS = 3

# How far ahead the morning digest looks. The digest is the one message people
# are asked to read every day, so it carries the week rather than only what is
# already late: a renewal you learn about on the morning it is due is not a
# reminder, it is an apology.
NOTIFICATION_LOOKAHEAD_DAYS = 7

# How long before a task with a clock time its reminders go out, in minutes.
TASK_REMINDER_MINUTES = (60, 30)

# How late a reminder may still be sent. If the worker was down over the hour
# mark, the "in one hour" message is dropped rather than delivered at twenty
# past - a reminder that is wrong about the time is worse than none, and the
# nearer one is still coming.
TASK_REMINDER_GRACE_MINUTES = 15
