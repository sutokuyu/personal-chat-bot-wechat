from __future__ import annotations

import json
import logging
import time

import pytest
from langchain.tools import tool
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.agent_factory import AgentFactory
from app.config import AgentConfig
from app.errors import AgentExecutionError, AgentTimeoutError, AgentToolLimitError
from app.memory.models import MemoryScope
from app.tools.context import AgentRuntimeContext
from app.tools.memory_tools import get_user_profile_impl, search_memory_impl
from app.tools.registry import build_tools
from tests.conftest import (
    ScriptedChatModel,
    make_ctx,
    make_loaded,
    make_repo,
)


def _factory(
    tmp_path,
    responses,
    *,
    tools=None,
    max_tool_rounds=3,
    timeout=5.0,
    persona_id="deepseek-chan",
):
    loaded = make_loaded("deepseek-chan")
    db, policy, repo = make_repo(tmp_path)
    model = ScriptedChatModel(responses=responses)
    if tools is None:
        tools = build_tools(loaded.config)
    factory = AgentFactory(
        config=AgentConfig(max_tool_rounds=max_tool_rounds, timeout_seconds=timeout),
        model_config=loaded.config.model,
        persona_prompt=loaded.persona.prompt,
        tools=tools,
        model_factory=lambda _config: model,
        instance_id="deepseek-chan",
        persona_id=persona_id,
    )
    runtime_ctx = AgentRuntimeContext(
        instance_id="deepseek-chan",
        persona_id=persona_id,
        guild_id="100",
        channel_id="200",
        user_id="300",
        user_name="Alice",
        message_id="1000",
        allow_shared_memory=False,
        repository=repo,
        policy=policy,
        logger=logging.getLogger("test.tools"),
    )
    return factory, model, repo, runtime_ctx


def _tool_call(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


async def test_agent_returns_final_answer_without_tools(tmp_path):
    factory, model, _repo, ctx = _factory(
        tmp_path, [AIMessage(content="Just chatting, no tools needed.")]
    )
    result = await factory.run("hello", context=ctx)
    assert result.text == "Just chatting, no tools needed."
    assert result.tool_calls == ()
    assert len(model.calls) == 1


async def test_agent_executes_tool_and_feeds_result_back(tmp_path):
    factory, model, repo, ctx = _factory(
        tmp_path,
        [
            _tool_call("search_memory", {"query": "Civilization"}),
            AIMessage(content="You like Civilization!"),
        ],
    )
    repo.add_memory(
        make_ctx(),
        scope=MemoryScope.PERSONA,
        category="preference",
        content="Alice likes Civilization games",
        importance=0.9,
        confidence=0.9,
    )

    result = await factory.run("what games do I like?", context=ctx)

    assert result.text == "You like Civilization!"
    assert result.tool_calls == ("search_memory",)
    second_call_messages = model.calls[1]
    tool_messages = [m for m in second_call_messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert "Civilization" in str(tool_messages[0].content)


async def test_agent_can_call_tools_in_several_rounds(tmp_path):
    factory, model, repo, ctx = _factory(
        tmp_path,
        [
            _tool_call("search_memory", {"query": "Civilization"}, "c1"),
            _tool_call("get_user_profile", {}, "c2"),
            AIMessage(content="Final answer after both tools."),
        ],
    )
    repo.upsert_user_profile(make_ctx())
    repo.add_memory(
        make_ctx(),
        scope=MemoryScope.PERSONA,
        category="preference",
        content="Alice likes Civilization",
        importance=0.8,
        confidence=0.8,
    )

    result = await factory.run("tell me about me", context=ctx)

    assert result.text == "Final answer after both tools."
    assert result.tool_calls == ("search_memory", "get_user_profile")
    assert len(model.calls) == 3


async def test_tool_call_limit_is_enforced(tmp_path):
    calls: list[int] = []

    @tool("tick")
    def tick() -> str:
        """Increment the counter."""
        calls.append(1)
        return "tick"

    factory, model, _repo, ctx = _factory(
        tmp_path,
        [
            _tool_call("tick", {}, "a"),
            _tool_call("tick", {}, "b"),
            _tool_call("tick", {}, "c"),
        ],
        tools=[tick],
        max_tool_rounds=2,
    )

    with pytest.raises(AgentToolLimitError):
        await factory.run("loop forever", context=ctx)
    assert len(calls) == 2
    assert len(model.calls) == 3


async def test_invalid_tool_arguments_are_returned_to_the_model(tmp_path):
    factory, model, _repo, ctx = _factory(
        tmp_path,
        [
            _tool_call("search_memory", {}),
            AIMessage(content="I could not search, but I can still answer."),
        ],
    )
    result = await factory.run("search something", context=ctx)
    assert result.text == "I could not search, but I can still answer."
    tool_messages = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert tool_messages and tool_messages[0].status == "error"


async def test_unknown_tool_is_rejected_without_crashing(tmp_path):
    factory, model, _repo, ctx = _factory(
        tmp_path,
        [
            _tool_call("delete_everything", {}),
            AIMessage(content="That tool is not available."),
        ],
    )
    result = await factory.run("delete the database", context=ctx)
    assert result.text == "That tool is not available."
    tool_messages = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert tool_messages and tool_messages[0].status == "error"
    assert "delete_everything" in str(tool_messages[0].content)


async def test_tool_exception_becomes_controlled_agent_failure(tmp_path):
    @tool("boom")
    def boom() -> str:
        """Always fail."""
        raise RuntimeError("secret backend failure")

    factory, _model, _repo, ctx = _factory(
        tmp_path, [_tool_call("boom", {})], tools=[boom]
    )
    with pytest.raises(AgentExecutionError):
        await factory.run("call boom", context=ctx)


async def test_agent_timeout_is_controlled(tmp_path):
    class SlowModel(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
            time.sleep(0.3)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    loaded = make_loaded("deepseek-chan")
    _db, policy, repo = make_repo(tmp_path)
    model = SlowModel(responses=[AIMessage(content="too late")])
    factory = AgentFactory(
        config=AgentConfig(max_tool_rounds=2, timeout_seconds=0.05),
        model_config=loaded.config.model,
        persona_prompt=loaded.persona.prompt,
        tools=[],
        model_factory=lambda _config: model,
        instance_id="mika",
        persona_id="mika",
    )
    ctx = AgentRuntimeContext(
        "mika", "mika", "100", "200", "300", "Alice", "1000", False, repo, policy,
        logging.getLogger("test"),
    )
    with pytest.raises(AgentTimeoutError):
        await factory.run("hi", context=ctx)


def test_tool_business_logic_rejects_cross_persona_reads(tmp_path):
    _db, policy, repo = make_repo(tmp_path)
    repo.add_memory(
        make_ctx(persona_id="mika"),
        scope=MemoryScope.PERSONA,
        category="experience",
        content="Alice discussed Hokkaido with Mika",
        importance=0.9,
        confidence=0.9,
    )
    ren_ctx = make_ctx(persona_id="ren", instance_id="ren")
    payload = search_memory_impl(repo, ren_ctx, "Hokkaido", limit=5, max_chars=200)
    parsed = json.loads(payload)
    assert parsed["memories"] == []
    assert "discussed Hokkaido" not in payload
    profile = get_user_profile_impl(repo, ren_ctx)
    assert '"profile": null' in profile


async def test_real_agent_tool_respects_persona_scope(tmp_path):
    factory, model, repo, ctx = _factory(
        tmp_path,
        [
            _tool_call("search_memory", {"query": "Hokkaido"}),
            AIMessage(content="I do not recall that."),
        ],
        persona_id="ren",
    )
    repo.add_memory(
        make_ctx(persona_id="mika"),
        scope=MemoryScope.PERSONA,
        category="experience",
        content="Alice discussed Hokkaido with Mika",
        importance=0.9,
        confidence=0.9,
    )

    result = await factory.run("what did we discuss?", context=ctx)

    assert result.text == "I do not recall that."
    tool_messages = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert tool_messages
    assert json.loads(str(tool_messages[0].content))["memories"] == []
