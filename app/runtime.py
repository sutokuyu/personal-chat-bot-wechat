"""Per-instance runtime wiring and message pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from app.agent.agent_factory import AgentFactory, AgentResult
from app.agent.deepseek_model import assert_tool_calling_model, build_chat_model
from app.agent.prompts import build_agent_input
from app.config import LoadedInstance
from app.context_manager import ContextManager, IncomingMessage
from app.errors import (
    AgentExecutionError,
    AgentTimeoutError,
    AgentToolLimitError,
)
from app.memory.access_policy import MemoryAccessPolicy
from app.memory.extractor import (
    ExtractionRunner,
    MemoryExtractor,
    StructuredExtractionRunner,
)
from app.memory.repository import BotStateRepository, MemoryRepository
from app.participation_gate import (
    CooldownStore,
    LLMParticipationJudge,
    ParticipationGate,
    ParticipationJudge,
)
from app.storage.database import Database
from app.tools.context import AgentRuntimeContext
from app.tools.registry import build_tools, validate_tool_names
from app.vision import ImageDescriber
from app.wxauto_adapter import WxAutoAdapter, chat_id_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CachedImage:
    """One image kept for follow-up questions, with who posted it and when."""

    path: str
    caption: str
    sender_id: int
    sender_name: str
    message_id: int
    stored_at: float
    posted_at: datetime


class ReplySender(Protocol):
    async def send_reply(
        self,
        *,
        channel_id: int,
        guild_id: int,
        text: str,
        reference_message_id: int | None = None,
    ) -> bool: ...


class AgentRunner(Protocol):
    async def run(
        self,
        input_text: str,
        *,
        context: AgentRuntimeContext,
        images: list[str] | None = None,
    ) -> AgentResult: ...


class BotRuntime:
    """Owns all mutable state for exactly one configured bot instance."""

    def __init__(
        self,
        loaded: LoadedInstance,
        *,
        agent_runner: AgentRunner,
        sender: ReplySender | None = None,
        judge: ParticipationJudge | None = None,
        extraction_runner: ExtractionRunner | None = None,
        database: Database | None = None,
        context_manager: ContextManager | None = None,
        image_describer: ImageDescriber | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.persona = loaded.persona
        self.logger = logger_ or logging.getLogger(f"bot.{self.config.instance_id}")
        self.sender = sender
        self.agent_runner = agent_runner
        self.image_describer = image_describer
        self.database = database or Database(loaded.database_path)
        self.database.initialize()
        self.policy = MemoryAccessPolicy(
            shared_memory_enabled=self.config.memory.allow_shared_memory
        )
        self.repository = MemoryRepository(self.database, self.policy)
        self.state = BotStateRepository(self.database)
        self.context = context_manager or ContextManager(
            max_messages=self.config.context.max_messages,
            track_extraction=self.config.memory.enabled,
        )
        cooldowns = CooldownStore(self.state, self.config.instance_id)
        self.gate = ParticipationGate(
            instance_id=self.config.instance_id,
            participation=self.config.participation,
            reply=self.config.reply,
            context=self.context,
            cooldowns=cooldowns,
            judge=judge,
            allowed_guild_ids=None,
            allowed_channel_ids=None,
            allow_direct_messages=True,
            merge_window_seconds=self.config.context.merge_window_seconds,
        )
        self.extractor = MemoryExtractor(
            instance_id=self.config.instance_id,
            persona_id=self.config.persona_id,
            context=self.context,
            repository=self.repository,
            state=self.state,
            config=self.config.memory,
            runner=extraction_runner,
            logger_=self.logger,
        )
        self._channel_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._retry_tasks: dict[int, asyncio.Task] = {}
        self._retried: set[int] = set()
        self._retried_order: deque[int] = deque()
        # Images of the newest image message per channel, kept briefly so a
        # follow-up like "这张图里是什么" can still be answered.
        self._image_cache: dict[tuple[int, int], list[CachedImage]] = {}
        wechat = self.config.wechat
        self._chat_image_limits: dict[int, int] = {
            chat_id_for(wechat.account_id, chat.name): (
                chat.image_cache_size
                if chat.image_cache_size is not None
                else wechat.image_cache_size
            )
            for chat in wechat.chats
        }

    def set_sender(self, sender: ReplySender) -> None:
        self.sender = sender

    def _lock_for(self, key: tuple[int, int]) -> asyncio.Lock:
        lock = self._channel_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[key] = lock
        return lock

    def _schedule_cooldown_retry(
        self, message: IncomingMessage, retry_after: float
    ) -> None:
        """Re-judge a cooldown-blocked message once instead of dropping it.

        Without this, a user who keeps chatting refreshes the channel cooldown
        and everyone else's messages inside those windows are lost forever.
        """

        reply = self.config.reply
        if not reply.retry_on_cooldown or retry_after <= 0:
            return
        if message.is_bot or message.message_id in self._retried:
            return
        if retry_after > reply.max_cooldown_retry_seconds:
            return

        self._retried.add(message.message_id)
        self._retried_order.append(message.message_id)
        while len(self._retried_order) > 4000:
            self._retried.discard(self._retried_order.popleft())

        delay = retry_after + 0.3
        task = asyncio.create_task(
            self._retry_after_delay(message, delay),
            name=f"cooldown-retry-{message.message_id}",
        )
        self._retry_tasks[message.message_id] = task
        task.add_done_callback(
            lambda _task, mid=message.message_id: self._retry_tasks.pop(mid, None)
        )
        self.logger.debug(
            "message_retry_scheduled channel=%s message=%s delay=%.1fs",
            message.channel_id,
            message.message_id,
            delay,
        )

    async def _retry_after_delay(self, message: IncomingMessage, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock_for(message.key):
                await self._handle_message_locked(message, reprocess=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "message_retry_failed message=%s", message.message_id
            )

    async def handle_message(self, message: IncomingMessage) -> None:
        self._prune_all_images()
        captions: list[str] = []
        if message.image_paths:
            message, captions = await self._describe_images(message)
            self._retain_images(message, captions)
        key = message.key
        self.context.add_message(message)
        try:
            async with self._lock_for(key):
                await self._handle_message_locked(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "message_pipeline_failed guild=%s channel=%s message=%s",
                message.guild_id,
                message.channel_id,
                message.message_id,
            )
        finally:
            try:
                await self.extractor.maybe_schedule(key)
            except Exception:
                self.logger.exception("extractor_schedule_failed")

    async def _describe_images(
        self, message: IncomingMessage
    ) -> tuple[IncomingMessage, list[str]]:
        """Turn downloaded images into text the gate and transcript can use."""

        captions: list[str] = []
        for path in message.image_paths:
            caption = ""
            if self.image_describer is not None:
                caption = await self.image_describer.describe(path)
            captions.append(caption.strip() or "（图片内容未能识别）")

        joined = "；".join(dict.fromkeys(captions))
        self.logger.info(
            "image_described message=%s images=%d chars=%d",
            message.message_id,
            len(message.image_paths),
            len(joined),
        )
        return replace(message, content=f"[图片] {joined}".strip()), captions

    # ------------------------------------------------------------ image cache
    def _image_limit(self, key: tuple[int, int]) -> int:
        return self._chat_image_limits.get(key[1], self.config.wechat.image_cache_size)

    def _retain_images(self, message: IncomingMessage, captions: list[str]) -> None:
        """Store this message's images (FIFO per chat) for follow-up questions."""

        limit = self._image_limit(message.key)
        if limit <= 0 or not self.config.wechat.image_followup_seconds:
            self._cleanup_images(message)
            return

        entries = self._image_cache.setdefault(message.key, [])
        stored_at = time.monotonic()
        for path, caption in zip(message.image_paths, captions or [""] * len(message.image_paths)):
            entries.append(
                CachedImage(
                    path=path,
                    caption=caption,
                    sender_id=message.author_id,
                    sender_name=message.author_name,
                    message_id=message.message_id,
                    stored_at=stored_at,
                    posted_at=message.created_at,
                )
            )
        while len(entries) > limit:
            evicted = entries.pop(0)
            self._delete_files((evicted.path,))
            self.logger.debug(
                "image_evicted channel=%s message=%s path=%s",
                message.channel_id,
                evicted.message_id,
                Path(evicted.path).name,
            )
        self.logger.info(
            "image_cached channel=%s message=%s added=%d cached=%d limit=%d ttl=%.0fs",
            message.channel_id,
            message.message_id,
            len(message.image_paths),
            len(entries),
            limit,
            self.config.wechat.image_followup_seconds,
        )

    def _fresh_images(self, key: tuple[int, int]) -> list[CachedImage]:
        """Cached images for this chat that are still inside the TTL, oldest first."""

        entries = self._image_cache.get(key)
        if not entries:
            return []
        ttl = self.config.wechat.image_followup_seconds
        now = time.monotonic()
        kept: list[CachedImage] = []
        for entry in entries:
            if ttl <= 0 or now - entry.stored_at > ttl or not Path(entry.path).exists():
                self._delete_files((entry.path,))
            else:
                kept.append(entry)
        if kept:
            self._image_cache[key] = kept
        else:
            self._image_cache.pop(key, None)
        return kept

    def _select_images(
        self, message: IncomingMessage
    ) -> tuple[list[CachedImage], list[CachedImage]]:
        """Pick the images that most likely belong to this follow-up message.

        Images from the same sender win over images from other members, and
        newer images win over older ones. All fresh images are still reported
        in the prompt as context, even when only a few are attached.
        """

        entries = self._fresh_images(message.key)
        if not entries:
            return [], []
        limit = self.config.wechat.image_followup_max_images
        ranked = sorted(
            entries,
            key=lambda entry: (
                entry.sender_id == message.author_id,
                entry.stored_at,
            ),
            reverse=True,
        )[:limit]
        return sorted(ranked, key=lambda entry: entry.stored_at), entries

    def _image_context(self, entries: list[CachedImage], attached: list[CachedImage]) -> str:
        """Prompt block describing the cached images and which ones are attached."""

        if not entries:
            return ""

        def clean(value: str, limit: int = 160) -> str:
            text = " ".join(str(value).split())
            return text.replace("<", "＜").replace(">", "＞")[:limit]

        lines: list[str] = []
        labels: dict[str, str] = {}
        for index, entry in enumerate(entries, start=1):
            label = f"img {index}"
            labels[entry.path] = label
            lines.append(
                f"[{label}] {entry.posted_at.astimezone().strftime('%H:%M')} "
                f"{clean(entry.sender_name, 40)} (user_id={entry.sender_id}): "
                f"{clean(entry.caption)}"
            )
        attached_labels = [
            labels[entry.path] for entry in attached if entry.path in labels
        ]
        footer = (
            "The images attached to this message are, in order: "
            + ", ".join(attached_labels)
            + ". Use them only when the newest message actually refers to a picture."
            if attached_labels
            else "No image is attached to this message; the captions above are all the bot has."
        )
        return "\n".join(lines) + "\n" + footer

    def _prune_all_images(self) -> None:
        for key in list(self._image_cache):
            self._fresh_images(key)

    def _forget_images(self, key: tuple[int, int]) -> None:
        for entry in self._image_cache.pop(key, []):
            self._delete_files((entry.path,))

    def _delete_files(self, paths: tuple[str, ...]) -> None:
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                self.logger.debug("image_cleanup_failed path=%s", path)

    def _cleanup_images(self, message: IncomingMessage) -> None:
        self._delete_files(message.image_paths)

    async def _handle_message_locked(
        self, message: IncomingMessage, *, reprocess: bool = False
    ) -> None:
        # Snowflake ids sort chronologically; ignore messages that arrived while
        # this one was waiting for the per-channel lock.
        recent = [
            item
            for item in self.context.recent(message.key)
            if item.message_id <= message.message_id
        ]
        attached: list[CachedImage] = []
        image_context = ""
        if not message.image_paths:
            attached, cached = self._select_images(message)
            image_context = self._image_context(cached, attached)
        decision = await self.gate.evaluate(
            message,
            recent=recent,
            reprocess=reprocess,
            image_context=image_context,
        )
        if not decision.should_reply:
            self._schedule_cooldown_retry(message, decision.retry_after)
            self.logger.debug(
                "message_skipped instance=%s guild=%s channel=%s message=%s reason=%s",
                self.config.instance_id,
                message.guild_id,
                message.channel_id,
                message.message_id,
                decision.reason,
            )
            return

        self.logger.info(
            "reply_candidate instance=%s guild=%s channel=%s user=%s forced=%s",
            self.config.instance_id,
            message.guild_id,
            message.channel_id,
            message.author_id,
            decision.forced,
        )

        claimed = await self.gate.claim_reply(message)
        if not claimed:
            self.logger.info(
                "message_claim_skipped instance=%s channel=%s message=%s",
                self.config.instance_id,
                message.channel_id,
                message.message_id,
            )
            return

        keep_claim = False
        try:
            trusted_ctx = AgentRuntimeContext(
                instance_id=self.config.instance_id,
                persona_id=self.config.persona_id,
                guild_id=str(message.guild_id),
                channel_id=str(message.channel_id),
                user_id=str(message.author_id),
                user_name=message.author_name,
                message_id=str(message.message_id),
                allow_shared_memory=self.config.memory.allow_shared_memory,
                repository=self.repository,
                policy=self.policy,
                logger=self.logger,
            )
            await asyncio.to_thread(
                self.repository.upsert_user_profile, trusted_ctx.trusted()
            )

            attached: list[CachedImage] = []
            image_context = ""
            if not message.image_paths:
                attached, cached = self._select_images(message)
                image_context = self._image_context(cached, attached)
                if attached:
                    self.logger.info(
                        "image_followup channel=%s message=%s attached=%d cached=%d",
                        message.channel_id,
                        message.message_id,
                        len(attached),
                        len(cached),
                    )
            images = [entry.path for entry in attached] or list(message.image_paths)

            agent_input = build_agent_input(
                recent,
                message,
                merge_window_seconds=self.config.context.merge_window_seconds,
                image_context=image_context,
            )
            try:
                result = await self.agent_runner.run(
                    agent_input,
                    context=trusted_ctx,
                    images=images or None,
                )
            except AgentTimeoutError:
                self.logger.warning(
                    "reply_aborted reason=agent_timeout message=%s", message.message_id
                )
                return
            except AgentToolLimitError:
                self.logger.warning(
                    "reply_aborted reason=tool_limit message=%s", message.message_id
                )
                return
            except AgentExecutionError as exc:
                self.logger.warning(
                    "reply_aborted reason=agent_error detail=%s message=%s",
                    exc,
                    message.message_id,
                )
                return

            text = self._sanitize_reply(result.text)
            if not text:
                text = self.config.reply.empty_reply_fallback
            if self.sender is None:
                self.logger.error("reply_not_sent reason=no_sender")
                return
            sent = await self.sender.send_reply(
                channel_id=message.channel_id,
                guild_id=message.guild_id,
                text=text,
                reference_message_id=message.message_id,
            )
            if sent:
                self.context.add_bot_reply(
                    guild_id=message.guild_id,
                    channel_id=message.channel_id,
                    author_name=self.config.persona_id,
                    content=text,
                )
                keep_claim = True
                await self.gate.record_reply(message)
            self.logger.info(
                "reply_finished instance=%s message=%s sent=%s chars=%s tools=%s",
                self.config.instance_id,
                message.message_id,
                sent,
                len(text),
                ",".join(result.tool_calls) or "-",
            )
        finally:
            if not keep_claim:
                try:
                    await self.gate.release_reply(message)
                except Exception:
                    self.logger.exception("message_claim_release_failed")

    def _sanitize_reply(self, text: str) -> str:
        text = (text or "").strip()
        if text.startswith("[") and text.endswith("]"):
            # Some models echo a speaker prefix; strip a single obvious bracket.
            text = text[1:-1].strip()
        text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
        text = text.replace("@所有人", "@\u200b所有人")
        limit = self.config.reply.max_reply_length
        return text[:limit].rstrip()

    async def close(self) -> None:
        tasks = list(self._retry_tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._retry_tasks.clear()
        for key in list(self._image_cache):
            self._forget_images(key)
        await self.extractor.close()


async def run_wechat(loaded: LoadedInstance, runtime: BotRuntime) -> None:
    adapter = WxAutoAdapter(
        config=loaded.config.wechat,
        handler=runtime.handle_message,
        logger_=runtime.logger,
    )
    runtime.set_sender(adapter)
    try:
        await adapter.start()
    finally:
        adapter.stop()
        await runtime.close()


def create_runtime(
    loaded: LoadedInstance,
    *,
    model_factory: Callable | None = None,
    judge: ParticipationJudge | None = None,
    extraction_runner: ExtractionRunner | None = None,
    database: Database | None = None,
    sender: ReplySender | None = None,
    image_describer: ImageDescriber | None = None,
) -> BotRuntime:
    """Wire production dependencies; tests inject fakes here."""

    config = loaded.config
    validate_tool_names(config)
    assert_tool_calling_model(config.model)
    tools = build_tools(config)

    agent_runner = AgentFactory(
        config=config.agent,
        model_config=config.model,
        persona_prompt=loaded.persona.prompt,
        tools=tools,
        model_factory=model_factory,
        instance_id=config.instance_id,
        persona_id=config.persona_id,
    )

    if judge is None and config.participation.enabled:
        judge_model = (
            model_factory(config.model)
            if model_factory is not None
            else build_chat_model(config.model, temperature=0.0)
        )
        judge = LLMParticipationJudge(
            judge_model,
            loaded.persona.prompt,
            timeout_seconds=config.participation.timeout_seconds,
            default_on_error=config.participation.default_reply_on_error,
        )

    if extraction_runner is None and config.memory.enabled:
        extraction_model = (
            model_factory(config.model)
            if model_factory is not None
            else build_chat_model(config.model, temperature=0.0)
        )
        extraction_runner = StructuredExtractionRunner(
            extraction_model,
            timeout_seconds=config.memory.extraction_timeout_seconds,
        )

    if image_describer is None and config.wechat.capture_images:
        vision_model = (
            model_factory(config.model)
            if model_factory is not None
            else build_chat_model(config.model, temperature=0.0)
        )
        image_describer = ImageDescriber(vision_model, timeout_seconds=30.0)

    return BotRuntime(
        loaded,
        agent_runner=agent_runner,
        sender=sender,
        judge=judge,
        extraction_runner=extraction_runner,
        database=database,
        image_describer=image_describer,
    )
