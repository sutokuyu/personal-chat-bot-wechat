"""SQLite schema migrations.

The schema version is tracked with ``PRAGMA user_version``. Each migration is
applied in order and is safe to run once per database.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS user_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    interaction_count INTEGER NOT NULL DEFAULT 0 CHECK (interaction_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (guild_id, user_id, persona_id)
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('persona', 'shared')),
    persona_id TEXT,
    category TEXT NOT NULL CHECK (
        category IN ('preference', 'experience', 'plan', 'state', 'relationship', 'other')
    ),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    importance REAL NOT NULL CHECK (importance >= 0.0 AND importance <= 1.0),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deleted', 'expired')),
    CHECK (
        (scope = 'persona' AND persona_id IS NOT NULL)
        OR (scope = 'shared' AND persona_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_memories_user_lookup
    ON memories (guild_id, user_id, status, scope, persona_id);

CREATE INDEX IF NOT EXISTS ix_memories_expiry
    ON memories (status, expires_at);

-- Prevents the same active fact being inserted twice, even by concurrent
-- processes. Updates therefore become idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS ux_memories_active_content
    ON memories (
        guild_id, user_id, scope, COALESCE(persona_id, ''), category, content_hash
    )
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS bot_state (
    instance_id TEXT NOT NULL,
    guild_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    state_key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instance_id, guild_id, channel_id, user_id, state_key)
);

CREATE INDEX IF NOT EXISTS ix_bot_state_updated ON bot_state (updated_at);
"""

_MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _MIGRATION_1),)


def get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations and return the resulting schema version."""

    current = get_schema_version(conn)
    for version, script in _MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")
        current = version
    return current
