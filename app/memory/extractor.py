"""Long-term memory extraction.

The extractor is a separate structured-output task, not part of the chat agent.
Model output is untrusted: every candidate is validated and scope-checked in
Python before the repository writes it. Extraction runs in background tasks and
never blocks WeChat message handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.prompts import build_extraction_prompt
from app.config import MemoryConfig
from app.context_manager import ContextManager, IncomingMessage
from app.errors import AppError, MemoryValidationError
from app.memory.access_policy import MemoryAccessPolicy
from app.memory.models import (
    ApplyReport,
    MemoryCandidate,
    MemoryExtractionOutput,
    MemoryOperation,
    MemoryScope,
    TrustedContext,
)
from app.memory.repository import BotStateRepository, MemoryRepository
from app.utils import tokenize

logger = logging.getLogger(__name__)


class ExtractionError(AppError):
    """The extraction model failed or produced unusable output."""


class ExtractionRunner(Protocol):
    async def extract(
        self, system: str, user: str
    ) -> MemoryExtractionOutput: ...


def _extract_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(cleaned[index:])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    raise ExtractionError("model response did not contain a JSON object")


class StructuredExtractionRunner:
    """Uses provider structured output, with a strict JSON fallback."""

    def __init__(
        self, model: BaseChatModel, *, timeout_seconds: float = 90.0
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        try:
            self._structured = model.with_structured_output(MemoryExtractionOutput)
        except Exception:  # pragma: no cover
            self._structured = None

    async def extract(self, system: str, user: str) -> MemoryExtractionOutput:
        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        if self._structured is not None:
            try:
                result = await asyncio.wait_for(
                    self._structured.ainvoke(messages), timeout=self.timeout_seconds
                )
                if isinstance(result, MemoryExtractionOutput):
                    return result
                if isinstance(result, dict):
                    return MemoryExtractionOutput.model_validate(result)
            except Exception as exc:
                logger.warning(
                    "structured_extraction_failed error=%s; trying JSON fallback",
                    type(exc).__name__,
                )
        raw = await asyncio.wait_for(
            self.model.ainvoke(
                messages
                + [
                    HumanMessage(
                        content=(
                            "Return ONLY a JSON object with a 'candidates' array. "
                            "Each candidate needs operation (ADD/UPDATE/IGNORE), "
                            "user_id, scope, category, content, importance, "
                            "confidence and optional target_memory_id/ttl_days."
                        )
                    )
                ]
            ),
            timeout=self.timeout_seconds,
        )
        text = raw.content if isinstance(raw.content, str) else str(raw.content)
        try:
            return MemoryExtractionOutput.model_validate(_extract_json_object(text))
        except Exception as exc:
            raise ExtractionError(f"could not parse extraction output: {exc}") from exc


class MemoryWriteService:
    """Validates model candidates and performs the actual writes."""

    def __init__(
        self,
        repository: MemoryRepository,
        policy: MemoryAccessPolicy,
        config: MemoryConfig,
    ) -> None:
        self.repository = repository
        self.policy = policy
        self.config = config

    def apply_candidates(
        self,
        base_ctx: TrustedContext,
        batch: list[IncomingMessage],
        candidates: Iterable[MemoryCandidate],
    ) -> ApplyReport:
        report = ApplyReport()
        users: dict[str, list[IncomingMessage]] = {}
        user_names: dict[str, str] = {}
        for message in batch:
            user_id = str(message.author_id)
            users.setdefault(user_id, []).append(message)
            user_names[user_id] = message.author_name
        batch_ids = {str(message.message_id): message for message in batch}

        for index, candidate in enumerate(candidates):
            if index >= self.config.max_candidates_per_batch:
                report.rejected += 1
                report.rejected_reasons.append("too many candidates in one batch")
                break
            result = self._apply_one(candidate, base_ctx, users, user_names, batch_ids)
            report = report.merge(result)
        return report

    def _reject(self, report: ApplyReport, reason: str) -> ApplyReport:
        report.rejected += 1
        report.rejected_reasons.append(reason[:160])
        return report

    def _apply_one(
        self,
        candidate: MemoryCandidate,
        base_ctx: TrustedContext,
        users: dict[str, list[IncomingMessage]],
        user_names: dict[str, str],
        batch_ids: dict[str, IncomingMessage],
    ) -> ApplyReport:
        report = ApplyReport()
        if candidate.operation is MemoryOperation.IGNORE:
            report.ignored += 1
            return report

        if not candidate.user_id or candidate.user_id not in users:
            return self._reject(report, "candidate user_id is not in this batch")

        ctx = base_ctx.for_user(candidate.user_id, user_names.get(candidate.user_id))
        content = " ".join(candidate.content.split()).strip()
        if len(content) < 3:
            return self._reject(report, "content too short")
        if len(content) > self.config.max_memory_chars:
            return self._reject(report, "content exceeds max_memory_chars")
        if candidate.importance < self.config.min_importance:
            report.ignored += 1
            return report
        if candidate.confidence < self.config.min_confidence:
            report.ignored += 1
            return report
        if candidate.scope is MemoryScope.SHARED and not (
            self.config.allow_shared_memory and ctx.allow_shared_memory
        ):
            return self._reject(report, "shared memory is not enabled")

        # Attribution guard: the candidate's content must have some lexical
        # overlap with messages actually written by that user in this batch.
        user_messages = users[candidate.user_id]
        user_tokens: set[str] = set()
        for message in user_messages:
            user_tokens |= tokenize(message.content)
        content_tokens = tokenize(content)
        if content_tokens and user_tokens and not (content_tokens & user_tokens):
            return self._reject(report, "content does not match the attributed user's messages")

        latest_user_message = max(user_messages, key=lambda item: item.message_id)
        source_message_id = self._resolve_source_id(
            candidate, latest_user_message, batch_ids
        )
        try:
            if candidate.operation is MemoryOperation.UPDATE:
                return self._apply_update(candidate, ctx, content, source_message_id)
            return self._apply_add(candidate, ctx, content, source_message_id)
        except MemoryValidationError as exc:
            return self._reject(report, f"validation: {exc}")
        except Exception as exc:
            logger.exception("memory_write_failed")
            return self._reject(report, f"storage: {type(exc).__name__}")

    @staticmethod
    def _resolve_source_id(
        candidate: MemoryCandidate,
        user_message: IncomingMessage,
        batch_ids: dict[str, IncomingMessage],
    ) -> str | None:
        if candidate.source_message_id:
            source = batch_ids.get(candidate.source_message_id)
            if source is not None and str(source.author_id) == candidate.user_id:
                return candidate.source_message_id
        return str(user_message.message_id)

    def _apply_add(
        self,
        candidate: MemoryCandidate,
        ctx: TrustedContext,
        content: str,
        source_message_id: str | None,
    ) -> ApplyReport:
        report = ApplyReport()
        persona_id = ctx.persona_id if candidate.scope is MemoryScope.PERSONA else None
        duplicates = self.repository.find_duplicates(
            ctx,
            scope=candidate.scope,
            persona_id=persona_id,
            category=candidate.category.value,
            content=content,
        )
        if duplicates:
            existing = duplicates[0]
            update_kwargs = {
                "content": content,
                "importance": max(existing.importance, candidate.importance),
                "confidence": max(existing.confidence, candidate.confidence),
                "source_message_id": source_message_id,
            }
            if candidate.ttl_days is not None:
                update_kwargs["ttl_days"] = candidate.ttl_days
            self.repository.update_memory(ctx, existing.id, **update_kwargs)
            report.updated += 1
            return report

        self.repository.add_memory(
            ctx,
            scope=candidate.scope,
            category=candidate.category.value,
            content=content,
            importance=candidate.importance,
            confidence=candidate.confidence,
            source_message_id=source_message_id,
            ttl_days=candidate.ttl_days,
        )
        report.added += 1
        return report

    def _apply_update(
        self,
        candidate: MemoryCandidate,
        ctx: TrustedContext,
        content: str,
        source_message_id: str | None,
    ) -> ApplyReport:
        report = ApplyReport()
        assert candidate.target_memory_id is not None
        existing = self.repository.get_memory(ctx, candidate.target_memory_id)
        if existing is None:
            return self._reject(report, "target memory not found in allowed scope")
        if existing.scope is not candidate.scope:
            return self._reject(report, "target memory scope mismatch")
        if existing.user_id != candidate.user_id:
            return self._reject(report, "target memory belongs to another user")
        self.policy.require_modify(ctx, existing)
        update_kwargs = {
            "content": content,
            "category": candidate.category.value,
            "importance": candidate.importance,
            "confidence": candidate.confidence,
            "source_message_id": source_message_id,
        }
        if candidate.ttl_days is not None:
            update_kwargs["ttl_days"] = candidate.ttl_days
        self.repository.update_memory(ctx, existing.id, **update_kwargs)
        report.updated += 1
        return report


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    processed: int
    report: ApplyReport
    skipped: bool = False


class MemoryExtractor:
    def __init__(
        self,
        *,
        instance_id: str,
        persona_id: str,
        context: ContextManager,
        repository: MemoryRepository,
        state: BotStateRepository,
        config: MemoryConfig,
        runner: ExtractionRunner | None,
        logger_: logging.Logger | None = None,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.instance_id = instance_id
        self.persona_id = persona_id
        self.context = context
        self.repository = repository
        self.state = state
        self.config = config
        self.runner = runner
        self.logger = logger_ or logger
        self.retry_delay_seconds = retry_delay_seconds
        self._running: dict[tuple[int, int], asyncio.Task] = {}
        self._attempts: dict[int, int] = {}

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.runner is not None

    async def maybe_schedule(self, key: tuple[int, int]) -> None:
        if not self.enabled or key in self._running:
            return
        if self.context.pending_extraction_count(key) < self.config.extraction_threshold:
            return
        task = asyncio.create_task(self._drain(key), name=f"memory-extract-{key[0]}-{key[1]}")
        self._running[key] = task
        task.add_done_callback(lambda _task, k=key: self._running.pop(k, None))

    async def drain_channel(self, key: tuple[int, int]) -> list[ExtractionResult]:
        """Process all currently available batches (used by tests and shutdown)."""

        results: list[ExtractionResult] = []
        while True:
            batch = self.context.take_extraction_batch(
                key,
                threshold=self.config.extraction_threshold,
                max_size=self.config.extraction_batch_size,
            )
            if batch is None:
                break
            results.append(await self._process_batch(key, batch))
        return results

    async def _drain(self, key: tuple[int, int]) -> None:
        try:
            await self.drain_channel(key)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("memory_extractor_task_failed key=%s", key)

    async def _process_batch(
        self, key: tuple[int, int], batch: list[IncomingMessage]
    ) -> ExtractionResult:
        assert self.runner is not None
        end_id = max(message.message_id for message in batch)
        if self.state.get_extraction_cursor(self.instance_id, str(key[1])) >= end_id:
            return ExtractionResult(processed=0, report=ApplyReport(), skipped=True)

        try:
            output = await self._extract(batch)
            base_ctx = TrustedContext(
                instance_id=self.instance_id,
                persona_id=self.persona_id,
                guild_id=str(key[0]),
                channel_id=str(key[1]),
                user_id=str(batch[-1].author_id),
                user_name=batch[-1].author_name,
                message_id=str(end_id),
                allow_shared_memory=self.config.allow_shared_memory,
            )
            service = MemoryWriteService(self.repository, self.repository.policy, self.config)
            report = await asyncio.to_thread(
                service.apply_candidates, base_ctx, batch, output.candidates
            )
        except asyncio.CancelledError:
            self.context.requeue_extraction_batch(key, batch)
            raise
        except Exception as exc:
            attempts = self._attempts.get(end_id, 0) + 1
            self._attempts[end_id] = attempts
            self.logger.warning(
                "memory_extraction_failed channel=%s attempts=%s error=%s",
                key[1],
                attempts,
                type(exc).__name__,
            )
            if attempts < self.config.extraction_max_attempts:
                self.context.requeue_extraction_batch(key, batch)
                await asyncio.sleep(self.retry_delay_seconds)
                return ExtractionResult(processed=len(batch), report=ApplyReport(), skipped=True)
            # Give up on this batch so a permanently failing model cannot block
            # all future extraction for the channel.
            self.state.advance_extraction_cursor(self.instance_id, str(key[1]), end_id)
            self._attempts.pop(end_id, None)
            return ExtractionResult(processed=len(batch), report=ApplyReport(), skipped=True)

        self.state.advance_extraction_cursor(self.instance_id, str(key[1]), end_id)
        self._attempts.pop(end_id, None)
        self.logger.info(
            "memory_extraction_done channel=%s messages=%s added=%s updated=%s "
            "ignored=%s rejected=%s",
            key[1],
            len(batch),
            report.added,
            report.updated,
            report.ignored,
            report.rejected,
        )
        return ExtractionResult(processed=len(batch), report=report)

    async def _extract(self, batch: list[IncomingMessage]) -> MemoryExtractionOutput:
        assert self.runner is not None
        existing: dict[str, list[dict]] = {}
        seen: set[str] = set()
        for message in batch:
            user_id = str(message.author_id)
            if user_id in seen:
                continue
            seen.add(user_id)
            ctx = TrustedContext(
                instance_id=self.instance_id,
                persona_id=self.persona_id,
                guild_id=str(message.guild_id),
                channel_id=str(message.channel_id),
                user_id=user_id,
                user_name=message.author_name,
                message_id=str(message.message_id),
                allow_shared_memory=self.config.allow_shared_memory,
            )
            memories = self.repository.search_memories(
                ctx, "", limit=20, max_chars=self.config.max_memory_chars
            )
            existing[user_id] = [
                {
                    "id": memory.id,
                    "scope": memory.scope.value,
                    "category": memory.category.value,
                    "content": memory.content,
                    "importance": memory.importance,
                }
                for memory in memories
            ]
        system, user = build_extraction_prompt(
            batch,
            existing,
            allow_shared=self.config.allow_shared_memory,
            max_chars=self.config.max_memory_chars,
        )
        return await self.runner.extract(system, user)

    async def close(self) -> None:
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._running.clear()


def describe_batch(batch: list[IncomingMessage]) -> str:
    """Debug helper that avoids dumping message bodies."""

    return f"{len(batch)} messages {batch[0].message_id}..{batch[-1].message_id}"
