from __future__ import annotations

import asyncio

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
