"""
Identity: devices, optional accounts, and the notion of an "owner".

The model fits in one sentence: **a device is identity enough**. It enrols itself
on first launch and receives a bearer token; no password is required to use the
application.

An account is *optional*, and serves one purpose only: finding your groups again
from a second device. Attaching a device to an account moves its access over to
the account — that is what `owner_of` resolves.

    device without account  → owner = ('device',  <device id>)
    device with account     → owner = ('account', <account id>)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Header, HTTPException

from . import config, crypto, db

TOKEN_BYTES = 32
PBKDF2_ROUNDS = 240_000


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return secrets.token_urlsafe(16)


@dataclass(frozen=True)
class Owner:
    """Who the groups belong to: the account if there is one, the device otherwise."""

    type: str
    id: str
    device_id: str
    account_id: str | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.type, self.id)


# ── Passwords (optional accounts) ─────────────────────────────────────────────


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest_hex)


# ── Enrolment ─────────────────────────────────────────────────────────────────


def check_signup_key(presented: str | None) -> None:
    """
    Check the instance key, when there is one. It only guards enrolment: this is
    how a public instance is closed off without writing any auth. Constant-time
    comparison, on principle.
    """
    if config.SIGNUP_KEY == "":
        return
    if presented is None or not hmac.compare_digest(presented.strip(), config.SIGNUP_KEY):
        raise HTTPException(
            status_code=401,
            detail={"code": "signup_key_invalid", "reason": "Invalid instance key."},
        )


def enrol_device() -> tuple[str, str]:
    """Create a device and return (id, plaintext token — the only time it is shown)."""
    device_id = new_id()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    stamp = now()
    db.execute(
        "INSERT INTO device (id, token_hash, account_id, created_at, last_seen_at)"
        " VALUES (?, ?, NULL, ?, ?)",
        (device_id, crypto.hash_token(token), stamp, stamp),
    )
    return device_id, token


# ── Bearer resolution ─────────────────────────────────────────────────────────


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def current_owner(authorization: str | None = Header(default=None)) -> Owner:
    """
    FastAPI dependency: bearer token → owner, or 401.

    The token is compared by hash: the database never holds anything that would
    let someone impersonate a device.
    """
    token = _bearer(authorization)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthenticated", "reason": "Missing device token."},
        )

    row = db.query_one(
        "SELECT id, account_id FROM device WHERE token_hash = ?", (crypto.hash_token(token),)
    )
    if row is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthenticated", "reason": "Unknown device. Enrol it again."},
        )

    db.execute("UPDATE device SET last_seen_at = ? WHERE id = ?", (now(), row["id"]))
    return owner_of(row["id"], row["account_id"])


def owner_of(device_id: str, account_id: str | None) -> Owner:
    if account_id:
        return Owner(type="account", id=account_id, device_id=device_id, account_id=account_id)
    return Owner(type="device", id=device_id, device_id=device_id, account_id=None)
