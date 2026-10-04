"""Encryption for the secrets an administrator types into the integrations page.

Fernet (AES-128-CBC with an HMAC), keyed from ``SECRET_KEY`` through a
purpose-specific hash so the derived key is used for nothing else. The database
alone - a backup, a dump, a copy on a laptop - is not enough to read a token.

Rotating ``SECRET_KEY`` makes stored tokens unreadable. ``decrypt`` then
returns an empty string rather than raising, Jira reads as not configured, and
the page asks for the token again: a re-entry, not an outage of every page.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

_PURPOSE = b"csm.integrations.secret.v1:"


def _fernet() -> Fernet:
    digest = hashlib.sha256(_PURPOSE + settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""
