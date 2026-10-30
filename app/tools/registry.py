"""Tool registry.

Tools are selected per instance from configuration. Unknown names are rejected
at startup rather than silently ignored.
"""

from __future__ import annotations

from app.config import InstanceConfig
from app.errors import ConfigurationError
from app.tools.memory_tools import build_memory_tools
from app.tools.web_tools import build_web_tools

MEMORY_TOOL_NAMES = frozenset({"search_memory", "get_user_profile"})
WEB_TOOL_NAMES = frozenset({"web_search"})
KNOWN_TOOL_NAMES = MEMORY_TOOL_NAMES | WEB_TOOL_NAMES


def validate_tool_names(config: InstanceConfig) -> None:
    unknown = set(config.tools) - KNOWN_TOOL_NAMES
    if unknown:
        raise ConfigurationError(
            f"{config.instance_id}: unknown tools: {', '.join(sorted(unknown))}"
        )
    if not config.memory.enabled and set(config.tools) & MEMORY_TOOL_NAMES:
        raise ConfigurationError(
            f"{config.instance_id}: memory is disabled but memory tools are configured"
        )


def build_tools(config: InstanceConfig) -> list:
    """Build the tool list for one instance (never shared across personas)."""

    validate_tool_names(config)
    available: dict = {}
    if config.memory.enabled:
        available.update(
            {tool.name: tool for tool in build_memory_tools(config.memory)}
        )
    available.update({tool.name: tool for tool in build_web_tools(config.web_search)})
    return [available[name] for name in config.tools if name in available]


__all__ = [
    "KNOWN_TOOL_NAMES",
    "MEMORY_TOOL_NAMES",
    "WEB_TOOL_NAMES",
    "build_tools",
    "validate_tool_names",
]
