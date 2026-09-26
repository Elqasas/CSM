from .base import *  # noqa: F403
from .base import DATABASES, SECRET_KEY, env

DEBUG = False

INSTALLED_APPS += ["axes"]  # noqa: F405
MIDDLEWARE += ["axes.middleware.AxesMiddleware"]  # noqa: F405
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # hour

if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise RuntimeError(
        "Production requires PostgreSQL. Set DATABASE_URL to a postgres:// URL."
    )

# The base module gives SECRET_KEY a development default so a fresh clone runs.
# Inheriting that default in production would be catastrophic and completely
# silent: session cookies, password reset links and signed data would all be
# forgeable by anyone who has read this repository. `check --deploy` does not
# catch it, because a key is set - it is simply the wrong one. Refusing to boot
# is the only failure mode anybody notices.
if SECRET_KEY == "insecure-dev-key-override-in-production":  # noqa: F405
    raise RuntimeError(
        "SECRET_KEY is still the development default. Set a real one in the "
        "environment before starting production."
    )

if len(SECRET_KEY) < 50:  # noqa: F405
    raise RuntimeError("SECRET_KEY must be at least 50 characters.")

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
vars().update(env.email_url("EMAIL_URL", default="smtp://localhost:25"))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}
