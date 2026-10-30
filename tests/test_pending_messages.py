from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.context_manager import ContextManager
from app.memory.models import MemoryScope
from app.storage.pending_messages import PendingMessageStore
from tests.conftest import make_ctx, make_database, make_message, make_repo

KEY = (100, 200)


def _store(tmp_path, *, max_per_channel: int = 200) -> PendingMessageStore:
    return PendingMessageStore(make_database(tmp_path), max_per_channel=max_per_channel)


def test_store_round_trips_messages(tmp_path):
    store = _store(tmp_path)
    first = make_message(10, content="hello")
    second = make_message(11, content="world", author_name="Bob")

    store.append(first)
    store.append(second)

    loaded = store.load(KEY)
    assert [item.content for item in loaded] == ["hello", "world"]
    assert loaded[1].author_name == "Bob"
    assert loaded[0].created_at == first.created_at


def test_store_skips_bot_and_empty_messages(tmp_path):
    store = _store(tmp_path)

    store.append(make_message(10, content="hi"))
    store.append(make_message(11, content="", is_bot=False))
    store.append(make_message(12, content="bot says hi", is_bot=True))

    assert [item.message_id for item in store.load(KEY)] == [10]


def test_store_is_capped_per_channel(tmp_path):
    store = _store(tmp_path, max_per_channel=10)

    for message_id in range(1, 26):
        store.append(make_message(message_id, content=f"m{message_id}"))

    loaded = store.load(KEY)
    assert len(loaded) == 10
    assert loaded[0].message_id == 16, "oldest rows are dropped first"
    assert loaded[-1].message_id == 25


def test_store_discards_processed_messages(tmp_path):
    store = _store(tmp_path)
    store.append(make_message(10, content="a"))
    store.append(make_message(11, content="b"))

    store.discard(KEY, [10])

    assert [item.message_id for item in store.load(KEY)] == [11]


def test_load_can_skip_messages_before_the_cursor(tmp_path):
    store = _store(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for offset, message_id in enumerate((10, 11, 12)):
        store.append(
            make_message(
                message_id,
                content=f"m{message_id}",
                created_at=base + timedelta(minutes=offset),
            )
        )

    loaded = store.load(KEY, after=base + timedelta(minutes=1))

    assert [item.message_id for item in loaded] == [12]


async def test_store_orders_by_time_not_message_id(tmp_path):
    # Message ids are content hashes, so id order says nothing about chat order.
    store = _store(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.append(make_message(999, content="first", created_at=base))
    store.append(
        make_message(1, content="second", created_at=base + timedelta(minutes=1))
    )

    assert [item.content for item in store.load(KEY)] == ["first", "second"]


async def test_context_persists_and_hydrates_across_restarts(tmp_path):
    store = _store(tmp_path)
    first_process = ContextManager(track_extraction=True, pending_store=store)
    for message_id in (1, 2, 3):
        first_process.add_message(make_message(message_id, content=f"m{message_id}"))

    # A new process starts with an empty in-memory buffer.
    restarted = ContextManager(track_extraction=True, pending_store=store)
    assert restarted.pending_extraction_count(KEY) == 0

    restored = restarted.hydrate_extraction_buffer(KEY, store.load(KEY))

    assert restored == 3
    assert restarted.pending_extraction_count(KEY) == 3


async def test_context_hydration_ignores_duplicates_and_keeps_order(tmp_path):
    store = _store(tmp_path)
    context = ContextManager(track_extraction=True, pending_store=store)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    context.add_message(
        make_message(2, content="second", created_at=base + timedelta(minutes=1))
    )

    loaded = context.hydrate_extraction_buffer(
        KEY,
        [
            make_message(999, content="first", created_at=base),
            make_message(2, content="second", created_at=base + timedelta(minutes=1)),
        ],
    )

    assert loaded == 1
    batch = context.take_extraction_batch(KEY, threshold=2, max_size=5)
    assert [item.content for item in batch] == ["first", "second"]


async def test_drop_extracted_clears_memory_and_storage(tmp_path):
    store = _store(tmp_path)
    context = ContextManager(track_extraction=True, pending_store=store)
    for message_id in (1, 2):
        context.add_message(make_message(message_id, content=f"m{message_id}"))

    context.drop_extracted(KEY, [1])

    assert context.pending_extraction_count(KEY) == 1
    assert [item.message_id for item in store.load(KEY)] == [2]


async def test_extraction_survives_a_restart_through_the_threshold(tmp_path):
    """The bug behind "no memories from this group": restarts reset the counter."""

    store = _store(tmp_path)
    # Two messages before the restart, three after: below the threshold on
    # either side, enough together.
    first = ContextManager(track_extraction=True, pending_store=store)
    for message_id in (1, 2):
        first.add_message(make_message(message_id, content=f"m{message_id}"))

    second = ContextManager(track_extraction=True, pending_store=store)
    second.hydrate_extraction_buffer(KEY, store.load(KEY))
    for message_id in (3, 4, 5):
        second.add_message(make_message(message_id, content=f"m{message_id}"))

    assert second.pending_extraction_count(KEY) == 5
    batch = second.take_extraction_batch(KEY, threshold=5, max_size=10)
    assert [item.message_id for item in batch] == [1, 2, 3, 4, 5]


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

    channel = repo.db.read(
        lambda conn: conn.execute(
            "SELECT channel_id FROM memories WHERE id = ?", (memory.id,)
        ).fetchone()["channel_id"]
    )
    assert channel == "777"
