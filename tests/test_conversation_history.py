from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.context_manager import ContextManager
from app.storage.conversation_history import ConversationHistoryStore
from tests.conftest import make_database, make_message

KEY = (100, 200)


def test_history_store_round_trips_user_and_bot_messages(tmp_path):
    store = ConversationHistoryStore(make_database(tmp_path))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    user_message = make_message(
        10,
        content="之前聊到哪里了？",
        created_at=start,
        is_mention=True,
        mentions_other=True,
    )
    own_reply = make_message(
        -1,
        content="我们刚才在聊旅行。",
        author_id=0,
        author_name="deepseek-chan",
        is_bot=True,
        is_self=True,
        created_at=start + timedelta(seconds=1),
    )

    store.append(user_message)
    store.append(own_reply)

    restored = store.load(KEY)
    assert [message.content for message in restored] == [
        "之前聊到哪里了？",
        "我们刚才在聊旅行。",
    ]
    assert restored[0].is_mention and restored[0].mentions_other
    assert restored[1].is_bot and restored[1].is_self


def test_history_store_keeps_only_the_newest_messages(tmp_path):
    store = ConversationHistoryStore(make_database(tmp_path), max_per_channel=3)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for offset in range(5):
        store.append(
            make_message(
                offset + 1,
                content=f"message {offset + 1}",
                created_at=start + timedelta(minutes=offset),
            )
        )

    assert [message.content for message in store.load(KEY)] == [
        "message 3",
        "message 4",
        "message 5",
    ]


def test_hydrated_bot_replies_keep_synthetic_ids_unique():
    context = ContextManager()
    previous_reply = make_message(
        -1,
        content="之前的回复",
        author_id=0,
        is_bot=True,
        is_self=True,
    )

    assert context.hydrate_history(KEY, [previous_reply]) == 1
    next_reply = context.add_bot_reply(
        guild_id=KEY[0],
        channel_id=KEY[1],
        author_name="deepseek-chan",
        content="新的回复",
    )

    assert next_reply.message_id == -2
    assert [item.message_id for item in context.recent(KEY)] == [-1, -2]
