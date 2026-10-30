from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import MemoryConfig
from app.context_manager import ContextManager
from app.memory.extractor import ExtractionError, MemoryExtractor, MemoryWriteService
from app.memory.models import (
    MemoryCandidate,
    MemoryCategory,
    MemoryExtractionOutput,
    MemoryOperation,
    MemoryScope,
)
from app.memory.repository import BotStateRepository
from tests.conftest import (
    FakeExtractionRunner,
    make_database,
    make_message,
    make_repo,
)


def _config(**overrides) -> MemoryConfig:
    values = dict(
        enabled=True,
        allow_shared_memory=False,
        extraction_threshold=2,
        extraction_batch_size=10,
        extraction_max_attempts=3,
    )
    values.update(overrides)
    return MemoryConfig(**values)


def _build(tmp_path, runner, *, config=None):
    db, policy, repo = make_repo(tmp_path, shared=False)
    context = ContextManager()
    state = BotStateRepository(db)
    extractor = MemoryExtractor(
        instance_id="mika",
        persona_id="mika",
        context=context,
        repository=repo,
        state=state,
        config=config or _config(),
        runner=runner,
        retry_delay_seconds=0,
    )
    return extractor, context, repo, state


_SEED_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SEED_END = _SEED_START + timedelta(minutes=1)
_LATER_END = _SEED_END + timedelta(minutes=2)


def _seed_batch(context):
    base = _SEED_START
    context.add_message(
        make_message(
            1,
            content="我最近很喜欢文明6和策略游戏",
            author_id=300,
            created_at=base,
        )
    )
    context.add_message(
        make_message(
            2,
            content="我下个月要去北海道旅行",
            author_id=301,
            author_name="Bob",
            created_at=base + timedelta(minutes=1),
        )
    )


def _candidates(existing_id: int) -> MemoryExtractionOutput:
    return MemoryExtractionOutput(
        candidates=[
            MemoryCandidate(
                operation=MemoryOperation.ADD,
                user_id="300",
                scope=MemoryScope.PERSONA,
                category=MemoryCategory.PREFERENCE,
                content="喜欢文明6和策略游戏",
                importance=0.8,
                confidence=0.9,
                source_message_id="1",
            ),
            MemoryCandidate(
                operation=MemoryOperation.IGNORE,
                user_id="301",
                reason="small talk",
            ),
            MemoryCandidate(
                operation=MemoryOperation.UPDATE,
                user_id="301",
                scope=MemoryScope.PERSONA,
                category=MemoryCategory.PLAN,
                content="下个月要去北海道旅行",
                importance=0.9,
                confidence=0.9,
                target_memory_id=existing_id,
                ttl_days=90,
                source_message_id="2",
            ),
        ]
    )


async def test_extractor_applies_add_update_ignore_and_advances_cursor(tmp_path):
    runner = FakeExtractionRunner()
    extractor, context, repo, state = _build(tmp_path, runner)
    from app.memory.models import TrustedContext

    seeded = repo.add_memory(
        TrustedContext("mika", "mika", "100", "200", "301", "Bob", "0"),
        scope=MemoryScope.PERSONA,
        category="plan",
        content="我计划去北海道旅行",
        importance=0.7,
        confidence=0.8,
    )
    runner.outputs = [_candidates(seeded.id)]
    _seed_batch(context)

    results = await extractor.drain_channel((100, 200))

    assert len(results) == 1
    report = results[0].report
    assert report.added == 1
    assert report.updated == 1
    assert report.ignored == 1
    assert state.get_extraction_cursor("mika", "200") == _SEED_END

    ctx_300 = TrustedContext("mika", "mika", "100", "200", "300", "Alice", "1")
    ctx_301 = TrustedContext("mika", "mika", "100", "200", "301", "Bob", "2")
    assert repo.search_memories(ctx_300, "文明")[0].content == "喜欢文明6和策略游戏"
    assert repo.search_memories(ctx_301, "北海道")[0].content == "下个月要去北海道旅行"
    assert repo.get_memory(ctx_301, seeded.id).expires_at is not None

    # Nothing is extracted twice.
    assert await extractor.drain_channel((100, 200)) == []
    assert runner.calls == 1


async def test_extractor_requires_threshold(tmp_path):
    runner = FakeExtractionRunner(outputs=[MemoryExtractionOutput()])
    extractor, context, _repo, _state = _build(tmp_path, runner)
    context.add_message(make_message(1, content="hello"))
    assert await extractor.drain_channel((100, 200)) == []
    assert runner.calls == 0


async def test_attribution_mismatch_is_rejected(tmp_path):
    runner = FakeExtractionRunner()
    extractor, context, repo, _state = _build(tmp_path, runner)
    runner.outputs = [
        MemoryExtractionOutput(
            candidates=[
                MemoryCandidate(
                    operation=MemoryOperation.ADD,
                    user_id="300",
                    category=MemoryCategory.PLAN,
                    content="下个月要去北海道旅行",
                    importance=0.9,
                    confidence=0.9,
                )
            ]
        )
    ]
    _seed_batch(context)
    results = await extractor.drain_channel((100, 200))
    assert results[0].report.rejected == 1
    assert repo.count_active_memories() == 0


async def test_shared_candidate_rejected_when_disabled(tmp_path):
    runner = FakeExtractionRunner()
    extractor, context, repo, _state = _build(tmp_path, runner)
    runner.outputs = [
        MemoryExtractionOutput(
            candidates=[
                MemoryCandidate(
                    operation=MemoryOperation.ADD,
                    user_id="300",
                    scope=MemoryScope.SHARED,
                    category=MemoryCategory.PREFERENCE,
                    content="喜欢文明6和策略游戏",
                    importance=0.9,
                    confidence=0.9,
                )
            ]
        )
    ]
    _seed_batch(context)
    results = await extractor.drain_channel((100, 200))
    assert results[0].report.rejected == 1
    assert repo.count_active_memories() == 0


async def test_update_target_outside_persona_scope_is_rejected(tmp_path):
    _db, _policy, ren_repo = make_repo(tmp_path / "ren")
    from app.memory.models import TrustedContext

    ren_memory = ren_repo.add_memory(
        TrustedContext("ren", "ren", "100", "200", "301", "Bob", "0"),
        scope=MemoryScope.PERSONA,
        category="plan",
        content="我计划去北海道旅行",
        importance=0.8,
        confidence=0.8,
    )
    runner = FakeExtractionRunner()
    extractor, context, repo, _state = _build(tmp_path, runner)
    runner.outputs = [
        MemoryExtractionOutput(
            candidates=[
                MemoryCandidate(
                    operation=MemoryOperation.UPDATE,
                    user_id="301",
                    category=MemoryCategory.PLAN,
                    content="下个月要去北海道旅行",
                    importance=0.9,
                    confidence=0.9,
                    target_memory_id=ren_memory.id,
                )
            ]
        )
    ]
    _seed_batch(context)
    results = await extractor.drain_channel((100, 200))
    assert results[0].report.rejected == 1
    assert repo.count_active_memories() == 0


async def test_model_failure_is_retried_then_succeeds(tmp_path):
    runner = FakeExtractionRunner()
    extractor, context, repo, state = _build(tmp_path, runner)
    runner.outputs = [
        ExtractionError("temporary outage"),
        MemoryExtractionOutput(
            candidates=[
                MemoryCandidate(
                    operation=MemoryOperation.ADD,
                    user_id="300",
                    category=MemoryCategory.PREFERENCE,
                    content="喜欢文明6和策略游戏",
                    importance=0.9,
                    confidence=0.9,
                )
            ]
        ),
    ]
    _seed_batch(context)

    first = await extractor.drain_channel((100, 200))
    assert first[0].skipped is True
    assert first[-1].report.added == 1
    assert context.pending_extraction_count((100, 200)) == 0
    assert state.get_extraction_cursor("mika", "200") == _SEED_END
    assert repo.count_active_memories() == 1
    assert runner.calls == 2


async def test_permanent_failure_does_not_block_future_batches(tmp_path):
    runner = FakeExtractionRunner()
    extractor, context, _repo, state = _build(
        tmp_path, runner, config=_config(extraction_max_attempts=1)
    )
    runner.outputs = [
        ExtractionError("permanent"),
        MemoryExtractionOutput(
            candidates=[
                MemoryCandidate(
                    operation=MemoryOperation.ADD,
                    user_id="300",
                    category=MemoryCategory.PREFERENCE,
                    content="喜欢文明6和策略游戏",
                    importance=0.9,
                    confidence=0.9,
                )
            ]
        ),
    ]
    _seed_batch(context)
    first = await extractor.drain_channel((100, 200))
    assert first[0].skipped is True
    assert context.pending_extraction_count((100, 200)) == 0
    assert state.get_extraction_cursor("mika", "200") == _SEED_END

    context.add_message(
        make_message(
            3,
            content="我喜欢文明6和策略游戏",
            author_id=300,
            created_at=_SEED_END + timedelta(minutes=1),
        )
    )
    context.add_message(
        make_message(4, content="继续聊", author_id=300, created_at=_LATER_END)
    )
    second = await extractor.drain_channel((100, 200))
    assert second[0].report.added == 1
    assert state.get_extraction_cursor("mika", "200") == _LATER_END


def test_write_service_programmatically_deduplicates_add(tmp_path):
    _db, policy, repo = make_repo(tmp_path)
    service = MemoryWriteService(repo, policy, _config())
    from app.memory.models import TrustedContext

    ctx = TrustedContext("mika", "mika", "100", "200", "300", "Alice", "1")
    report1 = service.apply_candidates(
        ctx,
        [make_message(1, content="我喜欢文明6和策略游戏", author_id=300)],
        [
            MemoryCandidate(
                operation=MemoryOperation.ADD,
                user_id="300",
                category=MemoryCategory.PREFERENCE,
                content="喜欢文明6和策略游戏",
                importance=0.7,
                confidence=0.8,
            )
        ],
    )
    report2 = service.apply_candidates(
        ctx,
        [make_message(2, content="我还是很喜欢文明6和策略游戏", author_id=300)],
        [
            MemoryCandidate(
                operation=MemoryOperation.ADD,
                user_id="300",
                category=MemoryCategory.PREFERENCE,
                content="喜欢文明6和策略游戏",
                importance=0.9,
                confidence=0.9,
            )
        ],
    )
    assert report1.added == 1
    assert report2.updated == 1
    assert repo.count_active_memories() == 1
