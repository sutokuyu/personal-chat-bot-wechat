"""Bounded durable short-term history for conversation continuity."""

from __future__ import annotations

import logging
import sqlite3

from app.context_manager import IncomingMessage
from app.storage.database import Database
from app.utils import isoformat, parse_iso

logger = logging.getLogger(__name__)


class ConversationHistoryStore:
    def __init__(self, database: Database, *, max_per_channel: int = 30) -> None:
        self.db = database
        self.max_per_channel = max(1, max_per_channel)

    def append(self, message: IncomingMessage) -> None:
        if not message.content:
            return
        try:
            self.db.write(lambda: self._append(message))
        except Exception:
            logger.warning("conversation_history_persist_failed", exc_info=True)

    def _append(self, message: IncomingMessage) -> None:
        guild_id, channel_id = str(message.guild_id), str(message.channel_id)

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT OR REPLACE INTO conversation_history
                    (guild_id, channel_id, message_id, author_id, author_name,
                     content, created_at, is_mention, mentions_other, is_bot, is_self)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    message.message_id,
                    message.author_id,
                    message.author_name,
                    message.content,
                    isoformat(message.created_at),
                    int(message.is_mention),
                    int(message.mentions_other),
                    int(message.is_bot),
                    int(message.is_self),
                ),
            )
            conn.execute(
                """
                DELETE FROM conversation_history
                WHERE guild_id = ? AND channel_id = ? AND rowid NOT IN (
                    SELECT rowid FROM conversation_history
                    WHERE guild_id = ? AND channel_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                )
                """,
                (
                    guild_id,
                    channel_id,
                    guild_id,
                    channel_id,
                    self.max_per_channel,
                ),
            )

        with self.db.transaction(write=True) as conn:
            operation(conn)

    def load(self, key: tuple[int, int]) -> list[IncomingMessage]:
        guild_id, channel_id = str(key[0]), str(key[1])
        rows = self.db.read(
            lambda conn: conn.execute(
                """
                SELECT message_id, author_id, author_name, content, created_at,
                       is_mention, mentions_other, is_bot, is_self
                FROM (
                    SELECT rowid AS history_order, * FROM conversation_history
                    WHERE guild_id = ? AND channel_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, history_order ASC
                """,
                (guild_id, channel_id, self.max_per_channel),
            ).fetchall()
        )
        return [
            IncomingMessage(
                message_id=int(row["message_id"]),
                guild_id=key[0],
                channel_id=key[1],
                author_id=int(row["author_id"]),
                author_name=str(row["author_name"]),
                content=str(row["content"]),
                created_at=parse_iso(str(row["created_at"])),
                is_mention=bool(row["is_mention"]),
                mentions_other=bool(row["mentions_other"]),
                is_bot=bool(row["is_bot"]),
                is_self=bool(row["is_self"]),
            )
            for row in rows
        ]


__all__ = ["ConversationHistoryStore"]
