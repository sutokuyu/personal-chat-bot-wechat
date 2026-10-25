"""Per-instance runtime wiring and message pipeline."""

from __future__ import annotations

import asyncio
import logging
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
from app.wxauto_adapter import WxAutoAdapter

logger = logging.getLogger(__name__)


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
        self, input_text: str, *, context: AgentRuntimeContext
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
        logger_: logging.Logger | None = None,
    ) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.persona = loaded.persona
        self.logger = logger_ or logging.getLogger(f"bot.{self.config.instance_id}")
        self.sender = sender
        self.agent_runner = agent_runner
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

    def set_sender(self, sender: ReplySender) -> None:
        self.sender = sender

    def _lock_for(self, key: tuple[int, int]) -> asyncio.Lock:
        lock = self._channel_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[key] = lock
        return lock

    async def handle_message(self, message: IncomingMessage) -> None:
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

    async def _handle_message_locked(self, message: IncomingMessage) -> None:
        # Snowflake ids sort chronologically; ignore messages that arrived while
        # this one was waiting for the per-channel lock.
        recent = [
            item
            for item in self.context.recent(message.key)
            if item.message_id <= message.message_id
        ]
        decision = await self.gate.evaluate(message, recent=recent)
        if not decision.should_reply:
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

            agent_input = build_agent_input(
                recent,
                message,
                merge_window_seconds=self.config.context.merge_window_seconds,
            )
            try:
                result = await self.agent_runner.run(agent_input, context=trusted_ctx)
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

    return BotRuntime(
        loaded,
        agent_runner=agent_runner,
        sender=sender,
        judge=judge,
        extraction_runner=extraction_runner,
        database=database,
    )
