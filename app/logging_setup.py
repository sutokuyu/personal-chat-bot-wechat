"""Logging setup. Callers must never log tokens, API keys, or raw memory dumps."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_NOISY_LOGGERS = ("wxauto", "wxauto4", "wxautox4", "httpx", "comtypes")

_state: dict[str, object] = {}


def _log_stream():
    """Return stderr as UTF-8 so Chinese diagnostics survive redirection."""

    stream = sys.stderr
    encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    if encoding in {"utf8", "cp65001"}:
        return stream
    try:
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        return stream
    return stream


def configure_logging(level: str = "INFO", *, log_file: Path | None = None) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler = logging.StreamHandler(_log_stream())
    handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [handler]
    if log_file is not None:
        file_handler = _file_handler(log_file, formatter)
        if file_handler is not None:
            handlers.append(file_handler)
    _state["level"] = numeric
    _state["handlers"] = handlers
    _apply(numeric, handlers)


def _file_handler(
    path: Path, formatter: logging.Formatter
) -> logging.Handler | None:
    """Rotating file log so a stuck bot can be diagnosed afterwards."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "log_file_unavailable path=%s error=%s", path, exc
        )
        return None
    handler.setFormatter(formatter)
    return handler


def snapshot_logging() -> tuple[int, list[logging.Handler]]:
    """Capture the current root logger so a noisy import can be rolled back."""

    root = logging.getLogger()
    return root.level, list(root.handlers)


def restore_logging(snapshot: tuple[int, list[logging.Handler]] | None = None) -> None:
    """Re-apply logging after a third-party import resets the root logger.

    wxauto4 calls ``logging.getLogger().handlers.clear()`` at import time and
    raises the root level to DEBUG. With a ``snapshot`` the previous handlers
    are restored verbatim (works under pytest too); without one the project
    configuration from ``configure_logging`` is re-applied.
    """

    if snapshot is not None:
        level, handlers = snapshot
        root = logging.getLogger()
        root.handlers.clear()
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)
        _quiet_noisy_loggers(level)
        return

    numeric = _state.get("level")
    handlers = _state.get("handlers")
    if isinstance(numeric, int) and isinstance(handlers, list) and handlers:
        _apply(numeric, handlers)


def _apply(numeric: int, handlers: list[logging.Handler]) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(numeric)
    _quiet_noisy_loggers(numeric)


def _quiet_noisy_loggers(numeric: int) -> None:
    # These libraries are very chatty at INFO (wxauto4 logs at DEBUG).
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric, logging.WARNING))
