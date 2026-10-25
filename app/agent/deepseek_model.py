"""ChatDeepSeek construction.

The DeepSeek model is used only through LangChain's chat-model interface, so
business logic never contains provider-specific inference code.
"""

from __future__ import annotations

import os
from typing import Mapping

from langchain_deepseek import ChatDeepSeek

from app.config import ModelConfig
from app.errors import ConfigurationError


def build_chat_model(
    config: ModelConfig,
    *,
    api_key: str | None = None,
    env: Mapping[str, str] | None = None,
    temperature: float | None = None,
) -> ChatDeepSeek:
    values = env if env is not None else os.environ
    key = (api_key or values.get(config.api_key_env, "")).strip()
    if not key:
        raise ConfigurationError(
            f"DeepSeek API key is missing; set {config.api_key_env} in the environment"
        )
    api_base = values.get(config.api_base_env, "").strip() if config.api_base_env else ""
    kwargs: dict = {
        "model": config.model_name,
        "api_key": key,
        "temperature": config.temperature if temperature is None else temperature,
        "timeout": config.timeout_seconds,
        "max_retries": config.max_retries,
    }
    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if api_base:
        kwargs["api_base"] = api_base
    return ChatDeepSeek(**kwargs)


def assert_tool_calling_model(config: ModelConfig) -> None:
    """DeepSeek's reasoner model does not support tool calling.

    The project targets ``deepseek-chat``; refusing obvious mismatches early is
    friendlier than a confusing agent failure later.
    """

    if config.model_name == "deepseek-reasoner":
        raise ConfigurationError(
            "deepseek-reasoner does not support tool calling; use deepseek-chat"
        )
