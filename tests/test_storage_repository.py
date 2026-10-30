from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.errors import MemoryAccessDenied
from app.memory.models import MemoryScope
from app.storage.migrations import SCHEMA_VERSION
from app.utils import isoformat, utcnow
from tests.conftest import make_ctx, make_database, make_repo


def test_database_initializes_and_profile_upsert_is_scoped(tmp_path):
    db = make_database(tmp_path)
    assert db.initialize() == SCHEMA_VERSION
    db, policy, repo = make_repo(tmp_path / "second")

    mika_ctx = make_ctx(persona_id="mika")
    ren_ctx = make_ctx(persona_id="ren", instance_id="ren")
    first = repo.upsert_user_profile(mika_ctx)
    second = repo.upsert_user_profile(mika_ctx)
    ren_profile = repo.upsert_user_profile(ren_ctx)

    assert first.interaction_count == 1
    assert second.interaction_count == 2
    assert ren_profile.interaction_count == 1
    assert repo.get_user_profile(mika_ctx).interaction_count == 2
    assert repo.get_user_profile(ren_ctx).interaction_count == 1


def test_memory_add_search_update_delete(tmp_path):
    _db, _policy, repo = make_repo(tmp_path)
    ctx = make_ctx()

    memory = repo.add_memory(
        ctx,
        scope=MemoryScope.PERSONA,
        category="preference",
        content="Alice likes Civilization and strategy games",
        importance=0.8,
        confidence=0.9,
    )
    assert memory.id > 0
    found = repo.search_memories(ctx, "Civilization strategy")
    assert [item.id for item in found] == [memory.id]

    updated = repo.update_memory(
        ctx, memory.id, content="Alice loves Civilization VI", category="preference"
    )
    assert updated.content == "Alice loves Civilization VI"
    assert repo.search_memories(ctx, "Civilization")[0].content.endswith("VI")

    assert repo.delete_memory(ctx, memory.id) is True
    assert repo.get_memory(ctx, memory.id) is None
    assert repo.search_memories(ctx, "Civilization") == []


def test_memory_expiry_filters_expired_rows(tmp_path):
    db, _policy, repo = make_repo(tmp_path)
    ctx = make_ctx()
    memory = repo.add_memory(
        ctx,
        scope=MemoryScope.PERSONA,
        category="plan",
        content="Alice plans to visit Hokkaido",
        importance=0.9,
        confidence=0.9,
        ttl_days=1,
    )
    past = isoformat(utcnow() - timedelta(days=2))
    with db.transaction(write=True) as conn:
        conn.execute("UPDATE memories SET expires_at = ? WHERE id = ?", (past, memory.id))

    assert repo.get_memory(ctx, memory.id) is None
    assert repo.search_memories(ctx, "Hokkaido") == []
    assert repo.expire_memories() == 1


def test_persona_specific_memories_are_strictly_isolated(tmp_path):
    _db, _policy, repo = make_repo(tmp_path)
    mika = make_ctx(persona_id="mika")
    ren = make_ctx(persona_id="ren", instance_id="ren")

    repo.add_memory(
        mika,
        scope=MemoryScope.PERSONA,
        category="experience",
        content="Alice discussed a Hokkaido trip with Mika",
        importance=0.9,
        confidence=0.9,
    )
    assert len(repo.search_memories(mika, "Hokkaido")) == 1
    assert repo.search_memories(ren, "Hokkaido") == []


def test_shared_memory_requires_explicit_permission(tmp_path):
    _db, _policy, repo = make_repo(tmp_path, shared=False)
    ctx = make_ctx(allow_shared=True)
    with pytest.raises(MemoryAccessDenied):
        repo.add_memory(
            ctx,
            scope=MemoryScope.SHARED,
            category="preference",
            content="Alice likes tea",
            importance=0.8,
            confidence=0.8,
        )

    # Global permission enabled, but this invocation is not authorized.
    _db2, _policy2, repo2 = make_repo(tmp_path / "shared", shared=True)
    with pytest.raises(MemoryAccessDenied):
        repo2.add_memory(
            make_ctx(allow_shared=False),
            scope=MemoryScope.SHARED,
            category="preference",
            content="Alice likes tea",
            importance=0.8,
            confidence=0.8,
        )


def test_shared_memory_is_limited_to_guild_and_user(tmp_path):
    _db, _policy, repo = make_repo(tmp_path, shared=True)
    mika = make_ctx(persona_id="mika", allow_shared=True)
    memory = repo.add_memory(
        mika,
        scope=MemoryScope.SHARED,
        category="experience",
        content="Alice attended a music festival",
        importance=0.8,
        confidence=0.8,
    )
    assert memory.persona_id is None

    ren_same_user = make_ctx(
        persona_id="ren", instance_id="ren", allow_shared=True
    )
    assert [item.id for item in repo.search_memories(ren_same_user, "festival")] == [
        memory.id
    ]

    other_guild = make_ctx(
        persona_id="ren",
        instance_id="ren",
        allow_shared=True,
        guild_id="999",
    )
    assert repo.search_memories(other_guild, "festival") == []
    other_user = make_ctx(
        persona_id="ren",
        instance_id="ren",
        allow_shared=True,
        user_id="301",
    )
    assert repo.search_memories(other_user, "festival") == []


def test_duplicate_add_is_idempotent(tmp_path):
    _db, _policy, repo = make_repo(tmp_path)
    ctx = make_ctx()
    first = repo.add_memory(
        ctx,
        scope=MemoryScope.PERSONA,
        category="preference",
        content="Alice likes Civ",
        importance=0.8,
        confidence=0.8,
    )
    second = repo.add_memory(
        ctx,
        scope=MemoryScope.PERSONA,
        category="preference",
        content="  Alice   likes Civ ",
        importance=0.9,
        confidence=0.9,
    )
    assert second.id == first.id
    assert repo.count_active_memories(guild_id="100", user_id="300") == 1


def test_two_personas_can_share_one_sqlite_file(tmp_path):
    db = make_database(tmp_path)
    from app.memory.access_policy import MemoryAccessPolicy
    from app.memory.repository import MemoryRepository

    policy = MemoryAccessPolicy(shared_memory_enabled=False)
    repo = MemoryRepository(db, policy)
    mika = make_ctx(persona_id="mika", instance_id="mika")
    ren = make_ctx(persona_id="ren", instance_id="ren")
    mika_memory = repo.add_memory(
        mika,
        scope=MemoryScope.PERSONA,
        category="preference",
        content="Alice likes Civilization",
        importance=0.8,
        confidence=0.8,
    )
    ren_memory = repo.add_memory(
        ren,
        scope=MemoryScope.PERSONA,
        category="preference",
        content="Alice likes technical films",
        importance=0.8,
        confidence=0.8,
    )
    assert [m.id for m in repo.search_memories(mika, "Civilization")] == [mika_memory.id]
    assert repo.search_memories(mika, "technical films") == []
    assert [m.id for m in repo.search_memories(ren, "technical films")] == [ren_memory.id]
    assert repo.search_memories(ren, "Civilization") == []


def test_purge_user_memories_can_exclude_shared(tmp_path):
    _db, _policy, repo = make_repo(tmp_path, shared=True)
    ctx = make_ctx(allow_shared=True)
    repo.add_memory(
        ctx,
        scope=MemoryScope.PERSONA,
        category="preference",
        content="Alice likes Civ",
        importance=0.8,
        confidence=0.8,
    )
    shared = repo.add_memory(
        ctx,
        scope=MemoryScope.SHARED,
        category="experience",
        content="Alice went to a concert",
        importance=0.8,
        confidence=0.8,
    )
    assert repo.purge_user_memories(guild_id="100", user_id="300", persona_id=ctx.persona_id) == 1
    assert repo.get_memory(ctx, shared.id) is not None
    assert repo.purge_user_memories(
        guild_id="100", user_id="300", persona_id=ctx.persona_id, include_shared=True
    ) == 1


def test_bot_state_and_extraction_cursor_are_instance_scoped(tmp_path):
    db = make_database(tmp_path)
    from app.memory.repository import BotStateRepository

    state = BotStateRepository(db)
    state.set_value("mika", "cooldown_channel", "x", guild_id="1", channel_id="2")
    assert state.get_value("mika", "cooldown_channel", guild_id="1", channel_id="2") == "x"
    assert state.get_value("ren", "cooldown_channel", guild_id="1", channel_id="2") is None

    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = datetime(2026, 1, 2, tzinfo=timezone.utc)

    assert state.advance_extraction_cursor("mika", "2", later) is True
    assert state.advance_extraction_cursor("mika", "2", earlier) is False
    assert state.get_extraction_cursor("mika", "2") == later
    assert state.get_extraction_cursor("ren", "2") is None


def test_cross_instance_message_claim_is_short_lived_and_releasable(tmp_path):
    db = make_database(tmp_path)
    from app.memory.repository import BotStateRepository

    state = BotStateRepository(db)
    assert state.claim_message_reply(
        guild_id="1", channel_id="2", message_id=10, instance_id="mika", ttl_seconds=60
    )
    assert not state.claim_message_reply(
        guild_id="1", channel_id="2", message_id=10, instance_id="ren", ttl_seconds=60
    )
    # A different message can be claimed.
    assert state.claim_message_reply(
        guild_id="1", channel_id="2", message_id=11, instance_id="ren", ttl_seconds=60
    )
    # Only the owner/message matching the claim may release it.
    assert not state.release_message_reply(
        guild_id="1", channel_id="2", message_id=11, instance_id="mika"
    )
    assert state.release_message_reply(
        guild_id="1", channel_id="2", message_id=11, instance_id="ren"
    )


def test_expired_message_claim_can_be_taken_over(tmp_path):
    db = make_database(tmp_path)
    from app.memory.repository import BotStateRepository

    state = BotStateRepository(db)
    state.claim_message_reply(
        guild_id="1", channel_id="2", message_id=10, instance_id="mika", ttl_seconds=0.01
    )
    import time

    time.sleep(0.1)
    assert state.claim_message_reply(
        guild_id="1", channel_id="2", message_id=10, instance_id="ren", ttl_seconds=60
    )
