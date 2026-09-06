"""
Identité : appareils, comptes optionnels, et la notion de « propriétaire ».

Le modèle tient en une phrase : **un appareil est une identité suffisante**.
Il s'enrôle seul au premier lancement et reçoit un jeton porté ; aucun mot de
passe n'est requis pour se servir de l'application.

Un compte est *optionnel*, et ne sert qu'à une chose : retrouver ses groupes
depuis un second appareil. Rattacher un appareil à un compte fait basculer ses
accès vers le compte — c'est ce que résout `owner_of`.

    appareil sans compte  → propriétaire = ('device',  <id appareil>)
    appareil avec compte  → propriétaire = ('account', <id compte>)
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
    """À qui appartiennent les groupes : un compte s'il existe, l'appareil sinon."""

    type: str
    id: str
    device_id: str
    account_id: str | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.type, self.id)


# ── Mots de passe (comptes optionnels) ────────────────────────────────────────


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


# ── Enrôlement ────────────────────────────────────────────────────────────────


def check_signup_key(presented: str | None) -> None:
    """
    Vérifie la clef d'instance, quand il y en a une. Elle ne garde que
    l'enrôlement : une instance publique se ferme ainsi sans écrire d'auth.
    Comparaison à temps constant, par principe.
    """
    if config.SIGNUP_KEY == "":
        return
    if presented is None or not hmac.compare_digest(presented.strip(), config.SIGNUP_KEY):
        raise HTTPException(
            status_code=401,
            detail={"code": "signup_key_invalid", "reason": "Clef d'instance invalide."},
        )


def enrol_device() -> tuple[str, str]:
    """Crée un appareil et renvoie (identifiant, jeton en clair — la seule fois)."""
    device_id = new_id()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    stamp = now()
    db.execute(
        "INSERT INTO device (id, token_hash, account_id, created_at, last_seen_at)"
        " VALUES (?, ?, NULL, ?, ?)",
        (device_id, crypto.hash_token(token), stamp, stamp),
    )
    return device_id, token


# ── Résolution du porteur ─────────────────────────────────────────────────────


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def current_owner(authorization: str | None = Header(default=None)) -> Owner:
    """
    Dépendance FastAPI : jeton porté → propriétaire, ou 401.

    Le jeton est comparé par empreinte : la base ne contient jamais de quoi se
    faire passer pour un appareil.
    """
    token = _bearer(authorization)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthenticated", "reason": "Jeton d'appareil absent."},
        )

    row = db.query_one(
        "SELECT id, account_id FROM device WHERE token_hash = ?", (crypto.hash_token(token),)
    )
    if row is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthenticated", "reason": "Appareil inconnu. Réenrôlez-le."},
        )

    db.execute("UPDATE device SET last_seen_at = ? WHERE id = ?", (now(), row["id"]))
    return owner_of(row["id"], row["account_id"])


def owner_of(device_id: str, account_id: str | None) -> Owner:
    if account_id:
        return Owner(type="account", id=account_id, device_id=device_id, account_id=account_id)
    return Owner(type="device", id=device_id, device_id=device_id, account_id=None)
