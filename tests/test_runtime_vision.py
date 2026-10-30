from __future__ import annotations

import asyncio
from dataclasses import replace

from app.config import WeChatChatConfig
from app.runtime import BotRuntime
from app.wxauto_adapter import chat_id_for
from tests.conftest import (
    FakeAgentRunner,
    FakeExtractionRunner,
    FakeImageDescriber,
    FakeJudge,
    FakeSender,
    make_database,
    make_loaded,
    make_message,
)


def _loaded(
    tmp_path,
    *,
    followup_seconds: float | None = None,
    cache_size: int | None = None,
    max_images: int | None = None,
    chat_size: int | None = None,
):
    loaded = make_loaded("deepseek-chan")
    chat: dict = {"name": "Alice", "kind": "direct"}
    if chat_size is not None:
        chat["image_cache_size"] = chat_size
    update: dict = {"chats": [WeChatChatConfig(**chat)], "capture_images": True}
    if followup_seconds is not None:
        update["image_followup_seconds"] = followup_seconds
    if cache_size is not None:
        update["image_cache_size"] = cache_size
    if max_images is not None:
        update["image_followup_max_images"] = max_images
    wechat = loaded.config.wechat.model_copy(update=update)
    # Cooldowns would swallow the follow-up messages this file tests.
    reply = loaded.config.reply.model_copy(
        update={
            "channel_cooldown_seconds": 0.0,
            "user_cooldown_seconds": 0.0,
            "continuation_channel_cooldown_seconds": 0.0,
            "continuation_user_cooldown_seconds": 0.0,
        }
    )
    config = loaded.config.model_copy(update={"wechat": wechat, "reply": reply})
    return loaded.model_copy(update={"config": config})


def _make_runtime(loaded, tmp_path, agent, describer=None):
    return BotRuntime(
        loaded,
        agent_runner=agent,
        sender=FakeSender(),
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
        image_describer=describer,
    )


def _image_message(tmp_path, name: str, message_id: int, **kwargs):
    image = tmp_path / name
    image.write_bytes(b"fake-png")
    message = replace(
        make_message(message_id, **kwargs),
        image_paths=(str(image),),
    )
    return message, image


async def test_image_message_is_described_and_passed_to_the_agent(tmp_path):
    message, image = _image_message(
        tmp_path, "cat.png", 70, content="[图片]", is_mention=True
    )
    describer = FakeImageDescriber(captions=["一只橘猫躺在沙发上"])
    agent = FakeAgentRunner()
    runtime = _make_runtime(_loaded(tmp_path), tmp_path, agent, describer)
    try:
        await runtime.handle_message(message)

        assert describer.paths == [str(image)]
        assert agent.calls and agent.calls[0][2] == [str(image)]
        assert "一只橘猫躺在沙发上" in agent.calls[0][0]

        stored = runtime.context.recent((100, 200))[0]
        assert stored.content.startswith("[图片]")
        assert "橘猫" in stored.content
        assert image.exists(), "the image must stay available for a follow-up"
    finally:
        await runtime.close()
    assert not image.exists(), "closing the runtime must delete cached images"


async def test_image_message_without_describer_still_reaches_the_agent(tmp_path):
    message, image = _image_message(
        tmp_path, "cat.png", 71, content="[图片]", is_mention=True
    )
    agent = FakeAgentRunner()
    runtime = _make_runtime(_loaded(tmp_path), tmp_path, agent)
    try:
        await runtime.handle_message(message)

        assert agent.calls and agent.calls[0][2] == [str(image)]
        assert "未能识别" in agent.calls[0][0]
    finally:
        await runtime.close()
    assert not image.exists()


async def test_images_are_deleted_when_retention_is_off(tmp_path):
    message, image = _image_message(tmp_path, "meal.png", 82, content="[图片]")
    agent = FakeAgentRunner()
    runtime = _make_runtime(
        _loaded(tmp_path, followup_seconds=0), tmp_path, agent, FakeImageDescriber()
    )
    try:
        await runtime.handle_message(message)
        assert not image.exists()

        await runtime.handle_message(make_message(83, content="这个怎么样？"))
        assert agent.calls[1][2] is None
    finally:
        await runtime.close()


async def test_followup_question_attaches_cached_images_with_sender_context(tmp_path):
    first, image = _image_message(tmp_path, "meal.png", 80, content="[图片]")
    followup = make_message(81, content="这个猫粮怎么样？")

    describer = FakeImageDescriber(captions=["一袋猫粮包装"])
    agent = FakeAgentRunner()
    judge = FakeJudge()
    runtime = BotRuntime(
        _loaded(tmp_path),
        agent_runner=agent,
        sender=FakeSender(),
        judge=judge,
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
        image_describer=describer,
    )
    try:
        await runtime.handle_message(first)
        await runtime.handle_message(followup)

        assert len(agent.calls) == 2
        assert agent.calls[1][2] == [str(image)]
        prompt = agent.calls[1][0]
        assert "<recent_images>" in prompt
        assert "Alice" in prompt
        assert "一袋猫粮包装" in prompt
        assert "attached to this message are, in order: img 1" in prompt
        assert describer.paths == [str(image)], "the image is described only once"
    finally:
        await runtime.close()


async def test_judge_sees_the_cached_image_captions(tmp_path):
    # A continuation from the same sender skips the judge, so ask as someone
    # else: the judge must still know a picture is available.
    first, image = _image_message(tmp_path, "meal.png", 105, content="[图片]")
    question = make_message(
        106, content="这张图里是什么？", author_id=999, author_name="Bob"
    )

    describer = FakeImageDescriber(captions=["一袋猫粮包装"])
    agent = FakeAgentRunner()
    judge = FakeJudge()
    runtime = BotRuntime(
        _loaded(tmp_path),
        agent_runner=agent,
        sender=FakeSender(),
        judge=judge,
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
        image_describer=describer,
    )
    try:
        await runtime.handle_message(first)
        await runtime.handle_message(question)

        assert judge.image_contexts, "the judge should have been consulted"
        assert "一袋猫粮包装" in judge.image_contexts[-1]
        assert "Alice" in judge.image_contexts[-1]
        assert agent.calls[-1][2] == [str(image)]
    finally:
        await runtime.close()


async def test_fifo_cache_keeps_only_the_configured_number(tmp_path):
    one, one_image = _image_message(tmp_path, "one.png", 90, content="[图片]")
    two, two_image = _image_message(tmp_path, "two.png", 91, content="[图片]")
    three, three_image = _image_message(tmp_path, "three.png", 92, content="[图片]")
    followup = make_message(93, content="第三张是什么？")

    agent = FakeAgentRunner()
    runtime = _make_runtime(
        _loaded(tmp_path, cache_size=2), tmp_path, agent, FakeImageDescriber()
    )
    try:
        for message in (one, two, three):
            await runtime.handle_message(message)
        await runtime.handle_message(followup)

        assert not one_image.exists(), "the oldest image must be evicted first"
        assert two_image.exists() and three_image.exists()
        assert agent.calls[-1][2] == [str(two_image), str(three_image)]
    finally:
        await runtime.close()


async def test_per_chat_limit_overrides_the_default(tmp_path):
    # The adapter derives channel ids from the chat name, so the limit lookup
    # needs the real id rather than the fixed one make_message() uses.
    channel_id = chat_id_for("wechat-main", "Alice")
    one, one_image = _image_message(
        tmp_path, "one.png", 94, content="[图片]", channel_id=channel_id
    )
    two, two_image = _image_message(
        tmp_path, "two.png", 95, content="[图片]", channel_id=channel_id
    )
    followup = make_message(96, content="刚才的图呢？", channel_id=channel_id)

    agent = FakeAgentRunner()
    runtime = _make_runtime(
        _loaded(tmp_path, cache_size=10, chat_size=1),
        tmp_path,
        agent,
        FakeImageDescriber(),
    )
    try:
        await runtime.handle_message(one)
        await runtime.handle_message(two)
        await runtime.handle_message(followup)

        assert not one_image.exists()
        assert agent.calls[-1][2] == [str(two_image)]
    finally:
        await runtime.close()


async def test_several_images_in_one_message_are_all_cached(tmp_path):
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    for path in (first, second):
        path.write_bytes(b"fake-png")
    message = replace(
        make_message(97, content="[图片]"),
        image_paths=(str(first), str(second)),
    )
    followup = make_message(98, content="这两张有什么区别？")

    agent = FakeAgentRunner()
    runtime = _make_runtime(
        _loaded(tmp_path, max_images=3),
        tmp_path,
        agent,
        FakeImageDescriber(captions=["第一张", "第二张"]),
    )
    try:
        await runtime.handle_message(message)
        assert agent.calls[0][2] == [str(first), str(second)]

        await runtime.handle_message(followup)
        assert agent.calls[1][2] == [str(first), str(second)]
        assert "第一张" in agent.calls[1][0] and "第二张" in agent.calls[1][0]
    finally:
        await runtime.close()


async def test_followup_prefers_images_from_the_same_sender(tmp_path):
    other_message, _other_image = _image_message(
        tmp_path, "other.png", 100, content="[图片]", author_id=999, author_name="Bob"
    )
    my_message, my_image = _image_message(
        tmp_path, "mine.png", 101, content="[图片]", author_id=300, author_name="Alice"
    )
    followup = make_message(102, content="我那张图里是什么？", author_id=300)

    agent = FakeAgentRunner()
    runtime = _make_runtime(
        _loaded(tmp_path, max_images=1),
        tmp_path,
        agent,
        FakeImageDescriber(captions=["Bob 的图", "Alice 的图"]),
    )
    try:
        await runtime.handle_message(other_message)
        await runtime.handle_message(my_message)
        await runtime.handle_message(followup)

        assert agent.calls[-1][2] == [str(my_image)], "same sender wins over newer"
        prompt = agent.calls[-1][0]
        assert "Bob" in prompt and "Alice" in prompt
    finally:
        await runtime.close()


async def test_cached_images_expire_after_the_window(tmp_path):
    message, image = _image_message(tmp_path, "meal.png", 103, content="[图片]")
    followup = make_message(104, content="这张图呢？")
    agent = FakeAgentRunner()
    runtime = _make_runtime(
        _loaded(tmp_path, followup_seconds=0.05),
        tmp_path,
        agent,
        FakeImageDescriber(),
    )
    try:
        await runtime.handle_message(message)
        await asyncio.sleep(0.1)
        await runtime.handle_message(followup)

        assert agent.calls[-1][2] is None
        assert not image.exists()
        assert "<recent_images>" not in agent.calls[-1][0]
    finally:
        await runtime.close()
