"""LangChain memory tools.

The LLM only supplies a free-text query. Identity and permissions come from
``ToolRuntime`` and are validated by the repository/policy on every call.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from app.config import MemoryConfig
from app.errors import MemoryAccessDenied
from app.memory.models import MemoryRecord, TrustedContext
from app.memory.repository import MemoryRepository
from app.tools.context import AgentRuntimeContext

logger = logging.getLogger(__name__)


class SearchMemoryArgs(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="Keywords describing the fact to recall (not a user id).",
    )


class ProfileArgs(BaseModel):
    """The current user's identity comes from runtime context, not the model.

    A harmless optional field is present so LangChain keeps the schema non-empty
    and still injects ``ToolRuntime``; its value is ignored.
    """

    purpose: str = Field(
        default="",
        max_length=200,
        description="Optional short reason for reading the profile.",
    )


def _memory_payload(memory: MemoryRecord) -> dict[str, Any]:
    return {
        "id": memory.id,
        "scope": memory.scope.value,
        "category": memory.category.value,
        "content": memory.content,
        "importance": round(memory.importance, 2),
        "updated_at": memory.updated_at.isoformat(),
    }


def search_memory_impl(
    repository: MemoryRepository,
    ctx: TrustedContext,
    query: str,
    *,
    limit: int = 5,
    max_chars: int = 500,
) -> str:
    """Business logic, independently callable in tests."""

    memories = repository.search_memories(
        ctx, query, limit=limit, max_chars=max_chars
    )
    return json.dumps(
        {
            "query": query,
            "memories": [_memory_payload(memory) for memory in memories],
            "note": (
                "These are the ONLY memories available. Use one only if it is "
                "directly relevant to the newest message; do not mention unrelated "
                "memories or use them to revive an old topic."
            ),
        },
        ensure_ascii=False,
    )


def get_user_profile_impl(
    repository: MemoryRepository, ctx: TrustedContext
) -> str:
    """Business logic for the current user's profile in this persona scope."""

    profile = repository.get_user_profile(ctx)
    if profile is None:
        return json.dumps(
            {"profile": None, "note": "No prior interaction profile for this user."},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "profile": {
                "user_id": profile.user_id,
                "persona_id": profile.persona_id,
                "first_seen_at": profile.first_seen_at.isoformat(),
                "last_seen_at": profile.last_seen_at.isoformat(),
                "interaction_count": profile.interaction_count,
            }
        },
        ensure_ascii=False,
    )


def _require_context(runtime: ToolRuntime[AgentRuntimeContext]) -> AgentRuntimeContext:
    ctx = runtime.context
    if not isinstance(ctx, AgentRuntimeContext):
        raise MemoryAccessDenied("tool called without a trusted runtime context")
    return ctx


def _run_tool(name: str, ctx: AgentRuntimeContext, operation):
    started = time.perf_counter()
    ctx.logger.info(
        "tool_start tool=%s instance=%s persona=%s guild=%s channel=%s user=%s",
        name,
        ctx.instance_id,
        ctx.persona_id,
        ctx.guild_id,
        ctx.channel_id,
        ctx.user_id,
    )
    try:
        result = operation()
        return result
    except MemoryAccessDenied as exc:
        ctx.logger.warning("tool_denied tool=%s error=%s", name, exc)
        return json.dumps(
            {"error": "permission_denied", "message": "This data is not accessible."},
            ensure_ascii=False,
        )
    except Exception:
        ctx.logger.exception("tool_failed tool=%s", name)
        return json.dumps(
            {"error": "tool_failed", "message": "The memory lookup failed."},
            ensure_ascii=False,
        )
    finally:
        ctx.logger.info(
            "tool_end tool=%s duration_ms=%.1f",
            name,
            (time.perf_counter() - started) * 1000,
        )


def build_memory_tools(config: MemoryConfig) -> list:
    @tool("search_memory", args_schema=SearchMemoryArgs)
    def search_memory(
        runtime: ToolRuntime[AgentRuntimeContext], query: str
    ) -> str:
        """Search long-term memories about the current user within this persona's scope.

        Use this when a durable preference, experience, or plan would improve the
        reply. Only memories belonging to the current guild, user, and persona
        (plus authorized shared memories) are returned.
        """

        ctx = _require_context(runtime)
        return _run_tool(
            "search_memory",
            ctx,
            lambda: search_memory_impl(
                ctx.repository,
                ctx.trusted(),
                query,
                limit=config.max_memories_per_search,
                max_chars=config.max_memory_chars,
            ),
        )

    @tool("get_user_profile", args_schema=ProfileArgs)
    def get_user_profile(
        runtime: ToolRuntime[AgentRuntimeContext], purpose: str = ""
    ) -> str:
        """Get the interaction profile for the current user and persona.

        The profile contains first/last interaction timestamps and interaction
        count. It never exposes profiles from other personas or guilds.
        """

        del purpose  # model-supplied but intentionally ignored for scope safety
        ctx = _require_context(runtime)
        return _run_tool(
            "get_user_profile",
            ctx,
            lambda: get_user_profile_impl(ctx.repository, ctx.trusted()),
        )

    return [search_memory, get_user_profile]


__all__ = [
    "build_memory_tools",
    "get_user_profile_impl",
    "search_memory_impl",
    "SearchMemoryArgs",
]
