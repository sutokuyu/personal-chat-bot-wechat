from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field as PydanticField

from app.agent.agent_factory import AgentResult
from app.config import load_instance
from app.context_manager import IncomingMessage
from app.memory.access_policy import MemoryAccessPolicy
from app.memory.extractor import ExtractionResult
from app.memory.models import ApplyReport, MemoryExtractionOutput, TrustedContext
from app.memory.repository import MemoryRepository
from app.participation_gate import ParticipationDecision
from app.storage.database import Database


class ScriptedChatModel(BaseChatModel):
    """Deterministic chat model that supports LangChain tool binding."""

    responses: list[AIMessage]
    calls: list[list[Any]] = PydanticField(default_factory=list)
    index: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-test-model"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        self.calls.append(list(messages))
        response = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


@dataclass
class FakeAgentRunner:
    result: AgentResult | Exception = field(
        default_factory=lambda: AgentResult(text="hello there")
    )
    calls: list[Any] = field(default_factory=list)

    async def run(
        self, input_text: str, *, context: Any, images: list[str] | None = None
    ) -> AgentResult:
        self.calls.append((input_text, context, images))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@dataclass
class FakeImageDescriber:
    captions: list[str] = field(default_factory=lambda: ["一只橘猫躺在沙发上"])
    paths: list[str] = field(default_factory=list)
    calls: int = 0

    async def describe(self, path: str) -> str:
        self.paths.append(str(path))
        caption = self.captions[min(self.calls, len(self.captions) - 1)]
        self.calls += 1
        return caption


@dataclass
class FakeSender:
    sent: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True

    async def send_reply(self, **kwargs) -> bool:  # noqa: ANN003
        self.sent.append(kwargs)
        return self.ok


@dataclass
class FakeJudge:
    decisions: list[ParticipationDecision] = field(
        default_factory=lambda: [ParticipationDecision(should_reply=True, reason="test")]
    )
    calls: int = 0
    image_contexts: list[str] = field(default_factory=list)

    async def judge(
        self, transcript: str, current: IncomingMessage, **kwargs
    ):
        self.image_contexts.append(kwargs.get("image_context", ""))
        decision = self.decisions[min(self.calls, len(self.decisions) - 1)]
        self.calls += 1
        return decision


@dataclass
class FakeExtractionRunner:
    outputs: list[MemoryExtractionOutput | Exception] = field(default_factory=list)
    prompts: list[tuple[str, str]] = field(default_factory=list)
    calls: int = 0

    async def extract(self, system: str, user: str) -> MemoryExtractionOutput:
        self.prompts.append((system, user))
        output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        if isinstance(output, Exception):
            raise output
        return output


def make_loaded(instance_id: str = "deepseek-chan"):
    return load_instance(instance_id)


def make_database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "bot.db")
    db.initialize()
    return db


def make_repo(
    tmp_path: Path, *, shared: bool = False
) -> tuple[Database, MemoryAccessPolicy, MemoryRepository]:
    db = make_database(tmp_path)
    policy = MemoryAccessPolicy(shared_memory_enabled=shared)
    return db, policy, MemoryRepository(db, policy)


def make_ctx(
    *,
    persona_id: str = "deepseek-chan",
    guild_id: str = "100",
    channel_id: str = "200",
    user_id: str = "300",
    user_name: str = "Alice",
    message_id: str = "1000",
    allow_shared: bool = False,
    instance_id: str = "deepseek-chan",
) -> TrustedContext:
    return TrustedContext(
        instance_id=instance_id,
        persona_id=persona_id,
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        user_name=user_name,
        message_id=message_id,
        allow_shared_memory=allow_shared,
    )


def make_message(
    message_id: int,
    *,
    content: str = "hello",
    guild_id: int = 100,
    channel_id: int = 200,
    author_id: int = 300,
    author_name: str = "Alice",
    is_mention: bool = False,
    mentions_other: bool = False,
    is_bot: bool = False,
    is_self: bool = False,
    created_at: datetime | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        guild_id=guild_id,
        channel_id=channel_id,
        author_id=author_id,
        author_name=author_name,
        content=content,
        created_at=created_at or datetime.now(timezone.utc),
        is_mention=is_mention,
        mentions_other=mentions_other,
        is_bot=is_bot,
        is_self=is_self,
    )


def empty_report() -> ApplyReport:
    return ApplyReport()


def fake_extraction_result(processed: int = 0) -> ExtractionResult:
    return ExtractionResult(processed=processed, report=ApplyReport())
