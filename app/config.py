"""
Configuration par variables d'environnement.

Aucune valeur secrète n'a de défaut : une clef absente doit se voir au démarrage
ou produire une erreur explicite, jamais se remplacer silencieusement par une
valeur devinée.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Stockage ──────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("SPLITTICKET_DATA_DIR") or "/data")
DB_PATH = Path(os.environ.get("SPLITTICKET_DB_PATH") or DATA_DIR / "splitticket.sqlite3")
IMAGES_DIR = Path(os.environ.get("SPLITTICKET_IMAGES_DIR") or DATA_DIR / "images")

# Identifiants d'appareil Tricount : réutilisables, donc conservés.
CREDENTIALS_PATH = Path(
    os.environ.get("TRICOUNT_CREDENTIALS_PATH") or DATA_DIR / ".tricount-credentials.json"
)

# ── Accès ─────────────────────────────────────────────────────────────────────

# Clef d'instance, optionnelle. Quand elle est définie, elle n'est exigée que
# pour l'enrôlement d'un appareil : c'est ce qui permet de fermer une instance
# publique sans écrire d'authentification. Héritée de TRICOUNT_RELAY_KEY, dont
# elle reprend le nom en second recours pour ne pas casser les déploiements.
SIGNUP_KEY = (
    os.environ.get("SPLITTICKET_SIGNUP_KEY") or os.environ.get("TRICOUNT_RELAY_KEY") or ""
).strip()

# Clef de chiffrement des clefs Gemini des utilisateurs, au repos. Sans elle, on
# refuse d'en stocker une : mieux vaut un service qui dit non qu'un service qui
# écrit la clef d'un tiers en clair sur disque.
SECRET_KEY = (os.environ.get("SPLITTICKET_SECRET_KEY") or "").strip()

# ── Gemini ────────────────────────────────────────────────────────────────────

# Clef de repli de l'instance. Vide = chaque utilisateur apporte la sienne.
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
GEMINI_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_TIMEOUT_SECONDS") or "60")

# ── Réseau ────────────────────────────────────────────────────────────────────

HOST = os.environ.get("SPLITTICKET_HOST") or os.environ.get("TRICOUNT_RELAY_HOST") or "127.0.0.1"
PORT = int(os.environ.get("SPLITTICKET_PORT") or os.environ.get("TRICOUNT_RELAY_PORT") or "8787")

# Origines autorisées à appeler l'API depuis un navigateur. « * » convient à un
# déploiement personnel : l'authentification est un jeton porté, pas un cookie,
# donc il n'y a pas de CSRF à craindre. Restreindre reste préférable en public.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in (os.environ.get("SPLITTICKET_ALLOWED_ORIGINS") or "*").split(",")
    if origin.strip()
]

# Taille maximale d'une photo de ticket acceptée à l'upload.
MAX_IMAGE_BYTES = int(os.environ.get("SPLITTICKET_MAX_IMAGE_BYTES") or str(12 * 1024 * 1024))

# ── Entretien ─────────────────────────────────────────────────────────────────

# Durée de conservation des photos, en jours. 0 = jamais purger.
# La photo n'est qu'une pièce justificative : les lignes du ticket, elles, ont
# été vérifiées à l'écran et restent. Voir `maintenance.py`.
IMAGE_RETENTION_DAYS = int(os.environ.get("SPLITTICKET_IMAGE_RETENTION_DAYS") or "90")

# Intervalle entre deux passes d'entretien.
PURGE_INTERVAL_SECONDS = int(os.environ.get("SPLITTICKET_PURGE_INTERVAL_SECONDS") or str(24 * 3600))

# ── Contrat ───────────────────────────────────────────────────────────────────

# Version du contrat HTTP, annoncée par /health. Le client la compare à la
# sienne et prévient l'utilisateur en cas d'écart : sans cela, une API et une
# application déployées séparément divergent en silence.
# À incrémenter à chaque changement incompatible de la surface /v1.
CONTRACT_VERSION = "1"


def ensure_directories() -> None:
    """Crée l'arborescence de données. Idempotent, appelé au démarrage."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
