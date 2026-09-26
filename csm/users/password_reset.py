"""Forgot-password by emailed one-time code.

The current password cannot be sent: it is stored as a one-way hash. What is
sent instead is a six-digit code that proves the reader controls the mailbox,
after which they choose a new password.

Three steps, each with one job:

1. ``issue_code(email)`` - send a code if an active account has that address.
   Silent either way, so the page cannot be used to learn who has an account.
2. ``verify_code(email, code)`` - check it, counting wrong guesses. A six-digit
   code is guessable in a million tries, so five is the whole budget.
3. ``complete_reset(code_row, password)`` - set the password and spend the code.

The code email is sent at once rather than through the notification outbox,
which drains on a schedule: a code that arrives after it expires is no code.
"""

from __future__ import annotations

import datetime as _dt
import logging
import secrets

from django.apps import apps
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils.crypto import constant_time_compare, salted_hmac

from csm.core import clock
from csm.users.models import PasswordResetCode, User

logger = logging.getLogger(__name__)

CODE_DIGITS = 6
CODE_MINUTES = getattr(settings, "PASSWORD_RESET_CODE_MINUTES", 10)
MAX_ATTEMPTS = 5
#: Codes one person may be sent per hour. Beyond it the request is dropped as
#: silently as an unknown address - otherwise the form is a way to flood a
#: colleague's inbox.
MAX_CODES_PER_HOUR = 3


def _hash(user_pk: int, code: str) -> str:
    # Keyed by SECRET_KEY and bound to the user, so a copied table is not a
    # million-row lookup away from every live code.
    return salted_hmac("csm.users.password_reset", f"{user_pk}:{code}").hexdigest()


def _user_for(email: str) -> User | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    return User.objects.filter(email=email, is_active=True).first()


def live_code(email: str) -> PasswordResetCode | None:
    """The one code that can still be used for this address, if any."""
    user = _user_for(email)
    if user is None:
        return None
    return (
        PasswordResetCode.objects.filter(
            user=user,
            used_at__isnull=True,
            expires_at__gt=clock.now(),
            attempts__lt=MAX_ATTEMPTS,
        )
        .order_by("-created_at")
        .first()
    )


def issue_code(email: str) -> bool:
    """Send a fresh code. Returns whether one was sent - for tests and logs
    only; the caller must not tell the reader."""
    user = _user_for(email)
    if user is None:
        return False
    now = clock.now()
    recent = PasswordResetCode.objects.filter(
        user=user, created_at__gte=now - _dt.timedelta(hours=1)
    ).count()
    if recent >= MAX_CODES_PER_HOUR:
        logger.warning("Password reset throttled for user %s", user.pk)
        return False

    code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
    with transaction.atomic():
        PasswordResetCode.objects.filter(
            user=user, used_at__isnull=True, expires_at__gt=now
        ).update(expires_at=now)
        PasswordResetCode.objects.create(
            user=user,
            code_hash=_hash(user.pk, code),
            created_at=now,
            expires_at=now + _dt.timedelta(minutes=CODE_MINUTES),
        )

    try:
        send_mail(
            subject="Your password reset code",
            message=(
                f"Your code is {code}\n\n"
                f"It works once and expires in {CODE_MINUTES} minutes.\n\n"
                "If you did not ask to reset your password, ignore this email - "
                "your password has not changed."
            ),
            from_email=None,
            recipient_list=[user.email],
        )
    except Exception:  # noqa: BLE001 - the page must look the same either way
        logger.exception("Could not email a password reset code to user %s", user.pk)
        return False
    return True


def verify_code(email: str, code: str) -> PasswordResetCode | None:
    """The code row when ``code`` is right, else None. A wrong guess is counted."""
    row = live_code(email)
    if row is None:
        return None
    if constant_time_compare(row.code_hash, _hash(row.user_id, (code or "").strip())):
        row.verified_at = clock.now()
        row.save(update_fields=["verified_at"])
        return row
    PasswordResetCode.objects.filter(pk=row.pk).update(attempts=row.attempts + 1)
    return None


def verified_code(pk) -> PasswordResetCode | None:
    """A code that passed ``verify_code`` and has not expired or been spent."""
    return (
        PasswordResetCode.objects.select_related("user")
        .filter(
            pk=pk,
            verified_at__isnull=False,
            used_at__isnull=True,
            expires_at__gt=clock.now(),
            user__is_active=True,
        )
        .first()
    )


@transaction.atomic
def complete_reset(row: PasswordResetCode, password: str) -> None:
    now = clock.now()
    user = row.user
    user.set_password(password)
    user.save(update_fields=["password"])
    PasswordResetCode.objects.filter(user=user, used_at__isnull=True).update(used_at=now)
    if apps.is_installed("axes"):
        # A person locked out by guessing is exactly who resets; leaving the
        # lockout in place makes the new password useless for an hour.
        from axes.utils import reset

        reset(username=user.email)
