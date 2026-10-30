from __future__ import annotations

import sqlite3

from app.memory.models import MemoryScope
from app.storage.database import Database
from app.storage.migrations import SCHEMA_VERSION, apply_migrations
from tests.conftest import make_ctx, make_database, make_repo


def _channel_of(db: Database, memory_id: int) -> str:
    row = db.read(
        lambda conn: conn.execute(
            "SELECT channel_id FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
    )
    return str(row["channel_id"])


def test_memories_record_the_chat_they_came_from(tmp_path):
    _db, _policy, repo = make_repo(tmp_path)

    memory = repo.add_memory(
        make_ctx(channel_id="777"),
        scope=MemoryScope.PERSONA,
        category="preference",
        content="Alice likes blue",
        importance=0.5,
        confidence=0.8,
    )

    assert _channel_of(repo.db, memory.id) == "777"


def test_migration_two_backfills_channel_ids_from_bot_state(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    try:
        # Only migration 1, i.e. a database written before channel_id existed.
        from app.storage.migrations import _MIGRATION_1

        raw.executescript(_MIGRATION_1)
        raw.execute("PRAGMA user_version = 1")
        raw.execute(
            """
            INSERT INTO bot_state
                (instance_id, guild_id, channel_id, user_id, state_key, value, updated_at)
            VALUES ('deepseek-chan', '1', 'chan-a', 'user-a', 'last_bot_reply',
                    '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')
            """
        )
        for user_id, content in (("user-a", "likes blue"), ("user-b", "has a cat")):
            raw.execute(
                """
                INSERT INTO memories
                    (guild_id, user_id, scope, persona_id, category, content,
                     content_hash, importance, confidence, created_at, updated_at)
                VALUES ('1', ?, 'persona', 'p', 'preference', ?, ?, 0.5, 0.5,
                        '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')
                """,
                (user_id, content, f"hash-{user_id}"),
            )
        raw.commit()

        assert apply_migrations(raw) == SCHEMA_VERSION

        rows = dict(raw.execute("SELECT user_id, channel_id FROM memories").fetchall())
    finally:
        raw.close()

    assert rows["user-a"] == "chan-a", "known speaker is attributed to its chat"
    assert rows["user-b"] == "", "unknown speaker stays unattributed"


def test_adding_the_channel_column_is_idempotent(tmp_path):
    db = make_database(tmp_path)

    assert db.initialize() == SCHEMA_VERSION
    assert db.initialize() == SCHEMA_VERSION

    columns = db.read(
        lambda conn: [
            row["name"] for row in conn.execute("PRAGMA table_info(memories)")
        ]
    )
    assert columns.count("channel_id") == 1
