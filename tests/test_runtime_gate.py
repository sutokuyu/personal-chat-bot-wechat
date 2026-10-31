from __future__ import annotations

import asyncio
from datetime import datetime

from langchain_core.messages import AIMessage

from app.agent.agent_factory import AgentResult
from app.config import ParticipationConfig, ReplyConfig
from app.context_manager import ContextManager, render_transcript
from app.errors import AgentTimeoutError
from app.memory.repository import BotStateRepository
from app.participation_gate import (
    CooldownStore,
    ParticipationDecision,
    ParticipationGate,
)
from app.runtime import BotRuntime, create_runtime
from app.wxauto_adapter import chat_id_for, stable_id
from tests.conftest import (
    FakeAgentRunner,
    FakeExtractionRunner,
    FakeJudge,
    FakeSender,
    ScriptedChatModel,
    make_database,
    make_loaded,
    make_message,
)


def _gate(
    tmp_path,
    *,
    judge=None,
    participation: ParticipationConfig | None = None,
    reply: ReplyConfig | None = None,
    random_fn=None,
):
    db = make_database(tmp_path)
    state = BotStateRepository(db)
    context = ContextManager()
    cooldown = CooldownStore(state, "deepseek-chan")
    gate = ParticipationGate(
        instance_id="deepseek-chan",
        participation=participation or ParticipationConfig(max_context_messages=10),
        reply=reply or ReplyConfig(channel_cooldown_seconds=5, user_cooldown_seconds=3),
        context=context,
        cooldowns=cooldown,
        judge=judge,
        merge_window_seconds=3,
        random_fn=random_fn,
    )
    return gate, context, state, cooldown


async def test_mention_is_forced_and_ignores_probability(tmp_path):
    judge = FakeJudge()
    gate, _context, _state, _cooldown = _gate(
        tmp_path,
        judge=judge,
        participation=ParticipationConfig(reply_probability=0.0),
    )
    decision = await gate.evaluate(
        make_message(1, content="@DeepSeek-chan hi", is_mention=True), recent=[]
    )
    assert decision.should_reply and decision.forced
    assert judge.calls == 0


async def test_zero_probability_silences_normal_messages(tmp_path):
    judge = FakeJudge()
    gate, _context, _state, _cooldown = _gate(
        tmp_path,
        judge=judge,
        participation=ParticipationConfig(reply_probability=0.0),
    )
    decision = await gate.evaluate(make_message(2, content="hello"), recent=[])
    assert not decision.should_reply
    assert judge.calls == 0


async def test_other_mention_is_skipped(tmp_path):
    judge = FakeJudge()
    gate, _context, _state, _cooldown = _gate(tmp_path, judge=judge)
    decision = await gate.evaluate(
        make_message(3, content="@小王 hi", mentions_other=True), recent=[]
    )
    assert not decision.should_reply
    assert "mentions someone else" in decision.reason
    assert judge.calls == 0


async def test_continuation_bypasses_probability_and_cooldowns(tmp_path):
    judge = FakeJudge(
        decisions=[ParticipationDecision(should_reply=False, reason="no")]
    )
    reply = ReplyConfig(
        channel_cooldown_seconds=20,
        user_cooldown_seconds=10,
        continuation_window_seconds=120,
        continuation_channel_cooldown_seconds=0,
        continuation_user_cooldown_seconds=0,
    )
    gate, _context, _state, cooldowns = _gate(tmp_path, judge=judge, reply=reply)
    first = make_message(10, content="hello", author_id=300)
    await gate.record_reply(first)
    follow = make_message(11, content="and then?", author_id=300)

    assert cooldowns.is_continuation(follow, 120)
    decision = await gate.evaluate(follow, recent=[first, follow])
    assert decision.should_reply and decision.forced
    assert decision.reason == "conversation continuation"
    assert judge.calls == 0


async def test_cooldown_decisions_expose_retry_delay(tmp_path):
    gate, _context, _state, _cooldowns = _gate(tmp_path, judge=FakeJudge())
    first = make_message(50, content="hi", author_id=300)
    await gate.record_reply(first)

    blocked = make_message(51, content="hello", author_id=301)
    decision = await gate.evaluate(blocked, recent=[first, blocked])

    assert not decision.should_reply
    assert 0 < decision.retry_after <= 5


async def test_reprocess_bypasses_duplicate_check(tmp_path):
    gate, _context, _state, _cooldowns = _gate(tmp_path, judge=FakeJudge())
    message = make_message(60, content="hello", author_id=300)

    assert (await gate.evaluate(message, recent=[message])).should_reply
    assert not (await gate.evaluate(message, recent=[message])).should_reply
    assert (
        await gate.evaluate(message, recent=[message], reprocess=True)
    ).should_reply


async def test_runtime_retries_message_blocked_by_cooldown(tmp_path):
    loaded = make_loaded("deepseek-chan")
    reply = ReplyConfig(
        channel_cooldown_seconds=0.2,
        user_cooldown_seconds=0.0,
        continuation_window_seconds=0.0,
        retry_on_cooldown=True,
        max_cooldown_retry_seconds=5,
    )
    loaded = loaded.model_copy(
        update={"config": loaded.config.model_copy(update={"reply": reply})}
    )
    sender = FakeSender()
    runtime = BotRuntime(
        loaded,
        agent_runner=FakeAgentRunner(),
        sender=sender,
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
    )
    try:
        first = make_message(40, content="hi", is_mention=True, author_id=300)
        await runtime.handle_message(first)
        assert len(sender.sent) == 1

        second = make_message(41, content="我也说一句", author_id=301)
        await runtime.handle_message(second)
        assert len(sender.sent) == 1, "the second message should hit the cooldown"

        await asyncio.sleep(0.9)
        assert len(sender.sent) == 2, "the retry should answer the blocked message"
    finally:
        await runtime.close()


async def test_runtime_replies_and_stores_self_reply(tmp_path):
    runtime = BotRuntime(
        make_loaded("deepseek-chan"),
        agent_runner=FakeAgentRunner(),
        sender=FakeSender(),
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
    )
    await runtime.handle_message(make_message(20, content="hi", is_mention=True))
    assert len(runtime.sender.sent) == 1
    history = runtime.context.recent((100, 200))
    assert any(item.is_self for item in history)
    assert [item.message_id for item in history if not item.is_self] == [20]
    await runtime.close()


async def test_runtime_restores_recent_user_and_bot_messages_after_restart(tmp_path):
    loaded = make_loaded("deepseek-chan")
    reply = ReplyConfig(
        channel_cooldown_seconds=0,
        user_cooldown_seconds=0,
        continuation_window_seconds=0,
    )
    loaded = loaded.model_copy(
        update={"config": loaded.config.model_copy(update={"reply": reply})}
    )
    database = make_database(tmp_path)
    account_id = loaded.config.wechat.account_id
    guild_id = stable_id("account", account_id)
    channel_id = chat_id_for(account_id, loaded.config.wechat.chats[0].name)

    first_runner = FakeAgentRunner(result=AgentResult(text="之前我们聊了旅行。"))
    first = BotRuntime(
        loaded,
        agent_runner=first_runner,
        sender=FakeSender(),
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=database,
    )
    await first.handle_message(
        make_message(
            80,
            content="我们刚才聊到什么？",
            guild_id=guild_id,
            channel_id=channel_id,
            is_mention=True,
        )
    )
    await first.close()

    second_runner = FakeAgentRunner()
    second = BotRuntime(
        loaded,
        agent_runner=second_runner,
        sender=FakeSender(),
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=database,
    )
    try:
        restored = second.context.recent((guild_id, channel_id))
        assert [item.content for item in restored] == [
            "我们刚才聊到什么？",
            "之前我们聊了旅行。",
        ]
        assert restored[-1].is_self

        await second.handle_message(
            make_message(
                81,
                content="那你建议去哪？",
                guild_id=guild_id,
                channel_id=channel_id,
                is_mention=True,
            )
        )
        agent_input = second_runner.calls[0][0]
        assert "我们刚才聊到什么？" in agent_input
        assert "之前我们聊了旅行。" in agent_input
        assert "那你建议去哪？" in agent_input
    finally:
        await second.close()


async def test_runtime_stays_silent_outside_active_hours(tmp_path):
    loaded = make_loaded("deepseek-chan")
    wechat = loaded.config.wechat.model_copy(
        update={"active_hours_start": "08:00", "active_hours_end": "01:00"}
    )
    loaded = loaded.model_copy(
        update={"config": loaded.config.model_copy(update={"wechat": wechat})}
    )
    runner = FakeAgentRunner()
    sender = FakeSender()
    runtime = BotRuntime(
        loaded,
        agent_runner=runner,
        sender=sender,
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
        now_fn=lambda: datetime(2026, 10, 31, 3, 30),
    )
    try:
        await runtime.handle_message(
            make_message(90, content="这么晚还没睡？", is_mention=True)
        )
        assert sender.sent == []
        assert runner.calls == [], "the agent must not run during silent hours"
        assert [item.message_id for item in runtime.context.recent((100, 200))] == [90]
    finally:
        await runtime.close()


async def test_runtime_answers_inside_active_hours(tmp_path):
    loaded = make_loaded("deepseek-chan")
    wechat = loaded.config.wechat.model_copy(
        update={"active_hours_start": "08:00", "active_hours_end": "01:00"}
    )
    loaded = loaded.model_copy(
        update={"config": loaded.config.model_copy(update={"wechat": wechat})}
    )
    sender = FakeSender()
    runtime = BotRuntime(
        loaded,
        agent_runner=FakeAgentRunner(),
        sender=sender,
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
        now_fn=lambda: datetime(2026, 10, 31, 12, 0),
    )
    try:
        await runtime.handle_message(
            make_message(91, content="中午吃啥？", is_mention=True)
        )
        assert len(sender.sent) == 1
    finally:
        await runtime.close()


async def test_runtime_agent_failure_is_controlled(tmp_path):
    runtime = BotRuntime(
        make_loaded("deepseek-chan"),
        agent_runner=FakeAgentRunner(result=AgentTimeoutError("slow")),
        sender=FakeSender(),
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
    )
    await runtime.handle_message(make_message(21, content="hi", is_mention=True))
    assert runtime.sender.sent == []
    await runtime.close()


async def test_create_runtime_uses_real_langchain_agent(tmp_path):
    model = ScriptedChatModel(responses=[AIMessage(content="微信这边收到。")])
    runtime = create_runtime(
        make_loaded("deepseek-chan"),
        model_factory=lambda _config: model,
        database=make_database(tmp_path),
    )
    sender = FakeSender()
    runtime.set_sender(sender)

    await runtime.handle_message(make_message(22, content="hi", is_mention=True))

    assert sender.sent and sender.sent[0]["text"] == "微信这边收到。"
    assert len(model.calls) == 1
    await runtime.close()


def test_transcript_marks_self_reply_as_you():
    context = ContextManager()
    context.add_message(make_message(1, content="hello"))
    own = context.add_bot_reply(
        guild_id=100,
        channel_id=200,
        author_name="deepseek-chan",
        content="我在",
    )
    rendered = render_transcript(context.recent((100, 200)), merge_window_seconds=0)
    assert f"[{own.message_id}]" in rendered
    assert "[you]" in rendered


async def test_low_interest_message_is_silenced_even_if_judge_says_reply(tmp_path):
    judge = FakeJudge(
        decisions=[
            ParticipationDecision(should_reply=True, interest=0.2, reason="meh")
        ]
    )
    gate, _context, _state, _cooldowns = _gate(
        tmp_path,
        judge=judge,
        participation=ParticipationConfig(min_interest=0.55),
    )
    decision = await gate.evaluate(make_message(70, content="几点了"), recent=[])
    assert not decision.should_reply
    assert "interest 0.20" in decision.reason


async def test_topic_window_blocks_a_second_person_on_the_same_topic(tmp_path):
    judge = FakeJudge()
    gate, _context, _state, _cooldowns = _gate(
        tmp_path,
        judge=judge,
        participation=ParticipationConfig(
            topic_window_seconds=120, topic_window_reply_probability=0.0
        ),
        reply=ReplyConfig(channel_cooldown_seconds=0, user_cooldown_seconds=0),
    )
    first = make_message(80, content="今晚吃什么", author_id=300)
    await gate.record_reply(first, text="吃面吧")

    second = make_message(81, content="我也想吃面", author_id=301)
    decision = await gate.evaluate(second, recent=[first, second])

    assert not decision.should_reply
    assert judge.calls == 0


async def test_topic_window_passes_what_the_persona_just_said_to_the_judge(tmp_path):
    judge = FakeJudge()
    gate, _context, _state, _cooldowns = _gate(
        tmp_path,
        judge=judge,
        participation=ParticipationConfig(
            topic_window_seconds=120, topic_window_reply_probability=1.0
        ),
        reply=ReplyConfig(channel_cooldown_seconds=0, user_cooldown_seconds=0),
    )
    first = make_message(90, content="今晚吃什么", author_id=300)
    await gate.record_reply(first, text="吃面吧")

    second = make_message(91, content="那家面馆关门了", author_id=301)
    decision = await gate.evaluate(second, recent=[first, second])

    assert decision.should_reply
    assert judge.recent_reply_hints[-1] == "吃面吧"


async def test_mention_bypasses_topic_window(tmp_path):
    judge = FakeJudge()
    gate, _context, _state, _cooldowns = _gate(
        tmp_path,
        judge=judge,
        participation=ParticipationConfig(
            topic_window_seconds=120, topic_window_reply_probability=0.0
        ),
        reply=ReplyConfig(channel_cooldown_seconds=0, user_cooldown_seconds=0),
    )
    first = make_message(95, content="今晚吃什么", author_id=300)
    await gate.record_reply(first, text="吃面吧")

    mention = make_message(
        96, content="@DeepSeek-chan 你说呢", author_id=301, is_mention=True
    )
    decision = await gate.evaluate(mention, recent=[first, mention])

    assert decision.should_reply and decision.forced


async def test_long_reply_is_sent_as_several_bubbles(tmp_path):
    text = "第一句话在这里说完了，还有一点补充。第二句话接着说，别担心。"
    loaded = make_loaded("deepseek-chan")
    reply = loaded.config.reply.model_copy(
        update={
            "split_replies": True,
            "split_min_chars": 20,
            "split_max_parts": 3,
            "split_probability": 1.0,
            "split_gap_min_seconds": 0.0,
            "split_gap_max_seconds": 0.0,
        }
    )
    loaded = loaded.model_copy(
        update={"config": loaded.config.model_copy(update={"reply": reply})}
    )
    sender = FakeSender()
    runtime = BotRuntime(
        loaded,
        agent_runner=FakeAgentRunner(result=AgentResult(text=text)),
        sender=sender,
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
        random_fn=lambda: 0.0,
    )
    await runtime.handle_message(make_message(30, content="hi", is_mention=True))

    assert len(sender.sent) >= 2
    assert "".join(part["text"] for part in sender.sent) == text
    stored = [item for item in runtime.context.recent((100, 200)) if item.is_self]
    assert stored and stored[-1].content == text
    await runtime.close()


async def test_model_chosen_split_points_win_over_the_heuristic(tmp_path):
    text = "等我一下。<split/>我去看看那个文件。"
    loaded = make_loaded("deepseek-chan")
    reply = loaded.config.reply.model_copy(
        update={
            "split_replies": True,
            "split_min_chars": 1000,  # heuristic would never split
            "split_probability": 0.0,
            "split_gap_min_seconds": 0.0,
            "split_gap_max_seconds": 0.0,
        }
    )
    loaded = loaded.model_copy(
        update={"config": loaded.config.model_copy(update={"reply": reply})}
    )
    sender = FakeSender()
    runtime = BotRuntime(
        loaded,
        agent_runner=FakeAgentRunner(result=AgentResult(text=text)),
        sender=sender,
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
    )
    await runtime.handle_message(make_message(31, content="hi", is_mention=True))

    assert [part["text"] for part in sender.sent] == [
        "等我一下。",
        "我去看看那个文件。",
    ]
    assert all("<split" not in part["text"] for part in sender.sent)
    await runtime.close()


async def test_split_markers_never_leak_when_splitting_is_disabled(tmp_path):
    text = "等我一下。<split/>我去看看那个文件。"
    loaded = make_loaded("deepseek-chan")
    reply = loaded.config.reply.model_copy(update={"split_replies": False})
    loaded = loaded.model_copy(
        update={"config": loaded.config.model_copy(update={"reply": reply})}
    )
    sender = FakeSender()
    runtime = BotRuntime(
        loaded,
        agent_runner=FakeAgentRunner(result=AgentResult(text=text)),
        sender=sender,
        judge=FakeJudge(),
        extraction_runner=FakeExtractionRunner(),
        database=make_database(tmp_path),
    )
    await runtime.handle_message(make_message(32, content="hi", is_mention=True))

    assert [part["text"] for part in sender.sent] == ["等我一下。我去看看那个文件。"]
    await runtime.close()
