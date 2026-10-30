from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.config import WeChatChatConfig, WeChatConfig
from app.errors import ConfigurationError
from app.wxauto_adapter import (
    WxAutoAdapter,
    chat_id_for,
    response_error,
    stable_id,
    user_id_for,
)

_OK = {"status": "成功", "message": "ok", "data": None}


@dataclass
class RawMessage:
    sender: str
    content: str
    id: str = "m1"
    type: str = "text"
    attr: str = "friend"
    sender_remark: str = ""
    hash: str = ""
    image_path: str = ""
    downloads: int = 0
    control: Any = None

    def download(self):
        self.downloads += 1
        if not self.image_path:
            raise RuntimeError("no image")
        return Path(self.image_path)


class _Rect:
    left = 10
    top = 20
    right = 210
    bottom = 120


class _Control:
    BoundingRectangle = _Rect()


class _FakeShot:
    size = (200, 100)

    def save(self, path) -> None:
        Path(path).write_bytes(b"png")


class FakeWx:
    def __init__(self, nickname: str = "DeepSeek-chan") -> None:
        self.nickname = nickname
        self.history: dict[str, list[RawMessage]] = {}
        self.current: str | None = None
        self.opened: list[str] = []
        self.sent: list[tuple[str | None, str]] = []
        self.unknown_chats: set[str] = set()
        self.read_error: str | None = None
        self.send_error: str | None = None
        self.sessions: list[dict] | None = None
        self.session_error: str | None = None

    def add_chat(self, name: str, *messages: RawMessage) -> None:
        self.history.setdefault(name, []).extend(messages)

    def ChatWith(self, who: str, exact: bool = True):
        if who in self.unknown_chats:
            return {"status": "失败", "message": "找不到聊天", "data": None}
        self.current = who
        self.opened.append(who)
        return who

    def GetAllMessage(self):
        if self.read_error:
            raise RuntimeError(self.read_error)
        return list(self.history.get(self.current or "", []))

    def SendMsg(self, msg: str, who: str | None = None, exact: bool = False):
        self.sent.append((who, msg))
        if self.send_error:
            return {"status": "失败", "message": self.send_error, "data": None}
        return _OK

    def GetSession(self):
        if self.session_error:
            raise RuntimeError(self.session_error)
        rows = self.sessions
        if rows is None:
            rows = [{"name": name} for name in self.history]
        return [type("Session", (), dict(row))() for row in rows]


def _config(
    *chats: tuple[str, str],
    bot_name: str | None = "DeepSeek-chan",
    poll_interval: float = 0.2,
    capture_images: bool = False,
) -> WeChatConfig:
    return WeChatConfig(
        account_id="wechat-main",
        chats=[
            WeChatChatConfig(name=name, kind=kind)
            for name, kind in chats or (("Alice", "direct"),)
        ],
        poll_interval_seconds=poll_interval,
        send_delay_seconds=0,
        bot_name=bot_name,
        capture_images=capture_images,
    )


def _adapter(wx: FakeWx, *, config: WeChatConfig | None = None, handler=None):
    async def _noop(message):  # pragma: no cover - default sink
        return None

    return WxAutoAdapter(
        config=config or _config(),
        handler=handler or _noop,
        client_factory=lambda: wx,
    )


def test_stable_ids_are_positive_and_deterministic():
    assert stable_id("chat", "a") == stable_id("chat", "a")
    assert stable_id("chat", "a") > 0
    assert stable_id("chat", "a") != stable_id("chat", "b")
    assert chat_id_for("acc", "Alice") == chat_id_for("acc", "Alice")
    assert user_id_for("acc", "Alice", "Bob") != user_id_for("acc", "Alice", "Carol")


def test_response_error_understands_wxresponse_shapes():
    assert response_error(None) is None
    assert response_error(_OK) is None
    assert response_error({"status": "success"}) is None
    assert "boom" in (response_error({"status": "失败", "message": "boom"}) or "")
    assert response_error("wxauto warning") == "wxauto warning"


def test_direct_chat_message_is_treated_as_directed():
    adapter = _adapter(FakeWx())
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
    adapter = _adapter(FakeWx(), config=_config(("Alice", "group")))
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


def test_self_system_and_non_text_messages_are_skipped():
    adapter = _adapter(FakeWx())
    chat = adapter.config.chats[0]
    assert adapter.to_internal(RawMessage(sender="我", content="hi", attr="self"), chat) is None
    assert adapter.to_internal(RawMessage(sender="系统", content="x", attr="system"), chat) is None
    assert (
        adapter.to_internal(RawMessage(sender="Alice", content="[图片]", type="image"), chat)
        is None
    )
    assert adapter.to_internal(RawMessage(sender="Alice", content="   ", id="6"), chat) is None


def test_quote_messages_are_treated_as_text():
    adapter = _adapter(FakeWx())
    message = adapter.to_internal(
        RawMessage(sender="Alice", content="确实是这样", type="quote", id="q1"),
        adapter.config.chats[0],
    )
    assert message is not None
    assert message.content == "确实是这样"


async def test_startup_history_is_not_replayed():
    wx = FakeWx()
    wx.add_chat("Alice", RawMessage(sender="Alice", content="old", id="old-1"))
    received: list[str] = []

    async def handler(message):
        received.append(message.content)

    adapter = _adapter(wx, handler=handler)
    assert await adapter.poll_once() == []
    assert wx.opened and set(wx.opened) == {"Alice"}
    assert received == []


async def test_new_messages_are_dispatched():
    wx = FakeWx()
    wx.add_chat("Alice", RawMessage(sender="Alice", content="old", id="old-1"))
    received: list[str] = []

    async def handler(message):
        received.append(message.content)

    adapter = _adapter(wx, handler=handler)
    await adapter.poll_once()

    wx.add_chat("Alice", RawMessage(sender="Alice", content="hello", id="new-1"))
    delivered = await adapter.poll_once()

    assert [m.content for m in delivered] == ["hello"]
    assert received == ["hello"]


async def test_content_hash_is_preferred_over_the_volatile_control_id():
    wx = FakeWx()
    wx.add_chat("Alice", RawMessage(sender="Alice", content="hello", id="runtime-1", hash="h1"))
    adapter = _adapter(wx)
    await adapter.poll_once()

    # WeChat rebuilt the control: new runtime id, same content hash.
    wx.history["Alice"] = [
        RawMessage(sender="Alice", content="hello", id="runtime-99", hash="h1")
    ]
    assert await adapter.poll_once() == []


async def test_anchor_survives_id_changes_across_chat_switches():
    wx = FakeWx()
    wx.add_chat(
        "Alice",
        RawMessage(sender="Alice", content="old", id="runtime-1", hash="h1"),
    )
    received: list[str] = []

    async def handler(message):
        received.append(message.content)

    adapter = _adapter(wx, handler=handler)
    await adapter.poll_once()

    # Simulate a re-render after switching to another chat and back.
    wx.history["Alice"] = [
        RawMessage(sender="Alice", content="old", id="runtime-50", hash="h1"),
        RawMessage(sender="Bob", content="new", id="runtime-51", hash="h2"),
    ]
    delivered = await adapter.poll_once()

    assert [m.content for m in delivered] == ["new"]
    assert received == ["new"]


async def test_identical_text_from_the_same_sender_collapses():
    wx = FakeWx()
    wx.add_chat("Alice")
    adapter = _adapter(wx)
    await adapter.poll_once()

    wx.add_chat(
        "Alice",
        RawMessage(sender="Alice", content="哈哈", id="r1", hash="same"),
        RawMessage(sender="Alice", content="哈哈", id="r2", hash="same"),
    )

    assert len(await adapter.poll_once()) == 1


async def test_a_burst_of_images_from_one_sender_is_kept_apart(tmp_path):
    images = []
    for index in range(3):
        path = tmp_path / f"pic{index}.png"
        path.write_bytes(b"fake-png")
        images.append(path)

    wx = FakeWx()
    wx.add_chat("Alice", RawMessage(sender="Alice", content="old", id="m1"))
    adapter = _adapter(wx, config=_config(capture_images=True))
    await adapter.poll_once()

    wx.history["Alice"] = [
        RawMessage(sender="Alice", content="old", id="m1"),
        *[
            RawMessage(
                sender="Alice",
                content="图片",
                type="image",
                id=f"i{index}",
                hash=f"digest{index}",
                image_path=str(path),
            )
            for index, path in enumerate(images)
        ],
    ]

    delivered = await adapter.poll_once()

    assert [m.image_paths[0] for m in delivered] == [str(path) for path in images]
    assert len({m.message_id for m in delivered}) == 3


async def test_older_history_exposed_later_is_not_dispatched():
    wx = FakeWx()
    wx.add_chat("Alice", RawMessage(sender="Alice", content="newest", id="m2"))
    received: list[str] = []

    async def handler(message):
        received.append(message.content)

    adapter = _adapter(wx, handler=handler)
    await adapter.poll_once()

    # A repaint exposes an older message that was never in the baseline.
    wx.history["Alice"].insert(0, RawMessage(sender="Alice", content="older", id="m1"))
    assert await adapter.poll_once() == []
    assert received == []

    wx.add_chat("Alice", RawMessage(sender="Alice", content="brand new", id="m3"))
    assert [m.content for m in await adapter.poll_once()] == ["brand new"]


async def test_window_that_scrolled_past_the_anchor_is_processed():
    wx = FakeWx()
    wx.add_chat("Alice", RawMessage(sender="Alice", content="first", id="m1"))
    adapter = _adapter(wx)
    await adapter.poll_once()

    # Several messages arrived and pushed the anchor out of the rendered
    # window: everything visible is newer than what we know, so it is new.
    wx.history["Alice"] = [
        RawMessage(sender="Alice", content="stale", id="x1"),
        RawMessage(sender="Alice", content="fresh", id="x2"),
    ]
    delivered = await adapter.poll_once()
    assert [m.content for m in delivered] == ["stale", "fresh"]

    # A second poll must not replay them.
    assert await adapter.poll_once() == []


async def test_chats_with_unread_messages_are_polled_first():
    wx = FakeWx()
    wx.add_chat("Alice")
    wx.add_chat("Bob")
    adapter = _adapter(
        wx,
        config=_config(("Alice", "group"), ("Bob", "group")),
    )
    await adapter.poll_once()

    wx.add_chat("Alice", RawMessage(sender="Alice", content="hi", id="a1"))
    wx.add_chat("Bob", RawMessage(sender="Bob", content="hey", id="b1"))
    wx.sessions = [{"name": "Alice", "isnew": False, "new_count": 0}, {"name": "Bob", "isnew": True, "new_count": 2}]
    wx.opened.clear()

    delivered = await adapter.poll_once()

    assert [m.content for m in delivered] == ["hey", "hi"]
    assert wx.opened == ["Bob", "Alice"], "the unread chat must be read first"


async def test_session_failures_fall_back_to_config_order():
    wx = FakeWx()
    wx.add_chat("Alice")
    wx.add_chat("Bob")
    adapter = _adapter(wx, config=_config(("Alice", "group"), ("Bob", "group")))
    await adapter.poll_once()

    wx.session_error = "session list unavailable"
    wx.opened.clear()
    await adapter.poll_once()

    assert wx.opened == ["Alice", "Bob"]


async def test_empty_render_is_reported_once():
    wx = FakeWx()
    wx.add_chat("Alice", RawMessage(sender="Alice", content="hello", id="m1"))
    adapter = _adapter(wx)
    await adapter.poll_once()

    warnings: list[str] = []
    adapter.logger.warning = lambda message, *args, **kwargs: warnings.append(
        message % args if args else message
    )

    wx.history["Alice"] = []
    await adapter.poll_once()
    await adapter.poll_once()

    assert len(warnings) == 1
    assert "wxauto4_chat_empty" in warnings[0]


async def test_repeating_the_same_image_inside_the_dedupe_window_is_dropped(tmp_path):
    image = tmp_path / "pic.png"
    image.write_bytes(b"fake-png")
    wx = FakeWx()
    wx.add_chat("Alice", RawMessage(sender="Alice", content="old", id="m1"))
    adapter = _adapter(wx, config=_config(capture_images=True))
    await adapter.poll_once()

    raw = RawMessage(
        sender="Alice",
        content="图片",
        type="image",
        id="i1",
        hash="same-digest",
        image_path=str(image),
    )
    wx.add_chat("Alice", raw)
    assert len(await adapter.poll_once()) == 1

    # A repaint shows the same picture again: it must not be dispatched twice.
    wx.history["Alice"] = [raw]
    assert await adapter.poll_once() == []


async def test_a_picture_resent_later_is_dispatched_again(tmp_path):
    image = tmp_path / "pic.png"
    image.write_bytes(b"fake-png")
    wx = FakeWx()
    wx.add_chat("Alice", RawMessage(sender="Alice", content="old", id="m1"))
    adapter = _adapter(wx, config=_config(capture_images=True))
    await adapter.poll_once()

    first = RawMessage(
        sender="Alice",
        content="图片",
        type="image",
        id="i1",
        hash="same-digest",
        image_path=str(image),
    )
    wx.add_chat("Alice", first)
    assert len(await adapter.poll_once()) == 1

    # Sending the same file again minutes later is a new message.
    adapter._seen_seconds = 0.0
    wx.history["Alice"] = [first, RawMessage(
        sender="Alice",
        content="图片",
        type="image",
        id="i2",
        hash="same-digest",
        image_path=str(image),
    )]
    assert len(await adapter.poll_once()) == 1


async def test_duplicate_message_ids_are_dropped():
    wx = FakeWx()
    wx.add_chat("Alice")
    adapter = _adapter(wx)
    await adapter.poll_once()

    wx.add_chat("Alice", RawMessage(sender="Alice", content="hello", id="dup-1"))
    assert len(await adapter.poll_once()) == 1
    assert await adapter.poll_once() == []


async def test_multiple_chats_are_polled_in_config_order():
    wx = FakeWx()
    wx.add_chat("Alice")
    wx.add_chat("群聊", RawMessage(sender="Bob", content="旧消息", id="g-old"))
    config = _config(("Alice", "direct"), ("群聊", "group"))

    adapter = _adapter(wx, config=config)
    await adapter.poll_once()

    wx.add_chat("Alice", RawMessage(sender="Alice", content="私聊新消息", id="d-1"))
    wx.add_chat("群聊", RawMessage(sender="Bob", content="@DeepSeek-chan 群消息", id="g-1"))
    delivered = await adapter.poll_once()

    assert [m.content for m in delivered] == ["私聊新消息", "@DeepSeek-chan 群消息"]
    assert delivered[1].is_mention is True


async def test_unknown_chat_fails_fast_at_startup():
    wx = FakeWx()
    wx.unknown_chats.add("Alice")
    adapter = _adapter(wx)

    with pytest.raises(ConfigurationError) as err:
        await adapter.poll_once()

    assert "Alice" in str(err.value)
    assert "找不到聊天" in str(err.value)


async def test_poll_read_errors_do_not_kill_the_loop():
    wx = FakeWx()
    wx.add_chat("Alice")
    adapter = _adapter(wx)
    await adapter.poll_once()

    wx.read_error = "window vanished"
    assert await adapter.poll_once() == []


async def test_send_reply_routes_to_configured_chat():
    wx = FakeWx()
    adapter = _adapter(wx)
    await adapter.poll_once()
    channel_id = chat_id_for("wechat-main", "Alice")

    assert await adapter.send_reply(channel_id=channel_id, guild_id=1, text="收到")
    assert wx.sent == [("Alice", "收到")]
    assert not await adapter.send_reply(channel_id=123, guild_id=1, text="unknown")


async def test_send_reply_reports_rejection():
    wx = FakeWx()
    wx.send_error = "send rejected"
    adapter = _adapter(wx)
    await adapter.poll_once()

    assert not await adapter.send_reply(
        channel_id=chat_id_for("wechat-main", "Alice"), guild_id=1, text="nope"
    )


async def test_bot_name_falls_back_to_wechat_nickname():
    wx = FakeWx(nickname="鲨鱼")
    adapter = _adapter(wx, config=_config(("Alice", "group"), bot_name=None))
    await adapter.poll_once()

    assert adapter.bot_name == "鲨鱼"
    message = adapter.to_internal(
        RawMessage(sender="Bob", content="@鲨鱼 在吗", id="7"),
        adapter.config.chats[0],
    )
    assert message is not None and message.is_mention


async def test_image_message_is_downloaded_when_capture_enabled(tmp_path):
    image = tmp_path / "cat.png"
    image.write_bytes(b"fake-png")
    wx = FakeWx()
    adapter = _adapter(wx, config=_config(capture_images=True))
    await adapter.poll_once()

    wx.add_chat(
        "Alice",
        RawMessage(
            sender="Alice",
            content="图片",
            type="image",
            id="i1",
            hash="hi1",
            image_path=str(image),
        ),
    )
    delivered = await adapter.poll_once()

    assert len(delivered) == 1
    message = delivered[0]
    assert message.image_paths == (str(image),)
    assert message.content == "[图片]"
    assert message.is_mention is True


async def test_image_messages_are_skipped_when_capture_disabled(tmp_path):
    image = tmp_path / "cat.png"
    image.write_bytes(b"fake-png")
    wx = FakeWx()
    adapter = _adapter(wx)
    await adapter.poll_once()

    raw = RawMessage(
        sender="Alice", content="图片", type="image", id="i2", hash="hi2",
        image_path=str(image),
    )
    wx.add_chat("Alice", raw)

    assert await adapter.poll_once() == []
    assert raw.downloads == 0


async def test_history_images_are_not_downloaded_during_priming(tmp_path):
    image = tmp_path / "old.png"
    image.write_bytes(b"fake-png")
    wx = FakeWx()
    raw = RawMessage(
        sender="Alice", content="图片", type="image", id="i3", hash="hi3",
        image_path=str(image),
    )
    wx.add_chat("Alice", raw)

    adapter = _adapter(wx, config=_config(capture_images=True))
    assert await adapter.poll_once() == []

    assert raw.downloads == 0


async def test_image_download_falls_back_to_a_screenshot(tmp_path, monkeypatch):
    captured: list[tuple[int, int, int, int]] = []

    def fake_grab(bbox):
        captured.append(bbox)
        return _FakeShot()

    monkeypatch.setattr("app.wxauto_adapter._grab_region", fake_grab)
    monkeypatch.setattr("app.wxauto_adapter._IMAGE_DIR", tmp_path / "images")

    wx = FakeWx()
    adapter = _adapter(wx, config=_config(capture_images=True))
    await adapter.poll_once()

    raw = RawMessage(
        sender="Alice", content="图片", type="image", id="i5", hash="hi5", control=_Control()
    )
    wx.add_chat("Alice", raw)
    delivered = await adapter.poll_once()

    assert len(delivered) == 1
    path = Path(delivered[0].image_paths[0])
    assert path.exists()
    assert path.parent == tmp_path / "images"
    assert captured == [(10, 20, 210, 120)]


async def test_image_capture_failure_is_reported_instead_of_dropped(tmp_path):
    wx = FakeWx()
    adapter = _adapter(wx, config=_config(capture_images=True))
    await adapter.poll_once()

    raw = RawMessage(sender="Alice", content="", type="image", id="i6", hash="hi6")
    wx.add_chat("Alice", raw)
    delivered = await adapter.poll_once()

    assert len(delivered) == 1
    assert delivered[0].image_paths == ()
    assert "图片未能获取" in delivered[0].content


async def test_blank_screenshot_is_rejected(tmp_path, monkeypatch):
    class _BlankShot:
        size = (200, 100)

        def convert(self, _mode):
            class _Gray:
                def getextrema(self):
                    return (0, 0)

            return _Gray()

        def save(self, path):  # pragma: no cover - must not be called
            Path(path).write_bytes(b"png")

    monkeypatch.setattr("app.wxauto_adapter._grab_region", lambda bbox: _BlankShot())
    monkeypatch.setattr("app.wxauto_adapter._IMAGE_DIR", tmp_path / "images")

    wx = FakeWx()
    adapter = _adapter(wx, config=_config(capture_images=True))
    await adapter.poll_once()

    raw = RawMessage(
        sender="Alice", content="图片", type="image", id="i7", hash="hi7", control=_Control()
    )
    wx.add_chat("Alice", raw)
    delivered = await adapter.poll_once()

    assert len(delivered) == 1
    assert delivered[0].image_paths == ()
    assert not (tmp_path / "images").exists()


async def test_offscreen_image_rect_is_skipped(tmp_path, monkeypatch):
    class _OffscreenRect:
        left = 5000
        top = 5000
        right = 5200
        bottom = 5100

    class _OffscreenControl:
        BoundingRectangle = _OffscreenRect()

    monkeypatch.setattr(
        "app.wxauto_adapter._virtual_screen_rect", lambda: (0, 0, 1920, 1080)
    )
    monkeypatch.setattr(
        "app.wxauto_adapter._grab_region",
        lambda bbox: pytest.fail("an offscreen rect must not be captured"),
    )

    wx = FakeWx()
    adapter = _adapter(wx, config=_config(capture_images=True))
    await adapter.poll_once()

    raw = RawMessage(
        sender="Alice",
        content="图片",
        type="image",
        id="i8",
        hash="hi8",
        control=_OffscreenControl(),
    )
    wx.add_chat("Alice", raw)
    delivered = await adapter.poll_once()

    assert len(delivered) == 1
    assert delivered[0].image_paths == ()


async def test_self_image_messages_are_ignored(tmp_path):
    image = tmp_path / "self.png"
    image.write_bytes(b"fake-png")
    wx = FakeWx()
    adapter = _adapter(wx, config=_config(capture_images=True))
    await adapter.poll_once()

    raw = RawMessage(
        sender="self", attr="self", content="图片", type="image", id="i4",
        hash="hi4", image_path=str(image),
    )
    wx.add_chat("Alice", raw)

    assert await adapter.poll_once() == []
    assert raw.downloads == 0


async def test_stale_image_files_are_swept_on_start(tmp_path, monkeypatch):
    images = tmp_path / "images"
    images.mkdir()
    stale = images / "old.png"
    stale.write_bytes(b"png")
    fresh = images / "new.png"
    fresh.write_bytes(b"png")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    monkeypatch.setattr("app.wxauto_adapter._IMAGE_DIR", images)

    wx = FakeWx()
    adapter = _adapter(wx, config=_config(capture_images=True))
    adapter._sweep_stale_images()

    assert not stale.exists()
    assert fresh.exists()


async def test_start_polls_until_stopped():
    wx = FakeWx()
    wx.add_chat("Alice")
    received: list[str] = []

    async def handler(message):
        received.append(message.content)

    adapter = _adapter(wx, handler=handler)
    task = asyncio.create_task(adapter.start())
    try:
        for _ in range(40):
            if adapter.running and adapter._primed:
                break
            await asyncio.sleep(0.02)
        assert adapter.running

        wx.add_chat("Alice", RawMessage(sender="Alice", content="live", id="s1"))
        for _ in range(60):
            if received:
                break
            await asyncio.sleep(0.02)
    finally:
        adapter.stop()
        await asyncio.wait_for(task, timeout=2)

    assert received == ["live"]
