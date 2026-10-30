from __future__ import annotations

import logging

from app.logging_setup import configure_logging, restore_logging, snapshot_logging


def test_configure_logging_writes_to_the_console_only_without_a_file():
    level, handlers = snapshot_logging()
    try:
        configure_logging("INFO")
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
        assert root.level == logging.INFO
    finally:
        restore_logging((level, handlers))


def test_configure_logging_writes_the_log_file(tmp_path):
    level, handlers = snapshot_logging()
    log_file = tmp_path / "data" / "logs" / "app.log"
    try:
        configure_logging("INFO", log_file=log_file)
        logging.getLogger("test.filelog").info("bot started")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert "bot started" in log_file.read_text(encoding="utf-8")
    finally:
        restore_logging((level, handlers))
