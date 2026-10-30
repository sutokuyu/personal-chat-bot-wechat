from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.agent_factory import AgentFactory
from app.config import AgentConfig, WebSearchConfig
from app.tools.context import AgentRuntimeContext
from app.tools.registry import build_tools, validate_tool_names
from app.tools.web_tools import SEARCH_PATH, build_web_tools, format_results, search_web
from tests.conftest import ScriptedChatModel, make_loaded, make_repo

SAMPLE_PAYLOAD: dict[str, Any] = {
    "results": [
        {
            "title": "DeepSeek V4 released",
            "url": "https://example.com/v4",
            "snippet": "short snippet",
            "summary": "DeepSeek released V4 with tool calling.",
            "site_name": "Example News",
            "date": "2026-06-01T00:00:00+00:00",
        }
    ]
}


async def test_search_web_sends_expected_request():
    calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    async def post(url, headers, payload):  # noqa: ANN001
        calls.append((url, headers, payload))
        return SAMPLE_PAYLOAD

    config = WebSearchConfig(max_results=3, include_content=True)
    payload = await search_web(
        config, "DeepSeek V4", env={"DEEPSEEK_API_KEY": "secret"}, post=post
    )

    assert payload == SAMPLE_PAYLOAD
    url, headers, body = calls[0]
    assert url == "https://api.deepseek.com" + SEARCH_PATH
    assert headers["Authorization"] == "Bearer secret"
    assert headers["Content-Type"] == "application/json"
    assert body == {"query": "DeepSeek V4", "count": 3, "need_content": True}


async def test_search_web_explicit_count_overrides_config():
    seen = {}

    async def post(_url, _headers, payload):  # noqa: ANN001
        seen.update(payload)
        return {"results": []}

    await search_web(
        WebSearchConfig(), "q", count=1, env={"DEEPSEEK_API_KEY": "k"}, post=post
    )
    assert seen["count"] == 1


async def test_search_web_requires_api_key():
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        await search_web(WebSearchConfig(), "anything", env={})


def test_format_results_truncates_and_flags_untrusted_content():
    long_summary = {"summary": "s" * 900}
    payload = {"results": [long_summary, long_summary, long_summary]}

    parsed = json.loads(format_results(payload, max_results=2))

    assert len(parsed["results"]) == 2
    assert len(parsed["results"][0]["summary"]) == 400
    assert "untrusted" in parsed["note"].lower()


def test_format_results_handles_empty_payload():
    parsed = json.loads(format_results({}, max_results=5))
    assert parsed["results"] == []


def test_format_results_keeps_truncated_page_content():
    payload = {"results": [{"title": "T", "content": "c" * 5000}]}
    parsed = json.loads(format_results(payload, max_results=5))
    assert len(parsed["results"][0]["content"]) == 1200

    without_content = json.loads(format_results({"results": [{"title": "T"}]}, max_results=5))
    assert "content" not in without_content["results"][0]


def test_build_web_tools_exposes_web_search():
    tools = build_web_tools(WebSearchConfig())
    assert [tool.name for tool in tools] == ["web_search"]
    assert "query" in tools[0].args


def test_web_search_is_selected_only_when_configured():
    loaded = make_loaded("deepseek-chan")
    without = loaded.config.model_copy(
        update={"tools": ["search_memory", "get_user_profile"]}
    )
    assert "web_search" not in [tool.name for tool in build_tools(without)]

    with_web = loaded.config.model_copy(update={"tools": ["web_search"]})
    validate_tool_names(with_web)
    assert [tool.name for tool in build_tools(with_web)] == ["web_search"]


def test_web_search_works_with_memory_disabled():
    loaded = make_loaded("deepseek-chan")
    config = loaded.config.model_copy(
        update={
            "tools": ["web_search"],
            "memory": loaded.config.memory.model_copy(update={"enabled": False}),
        }
    )
    validate_tool_names(config)
    assert [tool.name for tool in build_tools(config)] == ["web_search"]


def test_web_search_config_rejects_bad_values():
    with pytest.raises(ValueError):
        WebSearchConfig(max_results=50)
    with pytest.raises(ValueError):
        WebSearchConfig(api_base="api.deepseek.com")
    with pytest.raises(ValueError):
        WebSearchConfig(api_key_env="not a name")


async def test_agent_can_call_web_search_tool(tmp_path):
    async def post(_url, _headers, _payload):  # noqa: ANN001
        return SAMPLE_PAYLOAD

    loaded = make_loaded("deepseek-chan")
    _db, _policy, repo = make_repo(tmp_path)
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "DeepSeek V4"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="V4 已经发布，支持工具调用。"),
        ]
    )
    factory = AgentFactory(
        config=AgentConfig(max_tool_rounds=2, timeout_seconds=5.0),
        model_config=loaded.config.model,
        persona_prompt=loaded.persona.prompt,
        tools=build_web_tools(WebSearchConfig(), post=post, env={"DEEPSEEK_API_KEY": "k"}),
        model_factory=lambda _config: model,
        instance_id="deepseek-chan",
        persona_id="deepseek-chan",
    )
    ctx = AgentRuntimeContext(
        instance_id="deepseek-chan",
        persona_id="deepseek-chan",
        guild_id="100",
        channel_id="200",
        user_id="300",
        user_name="Alice",
        message_id="1000",
        allow_shared_memory=False,
        repository=repo,
        policy=_policy,
        logger=logging.getLogger("test.web"),
    )

    result = await factory.run("DeepSeek V4 发布了吗？", context=ctx)

    assert result.tool_calls == ("web_search",)
    tool_messages = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert "https://example.com/v4" in str(tool_messages[0].content)


async def test_agent_web_search_failure_is_reported_to_model(tmp_path):
    async def post(_url, _headers, _payload):  # noqa: ANN001
        raise RuntimeError("network down")

    loaded = make_loaded("deepseek-chan")
    _db, policy, repo = make_repo(tmp_path)
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "x"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="我查不到，先按我知道的说。"),
        ]
    )
    factory = AgentFactory(
        config=AgentConfig(max_tool_rounds=2, timeout_seconds=5.0),
        model_config=loaded.config.model,
        persona_prompt=loaded.persona.prompt,
        tools=build_web_tools(WebSearchConfig(), post=post, env={"DEEPSEEK_API_KEY": "k"}),
        model_factory=lambda _config: model,
        instance_id="deepseek-chan",
        persona_id="deepseek-chan",
    )
    ctx = AgentRuntimeContext(
        instance_id="deepseek-chan",
        persona_id="deepseek-chan",
        guild_id="100",
        channel_id="200",
        user_id="300",
        user_name="Alice",
        message_id="1000",
        allow_shared_memory=False,
        repository=repo,
        policy=policy,
        logger=logging.getLogger("test.web"),
    )

    result = await factory.run("随便问问", context=ctx)

    assert result.text == "我查不到，先按我知道的说。"
    tool_messages = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert "search_failed" in str(tool_messages[0].content)
