"""
Base SQLite : connexion, schéma, migrations.

Le schéma est appliqué par `user_version`, le compteur que SQLite tient lui-même.
Chaque migration est une fonction ; on n'en réécrit jamais une déjà livrée, on en
ajoute une à la suite.

Choix de fond : un ticket est stocké comme **un document JSON** (`document`)
flanqué des seules colonnes que la vue liste doit trier ou filtrer. La PWA édite
le ticket d'un bloc et `settle()` le consomme d'un bloc ; le découper en tables
imposerait des dizaines d'endpoints de patch pour aucun gain de lecture.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import config

_MIGRATIONS: list[str] = []


def migration(sql: str) -> None:
    _MIGRATIONS.append(sql)


# ── v1 — socle : appareils, comptes, groupes, tickets, images ─────────────────

migration(
    """
    CREATE TABLE account (
        id            TEXT PRIMARY KEY,
        email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        created_at    TEXT NOT NULL
    );

    CREATE TABLE device (
        id           TEXT PRIMARY KEY,
        token_hash   TEXT NOT NULL UNIQUE,
        account_id   TEXT REFERENCES account(id) ON DELETE SET NULL,
        created_at   TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    );
    CREATE INDEX device_account ON device(account_id);

    -- L'identifiant d'un groupe EST le code d'invitation Tricount : c'est la
    -- clef naturelle, stable, et celle que l'utilisateur colle depuis un lien.
    CREATE TABLE tricount_group (
        id                TEXT PRIMARY KEY,
        tricount_uuid     TEXT,
        title             TEXT NOT NULL,
        currency          TEXT NOT NULL DEFAULT 'CAD',
        members_json      TEXT NOT NULL DEFAULT '[]',
        members_synced_at TEXT,
        created_at        TEXT NOT NULL
    );

    -- Un groupe est accessible soit à un appareil, soit à un compte. Rattacher
    -- un appareil à un compte fait basculer ses accès vers le compte, ce qui
    -- suffit à les rendre visibles depuis un second appareil.
    CREATE TABLE group_access (
        group_id   TEXT NOT NULL REFERENCES tricount_group(id) ON DELETE CASCADE,
        owner_type TEXT NOT NULL CHECK (owner_type IN ('device', 'account')),
        owner_id   TEXT NOT NULL,
        joined_at  TEXT NOT NULL,
        PRIMARY KEY (group_id, owner_type, owner_id)
    );
    CREATE INDEX group_access_owner ON group_access(owner_type, owner_id);

    CREATE TABLE receipt (
        id                    TEXT PRIMARY KEY,
        group_id              TEXT NOT NULL REFERENCES tricount_group(id) ON DELETE CASCADE,
        created_by_type       TEXT NOT NULL,
        created_by_id         TEXT NOT NULL,
        merchant              TEXT,
        purchase_date         TEXT,
        status                TEXT NOT NULL DEFAULT 'draft',
        step                  TEXT NOT NULL DEFAULT 'capture',
        stated_subtotal_cents INTEGER,
        stated_total_cents    INTEGER,
        tip_cents             INTEGER NOT NULL DEFAULT 0,
        tip_basis             TEXT NOT NULL DEFAULT 'subtotal',
        total_cents           INTEGER NOT NULL DEFAULT 0,
        image_id              TEXT,
        document              TEXT NOT NULL,
        version               INTEGER NOT NULL DEFAULT 1,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    );
    CREATE INDEX receipt_group ON receipt(group_id, created_at DESC);

    CREATE TABLE image (
        id         TEXT PRIMARY KEY,
        group_id   TEXT NOT NULL,
        receipt_id TEXT,
        path       TEXT NOT NULL,
        mime       TEXT NOT NULL,
        bytes      INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX image_receipt ON image(receipt_id);

    -- Réglages par propriétaire (appareil ou compte). La clef Gemini y est
    -- stockée chiffrée, jamais en clair, et n'en ressort jamais entière.
    CREATE TABLE owner_settings (
        owner_type              TEXT NOT NULL,
        owner_id                TEXT NOT NULL,
        gemini_api_key_encrypted TEXT,
        gemini_key_hint         TEXT,
        gemini_model            TEXT,
        updated_at              TEXT NOT NULL,
        PRIMARY KEY (owner_type, owner_id)
    );
    """
)


_connection: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    """Connexion unique du processus, configurée une fois."""
    global _connection
    if _connection is None:
        config.ensure_directories()
        _connection = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        # WAL : les lectures ne bloquent pas l'écriture en cours. Utile dès que
        # deux membres d'un groupe consultent pendant qu'un troisième édite.
        _connection.execute("PRAGMA journal_mode = WAL")
        _connection.execute("PRAGMA foreign_keys = ON")
        _connection.execute("PRAGMA busy_timeout = 5000")
        migrate(_connection)
    return _connection


def reset_connection() -> None:
    """Referme la connexion. Utilisé par les tests pour repartir d'une base neuve."""
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None


def migrate(connection: sqlite3.Connection) -> None:
    """Applique les migrations manquantes, dans l'ordre, une transaction chacune."""
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    for index, sql in enumerate(_MIGRATIONS[current:], start=current + 1):
        with connection:
            connection.executescript(sql)
            connection.execute(f"PRAGMA user_version = {index}")


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Bloc transactionnel : tout ou rien."""
    connection = connect()
    with connection:
        yield connection


def query(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def execute(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
    connection = connect()
    with connection:
        return connection.execute(sql, params)


def loads(raw: str | None, fallback: Any) -> Any:
    """Colonne JSON → objet Python, en tolérant une colonne vide ou abîmée."""
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except ValueError:
        return fallback


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
