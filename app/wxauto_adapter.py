"""wxauto transport adapter for a single Windows PC WeChat account.

wxauto drives the desktop WeChat client through Windows UI Automation. It is
not an official API: the client must be logged in, the desktop session must be
available, and WeChat updates may change the UI tree.

This adapter intentionally targets wxauto 3.9.x, whose API is polling-based:
``AddListenChat`` + ``GetListenMessage`` + ``SendMsg``. Keep all wxauto calls on
the event-loop thread and process messages sequentially; UI automation is not
safe to call concurrently.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.config import WeChatChatConfig, WeChatConfig
from app.context_manager import IncomingMessage
from app.errors import ConfigurationError

logger = logging.getLogger(__name__)

MessageHandler = Callable[[IncomingMessage], Awaitable[None]]

_NON_TEXT_PREFIXES = (
    "[图片]",
    "[文件]",
    "[语音]",
    "[视频]",
    "[链接]",
    "[表情]",
    "[动画表情]",
    "[位置]",
    "[名片]",
    "[小程序]",
    "[聊天记录]",
)
_SKIP_TYPES = {"self", "sys", "time", "recall"}


def stable_id(*parts: str) -> int:
    """Create a deterministic positive 63-bit id from arbitrary text parts."""

    payload = "\x1f".join(parts).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    value = int.from_bytes(digest, "big") & ((1 << 63) - 1)
    return value or 1


def chat_id_for(account_id: str, chat_name: str) -> int:
    return stable_id("chat", account_id, chat_name)


def user_id_for(account_id: str, chat_name: str, author_name: str) -> int:
    return stable_id("user", account_id, chat_name, author_name)


class WxAutoAdapter:
    def __init__(
        self,
        *,
        config: WeChatConfig,
        handler: MessageHandler,
        client_factory: Callable[[], Any] | None = None,
        logger_: logging.Logger | None = None,
        seen_limit: int = 4000,
    ) -> None:
        self.config = config
        self.handler = handler
        self.logger = logger_ or logger
        self._client_factory = client_factory
        self.wx: Any | None = None
        self.bot_name = (config.bot_name or "").strip()
        self._chat_by_id = {
            chat_id_for(config.account_id, chat.name): chat for chat in config.chats
        }
        self._running = False
        self._seen_order: deque[int] = deque()
        self._seen: set[int] = set()
        self._seen_limit = seen_limit

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._running

    def _create_client(self) -> Any:
        try:
            from wxauto import WeChat  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - Windows-only dependency
            raise ConfigurationError(
                "wxauto is not available. Install wxauto on Windows and use "
                "WeChat 3.9.x, or inject a client_factory in tests."
            ) from exc
        try:
            return WeChat()
        except Exception as exc:  # pragma: no cover - requires logged-in WeChat
            raise ConfigurationError(
                "wxauto could not attach to PC WeChat. Ensure WeChat 3.9.x is "
                "running, logged in, and the desktop session is unlocked."
            ) from exc

    def _init_client(self) -> None:
        if self.wx is None:
            self.wx = self._client_factory() if self._client_factory else self._create_client()
        if not self.bot_name:
            self.bot_name = str(getattr(self.wx, "nickname", "") or "").strip()
        for chat in self.config.chats:
            try:
                self.wx.AddListenChat(chat.name)
            except Exception as exc:
                self.logger.exception(
                    "wxauto_listen_failed chat=%s; check the exact chat name", chat.name
                )
                raise ConfigurationError(
                    f"wxauto could not open/listen to chat {chat.name!r}; "
                    "check the exact name in WeChat"
                ) from exc
        self.logger.info(
            "wxauto_ready account=%s bot=%s chats=%s",
            self.config.account_id,
            self.bot_name or "unknown",
            ",".join(chat.name for chat in self.config.chats),
        )

    async def start(self) -> None:
        self._init_client()
        self._running = True
        try:
            while self._running:
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Never let a UI automation hiccup kill the whole bot.
                    self.logger.exception("wxauto_poll_failed")
                await asyncio.sleep(self.config.poll_interval_seconds)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------- polling
    async def poll_once(self) -> list[IncomingMessage]:
        if self.wx is None:
            self._init_client()
        assert self.wx is not None
        received: list[IncomingMessage] = []
        for chat in self.config.chats:
            try:
                raw_messages = self.wx.GetListenMessage(chat.name)
            except Exception:
                self.logger.exception("wxauto_get_messages_failed chat=%s", chat.name)
                continue
            for raw in self._normalize_messages(raw_messages):
                message = self.to_internal(raw, chat)
                if message is None:
                    continue
                if not self._mark_seen(message.message_id):
                    continue
                received.append(message)
                await self._handle_safely(message)
        return received

    @staticmethod
    def _normalize_messages(raw: Any) -> list[Any]:
        if raw is None:
            return []
        if isinstance(raw, dict):
            values: list[Any] = []
            for item in raw.values():
                if isinstance(item, (list, tuple)):
                    values.extend(item)
                elif item is not None:
                    values.append(item)
            return values
        if isinstance(raw, (list, tuple)):
            return list(raw)
        if isinstance(raw, str):
            return []  # wxauto warning/error string
        if hasattr(raw, "content"):
            return [raw]
        return []

    def _mark_seen(self, message_id: int) -> bool:
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        self._seen_order.append(message_id)
        while len(self._seen_order) > self._seen_limit:
            self._seen.discard(self._seen_order.popleft())
        return True

    async def _handle_safely(self, message: IncomingMessage) -> None:
        try:
            await self.handler(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "wxauto_message_handler_failed chat_id=%s user_id=%s",
                message.channel_id,
                message.author_id,
            )

    # --------------------------------------------------------------- conversion
    def to_internal(
        self, raw: Any, chat: WeChatChatConfig
    ) -> IncomingMessage | None:
        raw_type = str(getattr(raw, "type", "") or "").lower()
        if raw_type in _SKIP_TYPES:
            return None

        content = getattr(raw, "content", "")
        content = content if isinstance(content, str) else str(content or "")
        content = content.strip()
        if not content:
            return None
        if self.config.text_only and content.startswith(_NON_TEXT_PREFIXES):
            return None

        sender = getattr(raw, "sender", "")
        if isinstance(sender, (tuple, list)):
            sender = sender[0] if sender else ""
        sender = str(sender or "").strip()
        remark = str(getattr(raw, "sender_remark", "") or "").strip()
        if sender.lower() == "self" or (self.bot_name and sender == self.bot_name):
            return None
        author_name = sender or remark or "WeChatUser"

        is_group = chat.kind == "group"
        is_mention = (not is_group) or self._mentions_bot(content)
        mentions_other = is_group and not is_mention and "@" in content

        raw_id = getattr(raw, "id", None)
        if raw_id is None:
            raw_id = getattr(raw, "info", None)
        raw_id_text = str(raw_id) if raw_id is not None else f"{author_name}|{content}"

        return IncomingMessage(
            message_id=stable_id(
                "message", self.config.account_id, chat.name, raw_id_text
            ),
            guild_id=stable_id("account", self.config.account_id),
            channel_id=chat_id_for(self.config.account_id, chat.name),
            author_id=user_id_for(
                self.config.account_id, chat.name, author_name
            ),
            author_name=author_name,
            content=content,
            created_at=datetime.now(timezone.utc),
            is_mention=is_mention,
            mentions_other=mentions_other,
            is_bot=False,
            is_self=False,
        )

    def _mentions_bot(self, content: str) -> bool:
        if not self.bot_name:
            return False
        # WeChat inserts an @ followed by the display name. Allow whitespace
        # variants used by different client versions.
        return bool(
            re.search(rf"@\s*{re.escape(self.bot_name)}", content)
        )

    # --------------------------------------------------------------------- send
    async def send_reply(
        self,
        *,
        channel_id: int,
        guild_id: int,
        text: str,
        reference_message_id: int | None = None,
    ) -> bool:
        del guild_id, reference_message_id
        chat = self._chat_by_id.get(channel_id)
        if chat is None:
            self.logger.warning("wxauto_send_unknown_channel channel_id=%s", channel_id)
            return False
        if self.wx is None:
            self.logger.error("wxauto_send_before_start")
            return False
        text = (text or "").strip()
        if not text:
            return False
        try:
            self.wx.SendMsg(text, who=chat.name)
            if self.config.send_delay_seconds > 0:
                await asyncio.sleep(self.config.send_delay_seconds)
            return True
        except Exception:
            self.logger.exception("wxauto_send_failed chat=%s", chat.name)
            return False
