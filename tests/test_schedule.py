from __future__ import annotations

from datetime import datetime

import pytest

from app.schedule import is_within_window, parse_clock, resolve_window


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 31, hour, minute)


def test_parse_clock_rejects_bad_values():
    for value in ("8:00", "24:00", "08:60", "0800", "", "aa:bb"):
        with pytest.raises(ValueError):
            parse_clock(value)


def test_resolve_window_disables_the_check_when_unset():
    assert resolve_window(None, None) is None
    assert resolve_window("", "") is None


def test_window_wraps_past_midnight():
    start, end = parse_clock("08:00"), parse_clock("01:00")

    assert not is_within_window(_at(3, 30), start, end)
    assert is_within_window(_at(8, 0), start, end)
    assert is_within_window(_at(23, 59), start, end)
    assert is_within_window(_at(0, 59), start, end)
    assert not is_within_window(_at(1, 0), start, end)


def test_same_start_and_end_means_always_awake():
    moment = parse_clock("00:00")

    assert is_within_window(_at(4, 0), moment, moment)
