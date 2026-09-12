"""
SQLite database: connection, schema, migrations.

The schema is applied through `user_version`, the counter SQLite keeps itself.
Each migration is a function; a shipped migration is never rewritten, a new one
is appended instead.

Core choice: a receipt is stored as **one JSON document** (`document`) flanked
only by the columns the list view needs to sort or filter on. The PWA edits the
receipt as a whole and `settle()` consumes it as a whole; splitting it into
tables would require dozens of patch endpoints for no gain on reads.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import config

_MIGRATIONS: list[str] = []


def migration(sql: str) -> None:
    _MIGRATIONS.append(sql)


# ── v1 — foundation: devices, accounts, groups, receipts, images ──────────────

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

    -- A group's id IS the Tricount invitation code: it is the natural, stable
    -- key, and the one the user pastes from a link.
    CREATE TABLE tricount_group (
        id                TEXT PRIMARY KEY,
        tricount_uuid     TEXT,
        title             TEXT NOT NULL,
        currency          TEXT NOT NULL DEFAULT 'CAD',
        members_json      TEXT NOT NULL DEFAULT '[]',
        members_synced_at TEXT,
        created_at        TEXT NOT NULL
    );

    -- A group is reachable either by a device or by an account. Attaching a
    -- device to an account moves its access over to the account, which is all
    -- it takes for the groups to show up on a second device.
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

    -- Per-owner settings (device or account). The Gemini key is stored here
    -- encrypted, never in the clear, and never comes back out in full.
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


# ── v2 — the server stops holding a readable Gemini key ───────────────────────

migration(
    """
    -- The Gemini key is now encrypted in the browser, with a key derived from
    -- the user's password that never reaches us. What we keep is an opaque
    -- blob: we cannot read it, and we could not hand it back to anyone but its
    -- owner. Losing the password loses the key — that is the point, not a bug.
    ALTER TABLE owner_settings DROP COLUMN gemini_api_key_encrypted;
    ALTER TABLE owner_settings DROP COLUMN gemini_key_hint;
    ALTER TABLE owner_settings ADD COLUMN gemini_key_blob TEXT;

    -- Public salt for the client-side derivation. Not a secret: it only has to
    -- be unique per account, so that the same password on two instances does
    -- not yield the same key.
    ALTER TABLE account ADD COLUMN kdf_salt TEXT;

    -- Instance-scoped values generated on first use. Holds the pepper that
    -- makes an unknown e-mail return a plausible salt rather than a 404 —
    -- otherwise the login flow would answer "does this account exist?".
    CREATE TABLE instance (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """
)


_connection: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    """Single connection for the process, configured once."""
    global _connection
    if _connection is None:
        config.ensure_directories()
        _connection = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        # WAL: reads do not block the write in progress. Useful as soon as two
        # members of a group are browsing while a third one edits.
        _connection.execute("PRAGMA journal_mode = WAL")
        _connection.execute("PRAGMA foreign_keys = ON")
        _connection.execute("PRAGMA busy_timeout = 5000")
        migrate(_connection)
    return _connection


def reset_connection() -> None:
    """Close the connection. Used by the tests to start from a fresh database."""
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None


def migrate(connection: sqlite3.Connection) -> None:
    """Apply the missing migrations, in order, one transaction each."""
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    for index, sql in enumerate(_MIGRATIONS[current:], start=current + 1):
        with connection:
            connection.executescript(sql)
            connection.execute(f"PRAGMA user_version = {index}")


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Transactional block: all or nothing."""
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
    """JSON column → Python object, tolerating an empty or damaged column."""
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except ValueError:
        return fallback


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def instance_value(key: str) -> str:
    """
    Instance-scoped constant, generated on first use and kept afterwards.

    Used for values that must be stable across restarts but that nobody should
    have to configure — a missing one would only be noticed the day it silently
    changed behaviour.
    """
    row = query_one("SELECT value FROM instance WHERE key = ?", (key,))
    if row is not None:
        return row["value"]
    value = secrets.token_hex(32)
    # Another worker may have won the race: keep whatever landed first.
    execute("INSERT OR IGNORE INTO instance (key, value) VALUES (?, ?)", (key, value))
    return query_one("SELECT value FROM instance WHERE key = ?", (key,))["value"]
