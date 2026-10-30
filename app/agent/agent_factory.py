"""Agent factory using LangChain ``create_agent``.

LangChain owns the model/tool loop. This module supplies the model, persona
prompt, per-instance tools, trusted runtime context schema, and a tool-call
budget implemented with the official ``ToolCallLimitMiddleware``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

from app.agent.deepseek_model import assert_tool_calling_model, build_chat_model
from app.agent.prompts import build_system_prompt
from app.config import AgentConfig, ModelConfig
from app.errors import AgentExecutionError, AgentTimeoutError, AgentToolLimitError
from app.tools.context import AgentRuntimeContext
from app.vision import image_content_block

logger = logging.getLogger(__name__)

ModelFactory = Callable[[ModelConfig], BaseChatModel]


@dataclass(frozen=True, slots=True)
class AgentResult:
    text: str
    tool_calls: tuple[str, ...] = field(default_factory=tuple)
    model_calls: int = 0
    elapsed_seconds: float = 0.0


class AgentFactory:
    """Builds and caches exactly one agent for one instance/persona."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        model_config: ModelConfig,
        persona_prompt: str,
        tools: Sequence[Any],
        model_factory: ModelFactory | None = None,
        instance_id: str = "",
        persona_id: str = "",
    ) -> None:
        self.config = config
        self.model_config = model_config
        self.persona_prompt = persona_prompt
        self.tools = list(tools)
        self.instance_id = instance_id
        self.persona_id = persona_id
        self._model_factory = model_factory
        self._agent = None

    def _build_model(self) -> BaseChatModel:
        if self._model_factory is not None:
            return self._model_factory(self.model_config)
        assert_tool_calling_model(self.model_config)
        return build_chat_model(self.model_config)

    @property
    def agent(self):
        if self._agent is None:
            middleware = [
                ToolCallLimitMiddleware(
                    run_limit=self.config.max_tool_rounds,
                    exit_behavior="error",
                )
            ]
            self._agent = create_agent(
                model=self._build_model(),
                tools=self.tools,
                system_prompt=build_system_prompt(self.persona_prompt),
                middleware=middleware,
                context_schema=AgentRuntimeContext,
                name=f"{self.instance_id}-{self.persona_id}",
            )
        return self._agent

    async def run(
        self,
        input_text: str,
        *,
        context: AgentRuntimeContext,
        images: list[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> AgentResult:
        timeout = timeout_seconds or self.config.timeout_seconds
        started = time.perf_counter()
        logger.info(
            "agent_start instance=%s persona=%s guild=%s channel=%s user=%s",
            context.instance_id,
            context.persona_id,
            context.guild_id,
            context.channel_id,
            context.user_id,
        )
        try:
            content: str | list[dict] = input_text
            if images:
                blocks: list[dict] = [{"type": "text", "text": input_text}]
                for path in images:
                    block = image_content_block(path)
                    if block is not None:
                        blocks.append(block)
                if len(blocks) > 1:
                    content = blocks

            result = await asyncio.wait_for(
                self.agent.ainvoke(
                    {"messages": [{"role": "user", "content": content}]},
                    context=context,
                    config={"recursion_limit": 2 * self.config.max_tool_rounds + 6},
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            logger.warning("agent_timeout instance=%s timeout=%.1fs", self.instance_id, timeout)
            raise AgentTimeoutError(f"agent timed out after {timeout:.1f}s") from exc
        except ToolCallLimitExceededError as exc:
            logger.warning(
                "agent_tool_limit instance=%s limit=%s",
                self.instance_id,
                self.config.max_tool_rounds,
            )
            raise AgentToolLimitError(
                f"agent exceeded {self.config.max_tool_rounds} tool calls"
            ) from exc
        except Exception as exc:
            logger.exception("agent_failed instance=%s", self.instance_id)
            raise AgentExecutionError(f"agent failed: {type(exc).__name__}") from exc

        messages = result.get("messages", []) if isinstance(result, dict) else []
        tool_calls: list[str] = []
        model_calls = 0
        for message in messages:
            if isinstance(message, AIMessage):
                model_calls += 1
                tool_calls.extend(call.get("name", "unknown") for call in message.tool_calls)

        text = _final_text(messages)
        elapsed = time.perf_counter() - started
        logger.info(
            "agent_end instance=%s persona=%s duration_ms=%.1f tools=%s model_calls=%s "
            "reply_chars=%s",
            context.instance_id,
            context.persona_id,
            elapsed * 1000,
            ",".join(tool_calls) or "-",
            model_calls,
            len(text),
        )
        if not text:
            raise AgentExecutionError("agent returned an empty final message")
        return AgentResult(
            text=text,
            tool_calls=tuple(tool_calls),
            model_calls=model_calls,
            elapsed_seconds=elapsed,
        )


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    return ""


def _final_text(messages: Sequence[Any]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    if isinstance(last, AIMessage):
        return _content_to_text(last.content)
    return ""
