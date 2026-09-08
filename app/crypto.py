"""
Fingerprinting. Nothing here is reversible, and that is the whole point.

The service used to encrypt users' Gemini keys at rest, which meant holding
both the ciphertext and the means to read it. It no longer does: the key is
encrypted **in the browser**, with a key derived from the user's password that
never reaches us, and we store an opaque blob. There is nothing left to
decrypt, and therefore no `SPLITTICKET_SECRET_KEY` to protect, rotate, or leak.

What remains is the device token fingerprint — a plain hash, no secret needed.
"""

from __future__ import annotations

import hashlib


def hash_token(token: str) -> str:
    """
    Fingerprint of a device token. We store the fingerprint, not the token: a
    copy of the database must not be enough to impersonate a device.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hint(secret: str) -> str:
    """
    Displayable preview of a key: "AIza…7fQ". Kept for values the server does
    legitimately hold — the instance's own fallback key — never for a user's.
    """
    trimmed = secret.strip()
    if len(trimmed) <= 8:
        return "…"
    return f"{trimmed[:4]}…{trimmed[-3:]}"
