"""Shared application exceptions.

All exceptions raised by application code derive from :class:`AppError` so the
runtime can turn them into a controlled failure state without killing the bot.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for expected application errors."""


class ConfigurationError(AppError):
    """Invalid or missing configuration / credentials."""


class MemoryAccessDenied(AppError):
    """The current persona is not allowed to access the requested data."""


class MemoryValidationError(AppError):
    """A model-produced memory candidate failed validation."""


class AgentExecutionError(AppError):
    """The chat agent failed in a controlled way."""


class AgentTimeoutError(AgentExecutionError):
    """The chat agent did not finish before its deadline."""


class AgentToolLimitError(AgentExecutionError):
    """The agent exceeded the configured tool-call budget."""
