"""
Chiffrement au repos des clefs Gemini que les utilisateurs nous confient.

Conserver la clef d'API d'un tiers est une responsabilité, pas un détail de
stockage. Trois règles tiennent ce module :

 1. la clef n'est jamais écrite en clair sur disque ;
 2. elle n'est jamais renvoyée entière par l'API — seulement un indice de
    quelques caractères, assez pour que son propriétaire la reconnaisse ;
 3. sans `SPLITTICKET_SECRET_KEY`, on refuse d'en stocker une. Un service qui
    dit non vaut mieux qu'un service qui écrit le secret d'autrui en clair.

Fernet vient de `cryptography`, déjà présent comme dépendance de `tricount-api`.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from . import config


class SecretUnavailable(RuntimeError):
    """`SPLITTICKET_SECRET_KEY` absente : on ne peut pas chiffrer, donc on ne stocke pas."""


def _fernet() -> Fernet:
    if config.SECRET_KEY == "":
        raise SecretUnavailable(
            "SPLITTICKET_SECRET_KEY n'est pas définie : impossible de conserver une clef Gemini."
        )
    # La variable d'environnement est une phrase quelconque ; Fernet exige 32
    # octets en base64url. SHA-256 fait le pont de façon déterministe.
    digest = hashlib.sha256(config.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    """Déchiffre, ou None si la valeur est illisible — typiquement après rotation du secret."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, SecretUnavailable, ValueError):
        return None


def hint(secret: str) -> str:
    """
    Aperçu affichable d'une clef : « AIza…7fQ ». Assez pour que son propriétaire
    reconnaisse laquelle il a enregistrée, trop peu pour servir à quiconque.
    """
    trimmed = secret.strip()
    if len(trimmed) <= 8:
        return "…"
    return f"{trimmed[:4]}…{trimmed[-3:]}"


def hash_token(token: str) -> str:
    """
    Empreinte d'un jeton d'appareil. On stocke l'empreinte, pas le jeton : une
    copie de la base ne doit pas suffire à se faire passer pour un appareil.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
