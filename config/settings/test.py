from .base import *  # noqa: F403

DEBUG = False

# In-memory SQLite: the whole scoring surface is pure Python and needs no
# database at all, and the integration layers are small enough that a file-based
# database buys nothing.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "ATOMIC_REQUESTS": True,
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Pin the business timezone so day-count boundary tests are deterministic
# regardless of the machine running them.
APP_TIME_ZONE = "Africa/Cairo"
