"""
Configuration through environment variables.

No secret value has a default: a missing key must be visible at startup or raise
an explicit error, never be silently replaced by a guessed value.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Storage ───────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("SPLITTICKET_DATA_DIR") or "/data")
DB_PATH = Path(os.environ.get("SPLITTICKET_DB_PATH") or DATA_DIR / "splitticket.sqlite3")
IMAGES_DIR = Path(os.environ.get("SPLITTICKET_IMAGES_DIR") or DATA_DIR / "images")

# Tricount device credentials: reusable, so they are kept.
CREDENTIALS_PATH = Path(
    os.environ.get("TRICOUNT_CREDENTIALS_PATH") or DATA_DIR / ".tricount-credentials.json"
)

# ── Access ────────────────────────────────────────────────────────────────────

# Optional instance key. When set, it is only required to enrol a device: this is
# what lets a public instance be closed off without writing any authentication.
# Inherited from TRICOUNT_RELAY_KEY, whose name is still read as a fallback so
# existing deployments keep working.
SIGNUP_KEY = (
    os.environ.get("SPLITTICKET_SIGNUP_KEY") or os.environ.get("TRICOUNT_RELAY_KEY") or ""
).strip()

# Key used to encrypt users' Gemini keys at rest. Without it we refuse to store
# one: better a service that says no than a service that writes someone else's
# key to disk in the clear.
SECRET_KEY = (os.environ.get("SPLITTICKET_SECRET_KEY") or "").strip()

# ── Gemini ────────────────────────────────────────────────────────────────────

# Instance fallback key. Empty = every user brings their own.
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
GEMINI_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_TIMEOUT_SECONDS") or "60")

# ── Network ───────────────────────────────────────────────────────────────────

HOST = os.environ.get("SPLITTICKET_HOST") or os.environ.get("TRICOUNT_RELAY_HOST") or "127.0.0.1"
PORT = int(os.environ.get("SPLITTICKET_PORT") or os.environ.get("TRICOUNT_RELAY_PORT") or "8787")

# Origins allowed to call the API from a browser. "*" is fine for a personal
# deployment: authentication is a bearer token, not a cookie, so there is no CSRF
# to worry about. Restricting it is still preferable in public.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in (os.environ.get("SPLITTICKET_ALLOWED_ORIGINS") or "*").split(",")
    if origin.strip()
]

# Maximum size of a receipt photo accepted on upload.
MAX_IMAGE_BYTES = int(os.environ.get("SPLITTICKET_MAX_IMAGE_BYTES") or str(12 * 1024 * 1024))

# ── Housekeeping ──────────────────────────────────────────────────────────────

# How long photos are kept, in days. 0 = never purge.
# The photo is only supporting evidence: the receipt lines themselves have been
# checked on screen and stay. See `maintenance.py`.
IMAGE_RETENTION_DAYS = int(os.environ.get("SPLITTICKET_IMAGE_RETENTION_DAYS") or "90")

# Interval between two housekeeping passes.
PURGE_INTERVAL_SECONDS = int(os.environ.get("SPLITTICKET_PURGE_INTERVAL_SECONDS") or str(24 * 3600))

# ── Contract ──────────────────────────────────────────────────────────────────

# HTTP contract version, announced by /health. The client compares it to its own
# and warns the user on a mismatch: without it, an API and an app deployed
# separately drift apart silently.
# Bump it on every breaking change to the /v1 surface.
CONTRACT_VERSION = "1"


def ensure_directories() -> None:
    """Create the data tree. Idempotent, called at startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
