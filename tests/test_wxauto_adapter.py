from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import WeChatChatConfig, WeChatConfig
from app.wxauto_adapter import WxAutoAdapter, chat_id_for, stable_id, user_id_for


@dataclass
class RawMessage:
    sender: str
    content: str
    id: str = "m1"
    type: str = "friend"
    sender_remark: str = ""


@dataclass
class FakeWx:
    batches: list[list[RawMessage]] = field(default_factory=list)
    nickname: str = "DeepSeek-chan"
    added: list[str] = field(default_factory=list)
    sent: list[tuple[str, str | None]] = field(default_factory=list)

    def AddListenChat(self, name: str) -> None:
        self.added.append(name)

    def GetListenMessage(self, who: str) -> list[RawMessage]:
        return self.batches.pop(0) if self.batches else []

    def SendMsg(self, text: str, who: str | None = None) -> None:
        self.sent.append((text, who))


def _config(*, kind: str = "direct", bot_name: str | None = "DeepSeek-chan") -> WeChatConfig:
    return WeChatConfig(
        account_id="wechat-main",
        chats=[WeChatChatConfig(name="Alice", kind=kind)],
        poll_interval_seconds=0.2,
        send_delay_seconds=0,
        bot_name=bot_name,
    )


def test_stable_ids_are_positive_and_deterministic():
    assert stable_id("chat", "a") == stable_id("chat", "a")
    assert stable_id("chat", "a") > 0
    assert stable_id("chat", "a") != stable_id("chat", "b")
    assert chat_id_for("acc", "Alice") == chat_id_for("acc", "Alice")
    assert user_id_for("acc", "Alice", "Bob") != user_id_for("acc", "Alice", "Carol")


def test_direct_chat_message_is_treated_as_directed():
    adapter = WxAutoAdapter(config=_config(), handler=lambda msg: None)
    message = adapter.to_internal(
        RawMessage(sender="Alice", content="你好", id="1"),
        adapter.config.chats[0],
    )
    assert message is not None
    assert message.is_mention is True
    assert message.mentions_other is False
    assert message.author_name == "Alice"
    assert message.channel_id == chat_id_for("wechat-main", "Alice")
    assert message.guild_id == stable_id("account", "wechat-main")


def test_group_chat_mention_detection():
    adapter = WxAutoAdapter(config=_config(kind="group"), handler=lambda msg: None)
    chat = adapter.config.chats[0]
    mentioned = adapter.to_internal(
        RawMessage(sender="Bob", content="@DeepSeek-chan 你好", id="2"), chat
    )
    assert mentioned is not None
    assert mentioned.is_mention is True
    assert mentioned.mentions_other is False

    other = adapter.to_internal(
        RawMessage(sender="Bob", content="@Alice 你好", id="3"), chat
    )
    assert other is not None
    assert other.is_mention is False
    assert other.mentions_other is True

    normal = adapter.to_internal(RawMessage(sender="Bob", content="大家好", id="4"), chat)
    assert normal is not None
    assert normal.is_mention is False
    assert normal.mentions_other is False


def test_self_and_non_text_messages_are_skipped():
    adapter = WxAutoAdapter(config=_config(), handler=lambda msg: None)
    chat = adapter.config.chats[0]
    assert adapter.to_internal(RawMessage(sender="Self", content="hi", type="self"), chat) is None
    assert adapter.to_internal(RawMessage(sender="Alice", content="[图片]", id="5"), chat) is None
    assert adapter.to_internal(RawMessage(sender="Alice", content="   ", id="6"), chat) is None


async def test_poll_once_dispatches_and_deduplicates():
    wx = FakeWx(
        batches=[
            [
                RawMessage(sender="Alice", content="hello", id="1"),
                RawMessage(sender="Alice", content="again", id="2"),
            ],
            [RawMessage(sender="Alice", content="hello", id="1")],
        ]
    )
    received = []

    async def handler(message):
        received.append(message)

    adapter = WxAutoAdapter(
        config=_config(), handler=handler, client_factory=lambda: wx
    )
    first = await adapter.poll_once()
    second = await adapter.poll_once()

    assert wx.added == ["Alice"]
    assert [m.content for m in first] == ["hello", "again"]
    assert [m.content for m in received] == ["hello", "again"]
    assert second == []


async def test_send_reply_routes_to_configured_chat():
    wx = FakeWx()
    adapter = WxAutoAdapter(
        config=_config(), handler=lambda msg: None, client_factory=lambda: wx
    )
    await adapter.poll_once()
    channel_id = chat_id_for("wechat-main", "Alice")
    assert await adapter.send_reply(
        channel_id=channel_id, guild_id=1, text="收到"
    )
    assert wx.sent == [("收到", "Alice")]
    assert not await adapter.send_reply(
        channel_id=123, guild_id=1, text="unknown"
    )


async def test_bot_name_falls_back_to_wechat_nickname():
    wx = FakeWx(nickname="鲨鱼")
    adapter = WxAutoAdapter(
        config=_config(kind="group", bot_name=None),
        handler=lambda msg: None,
        client_factory=lambda: wx,
    )
    await adapter.poll_once()
    assert adapter.bot_name == "鲨鱼"
    message = adapter.to_internal(
        RawMessage(sender="Bob", content="@鲨鱼 在吗", id="7"),
        adapter.config.chats[0],
    )
    assert message is not None and message.is_mention
