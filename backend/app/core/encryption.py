"""Symmetric encryption for third-party secrets stored in the DB.

Channel credentials (WeChat Work Secret, webhook tokens, provider API keys set via
the admin UI) must not be stored in plaintext. We derive a Fernet key from the app
secret so no extra key material is required for dev; production should set a strong
APP_SECRET_KEY (or wire a KMS in place of this module).
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_PREFIX = "enc::"


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.app_secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(plain: str | None) -> str | None:
    if plain is None or plain == "":
        return plain
    token = _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt_secret(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    if not value.startswith(_PREFIX):
        # Tolerate plaintext written before encryption was enabled.
        return value
    raw = value[len(_PREFIX) :]
    try:
        return _fernet().decrypt(raw.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


def mask_secret(value: str | None) -> str:
    """For display in admin UIs — never echo the real secret back."""
    if not value:
        return ""
    return "••••••••"
