"""Participation gate: hard Python rules first, then an optional LLM judgement.

Hard rules cover duplicate messages, bot loops, guild/channel allowlists, and
per-instance cooldowns. Only messages that pass those rules reach the model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agent.prompts import build_participation_prompt
from app.config import ParticipationConfig, ReplyConfig
from app.context_manager import ContextManager, IncomingMessage, render_transcript
from app.memory.repository import BotStateRepository
from app.utils import utcnow

logger = logging.getLogger(__name__)


class ParticipationDecision(BaseModel):
    should_reply: bool
    reason: str = Field(default="", max_length=300)


@dataclass(frozen=True, slots=True)
class GateDecision:
    should_reply: bool
    reason: str
    forced: bool = False
    # Seconds until a cooldown-blocked message may be judged again. 0 means
    # "do not retry"; the runtime turns this into a single delayed retry so a
    # chatty user cannot permanently starve everyone else in the channel.
    retry_after: float = 0.0


class ParticipationJudge(Protocol):
    async def judge(
        self,
        transcript: str,
        current: IncomingMessage,
        *,
        image_context: str = "",
    ) -> ParticipationDecision: ...


_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)


def parse_decision_text(text: str) -> ParticipationDecision:
    """Fallback parser for models that do not honor structured output."""

    match = _JSON_RE.search(text)
    if match:
        try:
            payload = json.loads(match.group(0))
            if isinstance(payload, dict) and "should_reply" in payload:
                return ParticipationDecision(
                    should_reply=bool(payload["should_reply"]),
                    reason=str(payload.get("reason", ""))[:300],
                )
        except (json.JSONDecodeError, ValueError):
            pass
    lowered = text.strip().lower()
    if lowered in {"true", "yes", "reply"}:
        return ParticipationDecision(should_reply=True, reason="fallback parse")
    if lowered in {"false", "no", "silent"}:
        return ParticipationDecision(should_reply=False, reason="fallback parse")
    raise ValueError("could not parse participation decision")


class LLMParticipationJudge:
    def __init__(
        self,
        model: BaseChatModel,
        persona_prompt: str,
        *,
        timeout_seconds: float = 30.0,
        default_on_error: bool = False,
    ) -> None:
        self.model = model
        self.persona_prompt = persona_prompt
        self.timeout_seconds = timeout_seconds
        self.default_on_error = default_on_error
        try:
            self._structured = model.with_structured_output(ParticipationDecision)
        except Exception:  # pragma: no cover - provider without structured output
            self._structured = None

    async def judge(
        self,
        transcript: str,
        current: IncomingMessage,
        *,
        image_context: str = "",
    ) -> ParticipationDecision:
        system, user = build_participation_prompt(
            self.persona_prompt, transcript, current, image_context=image_context
        )
        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        try:
            if self._structured is not None:
                result = await asyncio.wait_for(
                    self._structured.ainvoke(messages), timeout=self.timeout_seconds
                )
                if isinstance(result, ParticipationDecision):
                    return result
                if isinstance(result, dict):
                    return ParticipationDecision.model_validate(result)
                raise ValueError("unexpected structured output type")
            raw = await asyncio.wait_for(
                self.model.ainvoke(messages), timeout=self.timeout_seconds
            )
            text = raw.content if isinstance(raw.content, str) else str(raw.content)
            return parse_decision_text(text)
        except Exception as exc:
            logger.warning(
                "participation_judge_failed error=%s default=%s",
                type(exc).__name__,
                self.default_on_error,
            )
            return ParticipationDecision(
                should_reply=self.default_on_error,
                reason=f"judge error: {type(exc).__name__}",
            )


class CooldownStore:
    """Cooldowns stored in SQLite and namespaced by instance id."""

    _CHANNEL_KEY = "cooldown_channel"
    _USER_KEY = "cooldown_user"
    _LAST_REPLY_KEY = "last_bot_reply"

    def __init__(self, state: BotStateRepository, instance_id: str) -> None:
        self.state = state
        self.instance_id = instance_id

    def _remaining(self, last: datetime | None, seconds: float, now: datetime) -> float:
        if last is None or seconds <= 0:
            return 0.0
        return max(0.0, seconds - (now - last).total_seconds())

    def channel_remaining(
        self, message: IncomingMessage, seconds: float, *, now: datetime | None = None
    ) -> float:
        last = self.state.get_datetime(
            self.instance_id,
            self._CHANNEL_KEY,
            guild_id=str(message.guild_id),
            channel_id=str(message.channel_id),
        )
        return self._remaining(last, seconds, now or utcnow())

    def user_remaining(
        self, message: IncomingMessage, seconds: float, *, now: datetime | None = None
    ) -> float:
        last = self.state.get_datetime(
            self.instance_id,
            self._USER_KEY,
            guild_id=str(message.guild_id),
            user_id=str(message.author_id),
        )
        return self._remaining(last, seconds, now or utcnow())

    def record_reply(self, message: IncomingMessage, *, now: datetime | None = None) -> None:
        from app.utils import isoformat

        timestamp = isoformat(now or utcnow())
        self.state.set_value(
            self.instance_id,
            self._CHANNEL_KEY,
            timestamp,
            guild_id=str(message.guild_id),
            channel_id=str(message.channel_id),
        )
        self.state.set_value(
            self.instance_id,
            self._USER_KEY,
            timestamp,
            guild_id=str(message.guild_id),
            user_id=str(message.author_id),
        )
        self.state.set_value(
            self.instance_id,
            self._LAST_REPLY_KEY,
            timestamp,
            guild_id=str(message.guild_id),
            channel_id=str(message.channel_id),
            user_id=str(message.author_id),
        )

    def is_continuation(
        self,
        message: IncomingMessage,
        window_seconds: float,
        *,
        now: datetime | None = None,
    ) -> bool:
        if window_seconds <= 0:
            return False
        last = self.state.get_datetime(
            self.instance_id,
            self._LAST_REPLY_KEY,
            guild_id=str(message.guild_id),
            channel_id=str(message.channel_id),
            user_id=str(message.author_id),
        )
        if last is None:
            return False
        return (now or utcnow()) - last <= timedelta(seconds=window_seconds)


class ParticipationGate:
    def __init__(
        self,
        *,
        instance_id: str,
        participation: ParticipationConfig,
        reply: ReplyConfig,
        context: ContextManager,
        cooldowns: CooldownStore,
        judge: ParticipationJudge | None,
        allowed_guild_ids: list[int] | None = None,
        allowed_channel_ids: list[int] | None = None,
        allow_direct_messages: bool = False,
        merge_window_seconds: float = 0.0,
        random_fn: Callable[[], float] | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.participation = participation
        self.reply = reply
        self.context = context
        self.cooldowns = cooldowns
        self.judge = judge
        self.allowed_guild_ids = set(allowed_guild_ids or [])
        self.allowed_channel_ids = set(allowed_channel_ids or [])
        self.allow_direct_messages = allow_direct_messages
        self.merge_window_seconds = merge_window_seconds
        self._random = random_fn or random.random

    def allowed_location(self, message: IncomingMessage) -> bool:
        if message.guild_id <= 0:
            return self.allow_direct_messages
        if self.allowed_guild_ids and message.guild_id not in self.allowed_guild_ids:
            return False
        if self.allowed_channel_ids and message.channel_id not in self.allowed_channel_ids:
            return False
        return True

    async def evaluate(
        self,
        message: IncomingMessage,
        *,
        recent: list[IncomingMessage],
        reprocess: bool = False,
        image_context: str = "",
    ) -> GateDecision:
        if message.author_id <= 0:
            return GateDecision(False, "author has no valid id")
        if message.is_bot and not self.participation.reply_to_other_bots:
            return GateDecision(False, "author is a bot without explicit opt-in")
        if not self.allowed_location(message):
            return GateDecision(False, "guild/channel not allowed for this instance")
        if not reprocess and not self.context.mark_seen(message.message_id):
            return GateDecision(False, "duplicate message id")
        if not message.content.strip():
            return GateDecision(False, "empty message")
        if (
            self.participation.ignore_other_mentions
            and message.mentions_other
            and not message.is_mention
        ):
            return GateDecision(False, "message mentions someone else")

        # Prevent an endless bot-to-bot chain unless explicitly allowed. A bot
        # message can still be considered when the preceding message is a human.
        if message.is_bot and not self.participation.allow_consecutive_bot_messages:
            previous = [
                item for item in recent if item.message_id < message.message_id
            ]
            if previous and previous[-1].is_bot:
                return GateDecision(False, "consecutive bot messages")

        continuation = False
        if not message.is_bot:
            continuation = await asyncio.to_thread(
                self.cooldowns.is_continuation,
                message,
                self.reply.continuation_window_seconds,
            )
        channel_cooldown = (
            self.reply.continuation_channel_cooldown_seconds
            if continuation
            else self.reply.channel_cooldown_seconds
        )
        user_cooldown = (
            self.reply.continuation_user_cooldown_seconds
            if continuation
            else self.reply.user_cooldown_seconds
        )
        channel_wait = await asyncio.to_thread(
            self.cooldowns.channel_remaining,
            message,
            channel_cooldown,
        )
        if channel_wait > 0:
            return GateDecision(
                False,
                f"channel cooldown ({channel_wait:.1f}s left)",
                retry_after=channel_wait,
            )
        user_wait = await asyncio.to_thread(
            self.cooldowns.user_remaining,
            message,
            user_cooldown,
        )
        if user_wait > 0:
            return GateDecision(
                False,
                f"user cooldown ({user_wait:.1f}s left)",
                retry_after=user_wait,
            )

        if message.is_mention and self.reply.mention_priority:
            return GateDecision(True, "direct mention", forced=True)
        if continuation and self.participation.enabled:
            return GateDecision(True, "conversation continuation", forced=True)
        if not self.participation.enabled or self.judge is None:
            return GateDecision(False, "participation judgement disabled")
        probability = (
            self.participation.bot_reply_probability
            if message.is_bot
            else self.participation.reply_probability
        )
        if probability <= 0.0:
            return GateDecision(False, "participation probability is zero")

        limited = recent[-self.participation.max_context_messages :]
        transcript = render_transcript(
            limited, merge_window_seconds=self.merge_window_seconds
        )
        decision = await self.judge.judge(
            transcript, message, image_context=image_context
        )
        if not decision.should_reply:
            return GateDecision(False, decision.reason or "model decision")

        # Even a suitable message should not always trigger a reply. Apply the
        # probabilistic throttle after the judge so unsuitable messages stay
        # silent for content reasons, while mentions above bypass it entirely.
        if probability < 1.0:
            draw = self._random()
            if draw >= probability:
                return GateDecision(
                    False,
                    f"participation probability skipped (p={probability:.2f})",
                )
        return GateDecision(True, decision.reason or "model decision")

    async def record_reply(self, message: IncomingMessage) -> None:
        await asyncio.to_thread(self.cooldowns.record_reply, message)

    async def claim_reply(self, message: IncomingMessage) -> bool:
        """Claim this message across persona processes sharing the database."""

        if not self.reply.claim_message_across_instances:
            return True
        return await asyncio.to_thread(
            self.cooldowns.state.claim_message_reply,
            guild_id=str(message.guild_id),
            channel_id=str(message.channel_id),
            message_id=message.message_id,
            instance_id=self.instance_id,
            ttl_seconds=self.reply.message_claim_ttl_seconds,
        )

    async def release_reply(self, message: IncomingMessage) -> None:
        if not self.reply.claim_message_across_instances:
            return
        await asyncio.to_thread(
            self.cooldowns.state.release_message_reply,
            guild_id=str(message.guild_id),
            channel_id=str(message.channel_id),
            message_id=message.message_id,
            instance_id=self.instance_id,
        )
