from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agent.prompts import build_agent_input, format_clock
from tests.conftest import make_message

JST = timezone(timedelta(hours=9))


def test_clock_lists_absolute_and_relative_dates():
    now = datetime(2026, 10, 31, 1, 21, tzinfo=JST)

    clock = format_clock(now)

    assert clock.startswith("2026-10-31 01:21（周六，UTC+09:00）")
    assert "今天 2026-10-31（周六）" in clock
    assert "明天 2026-11-01（周日）" in clock
    assert "昨天 2026-10-30（周五）" in clock


def test_clock_handles_month_and_year_rollover():
    clock = format_clock(datetime(2026, 12, 31, 23, 30, tzinfo=JST))

    assert "今天 2026-12-31（周四）" in clock
    assert "明天 2027-01-01（周五）" in clock


def test_clock_uses_the_local_timezone_of_the_machine():
    utc_midnight = datetime(2026, 10, 30, 16, 0, tzinfo=timezone.utc)

    clock = format_clock(utc_midnight)

    # Rendered in the machine's local zone; only the shape is asserted.
    assert clock.count("（") >= 4
    assert "今天 " in clock and "明天 " in clock


def test_agent_input_carries_the_clock():
    message = make_message(1, content="明天天气怎么样")

    text = build_agent_input([], message, now=datetime(2026, 10, 31, 1, 21, tzinfo=JST))

    assert text.startswith("Current date and time: 2026-10-31 01:21")
    assert "明天 2026-11-01（周日）" in text
    assert "Never say you do not know today's date" in text
    assert "明天天气怎么样" in text


def test_judge_prompt_carries_the_clock():
    from app.agent.prompts import build_participation_prompt

    message = make_message(2, content="明天要不要出去玩")
    _system, user = build_participation_prompt("persona", "transcript", message)

    assert user.startswith("Current date and time: ")
    assert "今天 " in user and "明天 " in user
