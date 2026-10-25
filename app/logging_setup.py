"""Logging setup. Callers must never log tokens, API keys, or raw memory dumps."""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)
    # These libraries are very chatty at INFO.
    logging.getLogger("wxauto").setLevel(max(numeric, logging.WARNING))
    logging.getLogger("httpx").setLevel(max(numeric, logging.WARNING))
