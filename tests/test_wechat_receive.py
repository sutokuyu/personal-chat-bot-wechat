"""Test 3: wxauto4 receives messages from a real chat.

文件传输助手 is a chat with yourself, so the message arrives with ``attr='self'``.
The bot intentionally ignores self messages at runtime (to prevent reply loops),
but this test verifies that wxauto4 itself delivers them.

Send ``hello`` to 文件传输助手 by hand while this test polls. Override the
timeout with ``WXAUTO_LISTEN_TIMEOUT`` (seconds).

A group can be checked too by setting ``WXAUTO_GROUP_NAME`` and sending
``@<bot nickname> 你好`` there.
"""

from __future__ import annotations

import os
import time

import pytest

from tests._wechat_live import (
    FILE_TRANSFER_HELPER,
    describe_message,
    listen_timeout,
    requires_live_wechat,
)

pytestmark = requires_live_wechat


def _wait_for_new_message(wx, chat_name: str) -> tuple[str, str, str, str]:
    assert wx.ChatWith(chat_name) == chat_name
    baseline = {
        getattr(message, "id", None)
        for message in wx.GetAllMessage()
        if message is not None
    }
    deadline = time.monotonic() + listen_timeout()

    while time.monotonic() < deadline:
        wx.ChatWith(chat_name)
        for message in wx.GetAllMessage():
            message_id = getattr(message, "id", None)
            if message_id in baseline:
                continue
            baseline.add(message_id)
            attr = str(getattr(message, "attr", ""))
            if attr == "system":
                continue
            content = str(getattr(message, "content", ""))
            if not content:
                continue
            return (
                chat_name,
                content,
                str(getattr(message, "type", "")),
                attr,
            )
        time.sleep(1)

    pytest.fail(f"no new message in {chat_name!r} within {listen_timeout()}s")


def test_receive_text_from_file_transfer_helper() -> None:
    from wxauto4 import WeChat

    wx = WeChat()
    describe_message(*_wait_for_new_message(wx, FILE_TRANSFER_HELPER))


def test_receive_text_from_configured_group() -> None:
    group = os.environ.get("WXAUTO_GROUP_NAME", "").strip()
    if not group:
        pytest.skip("set WXAUTO_GROUP_NAME to a real group name to test group receive")

    from wxauto4 import WeChat

    wx = WeChat()
    describe_message(*_wait_for_new_message(wx, group))


if __name__ == "__main__":  # pragma: no cover - manual run
    test_receive_text_from_file_transfer_helper()
