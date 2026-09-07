"""
Encryption at rest for the Gemini keys users entrust to us.

Holding someone else's API key is a responsibility, not a storage detail. Three
rules hold this module together:

 1. the key is never written to disk in the clear;
 2. it is never returned in full by the API — only a hint of a few characters,
    enough for its owner to recognise it;
 3. without `SPLITTICKET_SECRET_KEY`, we refuse to store one. A service that says
    no is better than a service that writes someone else's secret in the clear.

Fernet comes from `cryptography`, already a dependency of `tricount-api`.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from . import config


class SecretUnavailable(RuntimeError):
    """`SPLITTICKET_SECRET_KEY` is missing: we cannot encrypt, so we do not store."""


def _fernet() -> Fernet:
    if config.SECRET_KEY == "":
        raise SecretUnavailable(
            "SPLITTICKET_SECRET_KEY is not set: a Gemini key cannot be stored."
        )
    # The environment variable is an arbitrary phrase; Fernet requires 32 bytes
    # in base64url. SHA-256 bridges the two deterministically.
    digest = hashlib.sha256(config.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    """Decrypt, or None if the value is unreadable — typically after a secret rotation."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, SecretUnavailable, ValueError):
        return None


def hint(secret: str) -> str:
    """
    Displayable preview of a key: "AIza…7fQ". Enough for its owner to recognise
    which one they saved, too little to be of use to anyone else.
    """
    trimmed = secret.strip()
    if len(trimmed) <= 8:
        return "…"
    return f"{trimmed[:4]}…{trimmed[-3:]}"


def hash_token(token: str) -> str:
    """
    Fingerprint of a device token. We store the fingerprint, not the token: a
    copy of the database must not be enough to impersonate a device.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
