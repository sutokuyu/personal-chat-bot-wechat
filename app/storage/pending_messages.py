"""Durable queue of messages waiting to be mined for long-term memory.

The in-memory buffer in :class:`~app.context_manager.ContextManager` is bounded
and volatile, so a restart used to discard all progress towards
``extraction_threshold`` and extraction almost never ran. This store keeps the
pending messages on disk until a batch has actually been processed.

Only messages that would enter the extraction buffer are stored: text from
other people, never the bot's own replies. Rows are deleted right after a
successful extraction, and each channel is capped so the table cannot grow
without bound if extraction keeps failing.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from app.context_manager import IncomingMessage
from app.storage.database import Database
from app.utils import isoformat, parse_iso

logger = logging.getLogger(__name__)

_SELECT_COLUMNS = (
    "guild_id, channel_id, message_id, author_id, author_name, content, "
    "created_at, is_bot"
)


class PendingMessageStore:
    def __init__(self, database: Database, *, max_per_channel: int = 200) -> None:
        self.db = database
        self.max_per_channel = max(10, max_per_channel)

    def append(self, message: IncomingMessage) -> None:
        """Persist one message; failures are logged and never break the bot."""

        if message.is_bot or not message.content:
            return
        try:
            self.db.write(lambda: self._append(message))
        except Exception:
            logger.warning("pending_message_persist_failed", exc_info=True)

    def _append(self, message: IncomingMessage) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO pending_messages "
                f"({_SELECT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(message.guild_id),
                    str(message.channel_id),
                    int(message.message_id),
                    int(message.author_id),
                    message.author_name,
                    message.content,
                    isoformat(message.created_at),
                    int(message.is_bot),
                ),
            )
            conn.execute(
                """
                DELETE FROM pending_messages
                WHERE guild_id = ? AND channel_id = ? AND message_id NOT IN (
                    SELECT message_id FROM pending_messages
                    WHERE guild_id = ? AND channel_id = ?
                    ORDER BY created_at DESC, message_id DESC
                    LIMIT ?
                )
                """,
                (
                    str(message.guild_id),
                    str(message.channel_id),
                    str(message.guild_id),
                    str(message.channel_id),
                    self.max_per_channel,
                ),
            )

        with self.db.transaction(write=True) as conn:
            operation(conn)

    def load(
        self, key: tuple[int, int], *, after: datetime | None = None
    ) -> list[IncomingMessage]:
        """Pending messages for one chat, oldest first.

        WeChat message ids are content hashes, so ordering and cursor
        comparisons must use ``created_at``.
        """

        guild_id, channel_id = str(key[0]), str(key[1])
        rows = self.db.read(
            lambda conn: conn.execute(
                f"""
                SELECT {_SELECT_COLUMNS} FROM pending_messages
                WHERE guild_id = ? AND channel_id = ? AND created_at > ?
                ORDER BY created_at ASC, message_id ASC
                """,
                (guild_id, channel_id, isoformat(after) if after else ""),
            ).fetchall()
        )
        messages: list[IncomingMessage] = []
        for row in rows:
            messages.append(
                IncomingMessage(
                    message_id=int(row["message_id"]),
                    guild_id=int(row["guild_id"]),
                    channel_id=int(row["channel_id"]),
                    author_id=int(row["author_id"]),
                    author_name=str(row["author_name"]),
                    content=str(row["content"]),
                    created_at=parse_iso(str(row["created_at"])) or datetime.now(),
                    is_bot=bool(row["is_bot"]),
                )
            )
        return messages

    def discard(self, key: tuple[int, int], message_ids: list[int]) -> None:
        if not message_ids:
            return
        guild_id, channel_id = str(key[0]), str(key[1])

        def operation() -> None:
            with self.db.transaction(write=True) as conn:
                conn.executemany(
                    "DELETE FROM pending_messages "
                    "WHERE guild_id = ? AND channel_id = ? AND message_id = ?",
                    [(guild_id, channel_id, int(mid)) for mid in message_ids],
                )

        try:
            self.db.write(operation)
        except Exception:
            logger.warning("pending_message_discard_failed", exc_info=True)


__all__ = ["PendingMessageStore"]
