"""Helpers for opt-in live tests against a real, logged-in WeChat client.

These tests drive the desktop client, so they are skipped unless
``WXAUTO_LIVE=1`` is set on a Windows host running WeChat 4.1.8.107.
"""

from __future__ import annotations

import os
import sys

import pytest

LIVE_ENABLED = os.environ.get("WXAUTO_LIVE") == "1"

requires_live_wechat = pytest.mark.skipif(
    not LIVE_ENABLED or sys.platform != "win32",
    reason=(
        "live WeChat test: run on Windows with WeChat 4.1.8.107 logged in "
        "and set WXAUTO_LIVE=1"
    ),
)

FILE_TRANSFER_HELPER = "文件传输助手"


def listen_timeout() -> float:
    return float(os.environ.get("WXAUTO_LISTEN_TIMEOUT", "60"))


def describe_message(chat_name: str | None, content: str, message_type: str, attr: str) -> None:
    print(
        f"chat = {chat_name}\ncontent = {content}\ntype = {message_type}\nattr = {attr}",
        flush=True,
    )
