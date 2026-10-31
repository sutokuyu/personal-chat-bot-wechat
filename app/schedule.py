"""Local-time activity window.

A person is not reachable around the clock, so the bot only answers while its
owner could plausibly be at the keyboard. Reading continues outside the window
(context stays fresh); only the visible act of sending is suppressed.
"""

from __future__ import annotations

from datetime import datetime, time

_CLOCK_FORMAT_ERROR = "active hours must use 24-hour HH:MM format"


def parse_clock(value: str) -> time:
    try:
        hours, minutes = value.split(":")
        parsed = time(int(hours), int(minutes))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(_CLOCK_FORMAT_ERROR) from exc
    if len(hours) != 2 or len(minutes) != 2:
        raise ValueError(_CLOCK_FORMAT_ERROR)
    return parsed


def resolve_window(
    start: str | None, end: str | None
) -> tuple[time, time] | None:
    """Parse a configured window; ``None`` means the bot is always awake."""

    if not start or not end:
        return None
    return parse_clock(start), parse_clock(end)


def is_within_window(now: datetime, start: time, end: time) -> bool:
    """True inside ``[start, end)``; a window with ``start > end`` wraps midnight."""

    if start == end:
        return True
    current = now.time()
    if start < end:
        return start <= current < end
    return current >= start or current < end


__all__ = ["is_within_window", "parse_clock", "resolve_window"]
