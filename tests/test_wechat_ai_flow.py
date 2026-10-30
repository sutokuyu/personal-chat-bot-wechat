"""Test 4: full loop — WeChat message -> Bot -> DeepSeek -> WeChat reply.

A synthetic text message for 文件传输助手 is converted by the real adapter and
handed to the real runtime, so message conversion, the participation gate, the
LangChain agent, DeepSeek, and the wxauto4 send path are all exercised. Set
``WXAUTO_LIVE=1`` and make sure ``DEEPSEEK_API_KEY`` is available in the
environment or ``.env``.
"""

from __future__ import annotations

import os
import time
import types
from pathlib import Path

import pytest

from app.config import WeChatChatConfig, load_dotenv_if_present, load_instance
from app.runtime import create_runtime
from app.wxauto_adapter import WxAutoAdapter
from tests._wechat_live import FILE_TRANSFER_HELPER, requires_live_wechat

pytestmark = requires_live_wechat


async def test_ai_reply_roundtrip(tmp_path) -> None:
    load_dotenv_if_present(Path("."))
    if not os.environ.get("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY is not configured")

    loaded = load_instance("deepseek-chan")
    wechat = loaded.config.wechat.model_copy(
        update={
            "chats": [WeChatChatConfig(name=FILE_TRANSFER_HELPER, kind="direct")],
            "bot_name": None,
            "send_delay_seconds": 0,
        }
    )
    config = loaded.config.model_copy(update={"wechat": wechat})
    loaded = loaded.model_copy(
        update={"config": config, "database_path": tmp_path / "live.db"}
    )

    runtime = create_runtime(loaded)
    adapter = WxAutoAdapter(config=wechat, handler=runtime.handle_message)
    runtime.set_sender(adapter)

    sent: list[str] = []
    original_send = adapter.send_reply

    async def recording_send(**kwargs):
        ok = await original_send(**kwargs)
        if ok:
            sent.append(kwargs["text"])
        return ok

    adapter.send_reply = recording_send  # type: ignore[method-assign]

    try:
        adapter._init_client()
        raw = types.SimpleNamespace(
            type="text",
            attr="friend",
            sender="live-test",
            content="用一句话解释 Transformer 是什么。",
            id=f"live-{int(time.time())}",
        )
        message = adapter.to_internal(raw, wechat.chats[0])
        assert message is not None

        await runtime.handle_message(message)
        assert sent, "the bot produced no reply"

        print(f"ai_reply = {sent[0]}", flush=True)
    finally:
        await runtime.close()
