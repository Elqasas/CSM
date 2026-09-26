"""Run the test suite against PostgreSQL instead of SQLite.

The default test settings use in-memory SQLite, which is fast and right for
everyday work. This module exists because SQLite is not what production runs,
and the schema leans on three things the two engines implement differently:

* **Generated columns** (``total_users_count``, ``has_event``) with
  ``db_persist=True``.
* **Partial unique indexes** - the three business invariants that make "one open
  health alert per account" unfalsifiable rather than merely checked.
* **JSONB** for the score breakdown.

All of them work on both, which is exactly why a difference here would be
quiet. Run this before deploying, and in CI:

    DATABASE_URL=postgres://user:pass@localhost:5432/csm_test \\
        pytest --ds=config.settings.test_postgres

It deliberately does not inherit from ``prod``: this is the test suite, not a
production boot, so it keeps the fast password hasher and the in-memory mail
backend rather than the security posture.
"""

from .base import *  # noqa: F403
from .base import env

DEBUG = False

DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default="postgres://csm:csm@127.0.0.1:5432/csm_test",
    )
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True

if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise RuntimeError(
        "These settings exist to exercise PostgreSQL. Point DATABASE_URL at a "
        "postgres:// URL, or use config.settings.test for SQLite."
    )

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Pinned so day-count boundary tests are deterministic regardless of the machine.
APP_TIME_ZONE = "Africa/Cairo"
