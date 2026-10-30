"""wxauto4 transport adapter for a single Windows PC WeChat 4.x account.

wxauto4 drives the desktop WeChat client through Windows UI Automation. It is
not an official API: the client must be logged in, the desktop session must be
available, and WeChat updates may change the UI tree.

The free ``wxauto4`` build only exposes the polling API
(``ChatWith`` + ``GetAllMessage`` + ``SendMsg``); ``AddListenChat``,
``StartListening`` and ``GetNextNewMessage`` are Plus-only features and the free
build refuses to register more than one listener. This adapter therefore polls
each configured chat in turn, exactly like the pre-4.x transport did, so the
rest of the bot never sees wxauto4 types.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import WeChatChatConfig, WeChatConfig
from app.context_manager import IncomingMessage
from app.errors import ConfigurationError
from app.logging_setup import restore_logging, snapshot_logging
from app.wechat_env import WeChatEnvironment, check_environment

logger = logging.getLogger(__name__)

MessageHandler = Callable[[IncomingMessage], Awaitable[None]]

# WeChat renders media messages as a bracketed placeholder; those are not
# conversational text even when the message type reports "text".
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
# wxauto4 message classes expose both ``attr`` (who produced it) and ``type``
# (payload kind). Self/system rows never belong to the conversation.
_SKIP_ATTRS = frozenset({"self", "system"})
_SKIP_TYPES = frozenset({"system", "time", "recall"})
# "quote" is a text reply that carries a quoted snippet; its content is text.
_TEXT_TYPES = frozenset({"text", "quote"})

# wxauto4 saves downloaded images next to the process cwd by default; keep them
# inside the (gitignored) data directory instead.
_IMAGE_DIR = Path(__file__).resolve().parent.parent / "data" / "images"


def _grab_region(bbox: tuple[int, int, int, int]):
    """Screen capture used when wxauto4 cannot save a received image."""

    from PIL import ImageGrab

    return ImageGrab.grab(bbox=bbox)


def _virtual_screen_rect() -> tuple[int, int, int, int]:
    """(x, y, width, height) of the whole desktop, or zeros when unknown."""

    try:
        import ctypes

        metrics = ctypes.windll.user32.GetSystemMetrics
        return (metrics(76), metrics(77), metrics(78), metrics(79))
    except Exception:
        return (0, 0, 0, 0)


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


def response_error(result: Any) -> str | None:
    """Return a failure description for a wxauto4 ``WxResponse``, else ``None``."""

    if result is None:
        return None
    if isinstance(result, str):
        return result.strip() or "unknown error"

    success = getattr(result, "is_success", None)
    if isinstance(success, bool):
        if success:
            return None
        return _response_message(result)

    if isinstance(result, dict) and "status" in result:
        status = str(result.get("status") or "").strip().lower()
        if status in {"成功", "success", "ok"}:
            return None
        return _response_message(result)
    return None


def _response_message(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("message") or result)
    return str(result)


def _attach_failure_message(exc: Exception, environment: WeChatEnvironment | None) -> str:
    detected = environment.describe() if environment else "环境检测未完成"
    return (
        "wxauto4 无法连接微信 PC 客户端。\n"
        f"当前环境：{detected}\n"
        "请确认：\n"
        "1. 微信 4.1.x 已启动并完成登录；\n"
        "2. 主窗口保持打开、位于当前桌面最前面（wxauto4 初始化和轮询都要点按界面）；\n"
        "3. 桌面会话未锁屏，当前 Windows 用户就是登录微信的用户。\n"
        f"原始错误：{type(exc).__name__}: {exc}"
    )


class WxAutoAdapter:
    def __init__(
        self,
        *,
        config: WeChatConfig,
        handler: MessageHandler,
        client_factory: Callable[[], Any] | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.handler = handler
        self.logger = logger_ or logger
        self._client_factory = client_factory
        self.wx: Any | None = None
        self.bot_name = (config.bot_name or "").strip()
        self.environment: WeChatEnvironment | None = None

        self._chat_by_name = {chat.name: chat for chat in config.chats}
        self._chat_by_id = {
            chat_id_for(config.account_id, chat.name): chat for chat in config.chats
        }
        self._running = False
        # Chats whose current history has been recorded as "already seen".
        self._primed: set[str] = set()
        # Keys of the previous read per chat. New messages are everything after
        # the newest key that is still rendered, which is what keeps repaints
        # from replaying history without blocking a re-sent picture.
        self._prev_keys: dict[str, list[str]] = {}
        self._max_catchup = 10
        # Every WeChat UI interaction (switch chat, read, capture an image,
        # send) mutates the same shared window. Without one lock, a send that
        # switches to chat A can be interleaved with a poll that switches to
        # chat B, and the reply lands in the wrong chat.
        self._ui_lock = threading.RLock()
        # Dispatched message ids with a short memory: a repaint must not create
        # a duplicate, but sending the same picture again minutes later must.
        self._seen_at: dict[int, float] = {}
        self._seen_seconds = 120.0
        self._seen_lock = threading.Lock()
        # Last rendered message count per chat, used to notice a chat whose
        # window stopped rendering (minimised or hidden WeChat).
        self._last_counts: dict[str, int] = {}

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._running

    def _create_client(self) -> Any:
        log_snapshot = snapshot_logging()
        try:
            from wxauto4 import WeChat
            from wxauto4.param import WxParam
        except Exception as exc:
            restore_logging(log_snapshot)
            raise ConfigurationError(
                "wxauto4 未安装或无法导入。\n\n"
                "请执行：\n"
                "pip install wxauto4"
            ) from exc
        # wxauto4 reconfigures the root logger on import; put our handlers back.
        restore_logging(log_snapshot)
        self.environment = check_environment()
        # The free wxauto4 build reports a device fingerprint by default and
        # writes its own log directory; neither is wanted for a local bot.
        WxParam.TELEMETRY_ENABLED = False
        WxParam.ENABLE_FILE_LOGGER = False
        # WeChat 4.x rebuilds message controls when a chat is re-opened, so the
        # runtime ``id`` changes on every switch. ``hash`` is content based and
        # stays stable across those re-renders, which is what dedupe and the
        # polling anchor need.
        WxParam.MESSAGE_HASH = True
        if self.config.capture_images:
            _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            WxParam.DEFAULT_SAVE_PATH = str(_IMAGE_DIR)
        try:
            return WeChat()
        except Exception as exc:
            raise ConfigurationError(
                _attach_failure_message(exc, self.environment)
            ) from exc

    def _init_client(self) -> None:
        if self.wx is None:
            self.wx = (
                self._client_factory() if self._client_factory else self._create_client()
            )
        if not self.bot_name:
            self.bot_name = str(getattr(self.wx, "nickname", "") or "").strip()
        # Validate chat names up front, but take the polling baseline on the
        # first real poll: hashes from the window opened right after startup
        # differ from the ones produced by later ChatWith switches.
        for chat in self.config.chats:
            self._open_chat(chat.name, strict=True)
        self.logger.info(
            "wxauto4_ready account=%s bot=%s chats=%s wechat=%s wxauto4=%s mode=polling",
            self.config.account_id,
            self.bot_name or "unknown",
            ",".join(chat.name for chat in self.config.chats),
            (self.environment.wechat_version if self.environment else None) or "unknown",
            (self.environment.wxauto4_version if self.environment else None) or "unknown",
        )

    def _prime_chat(self, chat: WeChatChatConfig) -> None:
        """Take the baseline once the rendered history stops changing.

        The first read after a chat is opened can see only a partially rendered
        window (e.g. 3 of 11 messages), and the content hashes settle one read
        later. Priming on such a partial view would replay the rest as "new",
        so wait for two consecutive identical reads before recording history.
        """

        previous: list[str] | None = None
        messages: list[Any] = []
        for attempt in range(4):
            messages = self._read_chat(chat.name, strict=attempt == 0)
            keys = [self._raw_message_key(raw) for raw in messages]
            if previous == keys:
                break
            previous = keys
            time.sleep(0.4)

        stable_keys = [self._raw_message_key(raw) for raw in messages]
        self._prev_keys[chat.name] = stable_keys
        self._last_counts[chat.name] = len(messages)
        self._primed.add(chat.name)
        self.logger.info(
            "wxauto4_chat_ready chat=%s history=%d", chat.name, len(messages)
        )

    async def start(self) -> None:
        self._sweep_stale_images()
        self._init_client()
        self._running = True
        try:
            while self._running:
                await self.poll_once()
                await asyncio.sleep(self.config.poll_interval_seconds)
        finally:
            self._running = False

    def _sweep_stale_images(self) -> None:
        """Drop images left behind by a killed process (they are only kept minutes)."""

        if not self.config.capture_images or not _IMAGE_DIR.exists():
            return
        cutoff = time.time() - 3600
        for path in _IMAGE_DIR.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                self.logger.debug("wxauto4_image_sweep_failed path=%s", path)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------ polling
    async def poll_once(self) -> list[IncomingMessage]:
        """Read every configured chat once and dispatch new messages."""

        if self.wx is None:
            self._init_client()
        received: list[IncomingMessage] = []
        unread = self._unread_counts()
        for chat in self._poll_order(unread):
            if chat.name not in self._primed:
                try:
                    await asyncio.to_thread(self._prime_chat, chat)
                except Exception:
                    self.logger.exception("wxauto4_prime_failed chat=%s", chat.name)
                continue

            try:
                messages = await asyncio.to_thread(
                    self._poll_chat_blocking, chat, unread.get(chat.name, 0)
                )
            except Exception:
                self.logger.exception("wxauto4_poll_failed chat=%s", chat.name)
                continue
            for message in messages:
                if self._already_handled(message.message_id):
                    continue
                received.append(message)
                await self._handle_safely(message)
        return received

    def _poll_chat_blocking(
        self, chat: WeChatChatConfig, unread: int
    ) -> list[IncomingMessage]:
        """Read one chat and convert its new messages under the UI lock.

        Conversion happens here (not in the event loop) because image controls
        die as soon as another chat is opened, and because a concurrent send
        would otherwise switch the window away mid-read.
        """

        with self._ui_lock:
            raw_messages = self._read_chat_locked(chat.name, False)
            self._check_rendering(chat.name, len(raw_messages))
            candidates = self._candidates(raw_messages, chat)
            self.logger.debug(
                "wxauto4_poll chat=%s rendered=%d unread=%d new=%d last=%r",
                chat.name,
                len(raw_messages),
                unread,
                len(candidates),
                self._raw_message_key(raw_messages[-1])[:40] if raw_messages else None,
            )
            messages: list[IncomingMessage] = []
            for raw in candidates:
                message = self.to_internal(raw, chat)
                if message is not None:
                    messages.append(message)
            self._prev_keys[chat.name] = [
                self._raw_message_key(raw) for raw in raw_messages
            ]
            return messages

    def _already_handled(self, message_id: int) -> bool:
        """True when this exact message was dispatched moments ago.

        A repaint must not create a duplicate, but re-sending the same picture
        a few minutes later is a real new message and has to get through.
        """

        now = time.monotonic()
        with self._seen_lock:
            for key, seen_at in list(self._seen_at.items()):
                if now - seen_at >= self._seen_seconds:
                    del self._seen_at[key]
            if message_id in self._seen_at:
                return True
            self._seen_at[message_id] = now
            return False

    def _unread_counts(self) -> dict[str, int]:
        """Unread counter per chat name; empty when the session list fails."""

        if self.wx is None:
            return {}
        try:
            with self._ui_lock:
                sessions = self.wx.GetSession()
        except Exception:
            self.logger.debug("wxauto4_session_failed", exc_info=True)
            return {}

        counts: dict[str, int] = {}
        for session in sessions or []:
            name = str(getattr(session, "name", "") or "").strip()
            if not name:
                continue
            try:
                count = int(getattr(session, "new_count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            if count <= 0 and bool(getattr(session, "isnew", False)):
                count = 1
            counts[name] = count
        return counts

    def _poll_order(self, unread: dict[str, int]) -> list[WeChatChatConfig]:
        """Read chats with unread messages first so bursts are not missed."""

        return sorted(
            self.config.chats,
            key=lambda chat: 0 if unread.get(chat.name, 0) > 0 else 1,
        )

    def _check_rendering(self, chat_name: str, rendered: int) -> None:
        previous = self._last_counts.get(chat_name, 0)
        if rendered == 0 and previous > 0:
            self.logger.warning(
                "wxauto4_chat_empty chat=%s previous=%d; the WeChat window is "
                "probably minimised, hidden or occluded",
                chat_name,
                previous,
            )
        self._last_counts[chat_name] = rendered

    def _candidates(self, raw_messages: list[Any], chat: WeChatChatConfig) -> list[Any]:
        """Return only the messages that appeared after the previous read.

        The rendered window always sits at the bottom of the conversation, so
        everything after the newest key we already saw is new. Matching against
        the *previous* window (instead of a long history) keeps a repaint from
        replaying old messages while still letting the same picture be sent
        again later.

        When the current window shares no key with the previous one, the chat
        has scrolled past everything we know and the whole window is newer.
        """

        if not raw_messages:
            return []

        keys = [self._raw_message_key(raw) for raw in raw_messages]
        previous = self._prev_keys.get(chat.name)
        if not previous:
            return list(raw_messages)

        # The window only grew upward (a repaint revealed older history) when
        # the newest message is unchanged and the whole previous window is
        # still the tail of this one.
        last_key = previous[-1]
        if (
            keys[-1] == last_key
            and len(keys) >= len(previous)
            and keys[-len(previous) :] == previous
            and keys.count(last_key) == previous.count(last_key)
        ):
            return []

        # Otherwise the previous window slides in as a prefix of the new one;
        # everything after the overlap is new. Identical content (the same
        # picture sent twice) is still counted correctly this way.
        overlap = 0
        for size in range(min(len(previous), len(keys)), 0, -1):
            if previous[-size:] == keys[:size]:
                overlap = size
                break
        if overlap:
            return raw_messages[overlap:]

        if len(keys) > self._max_catchup:
            self.logger.warning(
                "wxauto4_anchor_gap chat=%s visible=%d, keeping the newest %d",
                chat.name,
                len(keys),
                self._max_catchup,
            )
        else:
            self.logger.info(
                "wxauto4_anchor_gap chat=%s visible=%d", chat.name, len(keys)
            )
        return raw_messages[-self._max_catchup :]

    def _open_chat(self, chat_name: str, *, strict: bool) -> None:
        try:
            target = self.wx.ChatWith(chat_name)
        except Exception as exc:
            if strict:
                raise ConfigurationError(
                    f"wxauto4 无法打开聊天 {chat_name!r}：{type(exc).__name__}: {exc}\n"
                    "请在微信左侧列表中核对完整聊天名称（含空格和表情）。"
                ) from exc
            raise

        # On success wxauto4 returns the chat name as a plain string; failures
        # come back as a WxResponse or None.
        if isinstance(target, str):
            failure = None if target.strip() else "ChatWith 返回空聊天名"
        elif target is None:
            failure = "ChatWith 没有返回聊天名"
        else:
            failure = response_error(target) or "ChatWith 返回了未知结果"

        if not failure:
            return
        if strict:
            raise ConfigurationError(
                f"wxauto4 无法打开聊天 {chat_name!r}：{failure}\n"
                "请在微信左侧列表中核对完整聊天名称（含空格和表情）。"
            )
        raise RuntimeError(f"chat {chat_name!r} unavailable: {failure}")

    def _read_chat(self, chat_name: str, strict: bool) -> list[Any]:
        with self._ui_lock:
            return self._read_chat_locked(chat_name, strict)

    def _read_chat_locked(self, chat_name: str, strict: bool) -> list[Any]:
        self._open_chat(chat_name, strict=strict)
        try:
            messages = self.wx.GetAllMessage()
        except Exception as exc:
            if strict:
                raise ConfigurationError(
                    f"wxauto4 无法读取聊天 {chat_name!r} 的消息：{type(exc).__name__}: {exc}"
                ) from exc
            raise
        return list(messages or [])

    async def _handle_safely(self, message: IncomingMessage) -> None:
        try:
            await self.handler(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "wxauto4_message_handler_failed chat_id=%s user_id=%s",
                message.channel_id,
                message.author_id,
            )

    # --------------------------------------------------------------- conversion
    def to_internal(
        self,
        raw: Any,
        chat: WeChatChatConfig,
        *,
        download_images: bool = True,
    ) -> IncomingMessage | None:
        raw_attr = str(getattr(raw, "attr", "") or "").strip().lower()
        raw_type = str(getattr(raw, "type", "") or "").strip().lower()
        if raw_attr in _SKIP_ATTRS or raw_type in _SKIP_TYPES:
            return None

        author_name = self._author_name(raw)
        if author_name is None:
            return None

        if raw_type == "image":
            # Image controls die as soon as the chat is switched, so the file
            # has to be downloaded now, not later in the pipeline.
            if not self.config.capture_images:
                return None
            if not download_images:
                # Priming only records history as seen; it must not fetch files.
                return self._build_message(raw, chat, author_name, "[图片]", ())
            image_path = self._download_image(raw)
            if image_path is None:
                # Reported instead of dropped: the bot should be able to say it
                # could not read the picture when someone asks about it.
                self.logger.warning(
                    "wxauto4_image_unavailable chat=%s sender=%s",
                    chat.name,
                    author_name,
                )
                return self._build_message(
                    raw, chat, author_name, "[图片]（图片未能获取）", ()
                )
            return self._build_message(
                raw, chat, author_name, "[图片]", (str(image_path),)
            )

        content = getattr(raw, "content", "")
        content = content if isinstance(content, str) else str(content or "")
        content = content.strip()
        if not content:
            return None
        if self.config.text_only:
            if raw_type and raw_type not in _TEXT_TYPES:
                return None
            if content.startswith(_NON_TEXT_PREFIXES):
                return None

        return self._build_message(raw, chat, author_name, content, ())

    def _author_name(self, raw: Any) -> str | None:
        sender = getattr(raw, "sender", "")
        if isinstance(sender, (tuple, list)):
            sender = sender[0] if sender else ""
        sender = str(sender or "").strip()
        if sender.lower() == "self" or (self.bot_name and sender == self.bot_name):
            return None
        remark = str(getattr(raw, "sender_remark", "") or "").strip()
        return sender or remark or "WeChatUser"

    def _download_image(self, raw: Any) -> Path | None:
        try:
            result = raw.download()
        except Exception as exc:
            # The free build fails on images received from other members
            # (component.save() cannot find its dialog); fall back to a
            # screenshot of the rendered message row below.
            self.logger.info(
                "wxauto4_image_download_failed error=%s", type(exc).__name__
            )
            result = None

        if result:
            path = Path(str(result))
            if path.exists():
                self.logger.info(
                    "wxauto4_image_saved source=download path=%s bytes=%d",
                    path.name,
                    path.stat().st_size,
                )
                return path
            self.logger.warning("wxauto4_image_missing path=%s", path)
        return self._screenshot_image(raw)

    def _screenshot_image(self, raw: Any) -> Path | None:
        control = getattr(raw, "control", None)
        rect = getattr(control, "BoundingRectangle", None) if control else None
        if rect is None:
            self.logger.warning("wxauto4_image_capture_skipped reason=no_rect")
            return None
        bbox = (rect.left, rect.top, rect.right, rect.bottom)
        if bbox[2] - bbox[0] < 10 or bbox[3] - bbox[1] < 10:
            self.logger.warning("wxauto4_image_capture_skipped reason=empty_rect")
            return None
        screen = _virtual_screen_rect()
        if screen[2] > 0 and screen[3] > 0:
            left, top, width, height = screen
            if (
                bbox[2] <= left
                or bbox[0] >= left + width
                or bbox[3] <= top
                or bbox[1] >= top + height
            ):
                self.logger.warning(
                    "wxauto4_image_capture_skipped reason=offscreen_rect bbox=%s",
                    bbox,
                )
                return None

        try:
            image = _grab_region(bbox)
        except Exception:
            self.logger.warning("wxauto4_image_capture_failed", exc_info=True)
            return None

        try:
            low, high = image.convert("L").getextrema()
        except Exception:
            low, high = 0, 255
        if high - low < 6:
            # An occluded or not-yet-rendered row captures a flat patch; using it
            # would make the vision model describe the wrong thing.
            self.logger.warning(
                "wxauto4_image_capture_skipped reason=blank_region bbox=%s", bbox
            )
            return None

        _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        path = _IMAGE_DIR / (
            f"wxauto_shot_{int(time.time() * 1000)}_{threading.get_ident()}.png"
        )
        try:
            image.save(path)
        except Exception:
            self.logger.warning("wxauto4_image_capture_save_failed", exc_info=True)
            return None
        self.logger.info(
            "wxauto4_image_saved source=screenshot path=%s size=%s",
            path.name,
            getattr(image, "size", "?"),
        )
        return path

    def _build_message(
        self,
        raw: Any,
        chat: WeChatChatConfig,
        author_name: str,
        content: str,
        image_paths: tuple[str, ...],
    ) -> IncomingMessage:
        is_group = chat.kind == "group"
        is_mention = (not is_group) or self._mentions_bot(content)
        mentions_other = is_group and not is_mention and "@" in content
        raw_id_text = self._raw_message_key(raw)

        return IncomingMessage(
            message_id=stable_id(
                "message", self.config.account_id, chat.name, raw_id_text
            ),
            guild_id=stable_id("account", self.config.account_id),
            channel_id=chat_id_for(self.config.account_id, chat.name),
            author_id=user_id_for(self.config.account_id, chat.name, author_name),
            author_name=author_name,
            content=content,
            created_at=datetime.now(timezone.utc),
            is_mention=is_mention,
            mentions_other=mentions_other,
            is_bot=False,
            is_self=False,
            image_paths=image_paths,
        )

    @staticmethod
    def _raw_message_key(raw: Any) -> str:
        """Stable identity based on what the message says.

        Runtime ids change on every re-render, and wxauto4's ``hash_text``
        embeds rendered text metrics, so both change when the window is
        resized. Sender + type + text survives those repaints, which is what
        dedupe and the polling anchor need. The trade-off is that two identical
        messages from the same sender collapse into one.

        Image messages carry a constant content ("图片"), so they also use
        ``Message.hash``: it is a content digest (md5-like) that stayed stable
        across chat switches, and it is what keeps a burst of pictures from the
        same sender distinguishable.
        """

        sender = str(getattr(raw, "sender", "") or "")
        raw_type = str(getattr(raw, "type", "") or "")
        content = str(getattr(raw, "content", "") or "")
        key = f"{sender}|{raw_type}|{content}"
        if raw_type.strip().lower() == "image":
            digest = str(getattr(raw, "hash", "") or "").strip()
            if digest:
                key = f"{key}|{digest}"
        return key

    def _mentions_bot(self, content: str) -> bool:
        if not self.bot_name:
            return False
        # WeChat inserts an @ followed by the display name. Allow whitespace
        # variants used by different client versions.
        return bool(re.search(rf"@\s*{re.escape(self.bot_name)}", content))

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
            self.logger.warning("wxauto4_send_unknown_channel channel_id=%s", channel_id)
            return False
        if self.wx is None:
            self.logger.error("wxauto4_send_before_start")
            return False
        text = (text or "").strip()
        if not text:
            return False
        sent = await asyncio.to_thread(self._send_blocking, chat.name, text)
        if sent and self.config.send_delay_seconds > 0:
            await asyncio.sleep(self.config.send_delay_seconds)
        return sent

    def _send_blocking(self, chat_name: str, text: str) -> bool:
        # Hold the UI lock across "switch to the chat" and "type the message":
        # a poll running in another thread must not switch the window in
        # between, or the text is delivered to the wrong chat.
        with self._ui_lock:
            try:
                result = self.wx.SendMsg(text, who=chat_name, exact=True)
            except Exception:
                self.logger.exception("wxauto4_send_failed chat=%s", chat_name)
                return False
        error = response_error(result)
        if error:
            self.logger.error(
                "wxauto4_send_rejected chat=%s detail=%s", chat_name, error
            )
            return False
        self.logger.info(
            "wxauto4_sent chat=%s chars=%d", chat_name, len(text)
        )
        return True
