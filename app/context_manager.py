"""Per-instance, per-channel short-term context and extraction buffers.

Nothing in this module is global: a :class:`ContextManager` belongs to exactly
one bot instance. Histories are memory-only and bounded, so raw channel traffic
is never persisted by default.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Iterable, Protocol

from app.utils import truncate, utcnow

ChannelKey = tuple[int, int]  # (guild_id, channel_id)


class PendingStore(Protocol):
    """Optional durable backing for the extraction buffer."""

    def append(self, message: "IncomingMessage") -> None: ...

    def load(
        self, key: ChannelKey, *, after: datetime | None = None
    ) -> list["IncomingMessage"]: ...

    def discard(self, key: ChannelKey, message_ids: list[int]) -> None: ...


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    message_id: int
    guild_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    created_at: datetime
    is_mention: bool = False
    mentions_other: bool = False
    is_bot: bool = False
    is_self: bool = False
    # Local files downloaded for this message. They only live for the duration
    # of one handling pass and are deleted afterwards.
    image_paths: tuple[str, ...] = ()

    @property
    def key(self) -> ChannelKey:
        return (self.guild_id, self.channel_id)


class ChannelContext:
    def __init__(self, max_messages: int) -> None:
        self.history: Deque[IncomingMessage] = deque(maxlen=max_messages)
        # Bound unprocessed traffic even if the extractor is slow or disabled.
        self.extraction_buffer: Deque[IncomingMessage] = deque(maxlen=2000)


class ContextManager:
    def __init__(
        self,
        *,
        max_messages: int = 30,
        seen_limit: int = 4000,
        track_extraction: bool = True,
        pending_store: "PendingStore | None" = None,
    ) -> None:
        self.max_messages = max_messages
        self.track_extraction = track_extraction
        self.pending_store = pending_store
        self._channels: dict[ChannelKey, ChannelContext] = {}
        self._seen_order: Deque[int] = deque()
        self._seen: set[int] = set()
        self._seen_limit = seen_limit
        self._lock = threading.Lock()
        self._next_synthetic_id = -1

    def _channel(self, key: ChannelKey) -> ChannelContext:
        channel = self._channels.get(key)
        if channel is None:
            channel = ChannelContext(self.max_messages)
            self._channels[key] = channel
        return channel

    def mark_seen(self, message_id: int) -> bool:
        """Return True exactly once for a given message id."""

        with self._lock:
            if message_id in self._seen:
                return False
            self._seen.add(message_id)
            self._seen_order.append(message_id)
            while len(self._seen_order) > self._seen_limit:
                old = self._seen_order.popleft()
                self._seen.discard(old)
            return True

    def add_message(self, message: IncomingMessage) -> None:
        track = self.track_extraction and not message.is_bot
        with self._lock:
            channel = self._channel(message.key)
            channel.history.append(message)
            # Other bots may be treated as chat participants, but their text is
            # not durable user memory. Self-authored messages are filtered by the
            # WeChat adapter before reaching this method.
            if track:
                channel.extraction_buffer.append(message)
        store = self.pending_store if track else None
        if store is not None:
            # Persisted so a restart does not discard progress towards the
            # extraction threshold.
            store.append(message)

    def add_bot_reply(
        self,
        *,
        guild_id: int,
        channel_id: int,
        author_name: str,
        content: str,
        created_at: datetime | None = None,
    ) -> IncomingMessage:
        """Store this bot's own sent reply for conversation continuity.

        Synthetic negative ids cannot collide with platform message ids. These
        messages are part of short-term context but are never extracted into
        long-term memory and never trigger the participation gate.
        """

        with self._lock:
            synthetic_id = self._next_synthetic_id
            self._next_synthetic_id -= 1
            message = IncomingMessage(
                message_id=synthetic_id,
                guild_id=guild_id,
                channel_id=channel_id,
                author_id=0,
                author_name=author_name,
                content=content,
                created_at=created_at or utcnow(),
                is_bot=True,
                is_self=True,
            )
            self._channel(message.key).history.append(message)
            return message

    def recent(self, key: ChannelKey) -> list[IncomingMessage]:
        with self._lock:
            channel = self._channels.get(key)
            return list(channel.history) if channel else []

    def pending_extraction_count(self, key: ChannelKey) -> int:
        with self._lock:
            channel = self._channels.get(key)
            return len(channel.extraction_buffer) if channel else 0

    def take_extraction_batch(
        self, key: ChannelKey, *, threshold: int, max_size: int
    ) -> list[IncomingMessage] | None:
        with self._lock:
            channel = self._channels.get(key)
            if channel is None or len(channel.extraction_buffer) < threshold:
                return None
            count = min(max_size, len(channel.extraction_buffer))
            batch = [channel.extraction_buffer.popleft() for _ in range(count)]
            return batch

    def hydrate_extraction_buffer(
        self, key: ChannelKey, messages: Iterable[IncomingMessage]
    ) -> int:
        """Reload messages persisted by a previous process, oldest first."""

        loaded = 0
        with self._lock:
            channel = self._channel(key)
            known = {item.message_id for item in channel.extraction_buffer}
            merged = list(channel.extraction_buffer)
            for message in messages:
                if message.is_bot or message.message_id in known:
                    continue
                known.add(message.message_id)
                merged.append(message)
                loaded += 1
            merged.sort(key=lambda item: (item.created_at, item.message_id))
            channel.extraction_buffer.clear()
            channel.extraction_buffer.extend(merged)
        return loaded

    def drop_extracted(self, key: ChannelKey, message_ids: list[int]) -> None:
        """Forget messages that have already been mined into long-term memory."""

        ids = set(message_ids)
        with self._lock:
            channel = self._channels.get(key)
            if channel is not None:
                kept = [
                    item
                    for item in channel.extraction_buffer
                    if item.message_id not in ids
                ]
                channel.extraction_buffer.clear()
                channel.extraction_buffer.extend(kept)
            store = self.pending_store
        if store is not None:
            store.discard(key, message_ids)

    def requeue_extraction_batch(
        self, key: ChannelKey, batch: Iterable[IncomingMessage], *, limit: int = 500
    ) -> None:
        with self._lock:
            channel = self._channel(key)
            for message in reversed(list(batch)):
                channel.extraction_buffer.appendleft(message)
            while len(channel.extraction_buffer) > limit:
                channel.extraction_buffer.pop()

    def channel_count(self) -> int:
        with self._lock:
            return len(self._channels)

    def clear(self, key: ChannelKey) -> None:
        with self._lock:
            self._channels.pop(key, None)

    def clear_all(self) -> None:
        with self._lock:
            self._channels.clear()


def _one_line(value: str, *, limit: int = 600) -> str:
    """Keep untrusted content on its own transcript line."""

    cleaned = " ".join(value.split())
    # Prevent user content from forging prompt section boundaries.
    cleaned = cleaned.replace("</transcript>", "&lt;/transcript&gt;")
    cleaned = cleaned.replace("<transcript>", "&lt;transcript&gt;")
    return truncate(cleaned, limit)


def render_transcript(
    messages: Iterable[IncomingMessage],
    *,
    merge_window_seconds: float = 3.0,
    max_line_chars: int = 600,
) -> str:
    """Render messages as untrusted transcript lines, merging quick bursts."""

    lines: list[str] = []
    pending: IncomingMessage | None = None
    pending_parts: list[str] = []

    def flush() -> None:
        nonlocal pending, pending_parts
        if pending is not None:
            marker = " [you]" if pending.is_self else (" [bot]" if pending.is_bot else "")
            lines.append(
                f"[{pending.message_id}] {pending.author_name} "
                f"(user_id={pending.author_id}){marker}: {' '.join(pending_parts)}"
            )
        pending = None
        pending_parts = []

    for message in messages:
        if (
            pending is not None
            and pending.author_id == message.author_id
            and (message.created_at - pending.created_at).total_seconds()
            <= merge_window_seconds
        ):
            pending_parts.append(_one_line(message.content, limit=max_line_chars))
            pending = IncomingMessage(
                message_id=message.message_id,
                guild_id=message.guild_id,
                channel_id=message.channel_id,
                author_id=pending.author_id,
                author_name=pending.author_name,
                content=pending.content,
                created_at=message.created_at,
                is_mention=pending.is_mention or message.is_mention,
                mentions_other=pending.mentions_other or message.mentions_other,
                is_bot=pending.is_bot or message.is_bot,
                is_self=pending.is_self or message.is_self,
            )
            continue
        flush()
        pending = message
        pending_parts = [_one_line(message.content, limit=max_line_chars)]
    flush()
    return "\n".join(lines)
