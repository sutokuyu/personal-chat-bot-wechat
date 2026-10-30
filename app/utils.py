"""Small time and text helpers shared by storage and business logic."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone

_WORD_RE = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    """Serialize a datetime to a stable, lexicographically sortable UTC string."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    """Parse timestamps written by :func:`isoformat` (and older values)."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def try_parse_iso(value: str | None) -> datetime | None:
    """Parse a timestamp, returning None for legacy or malformed values."""

    if not value:
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


def expires_in(days: int | None, *, now: datetime | None = None) -> str | None:
    if days is None:
        return None
    return isoformat((now or utcnow()) + timedelta(days=days))


def normalize_text(value: str) -> str:
    """Normalize text for duplicate detection (not for display)."""

    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def tokenize(value: str) -> set[str]:
    """Tokenize English words and individual CJK characters for simple scoring."""

    tokens: set[str] = set()
    for word in _WORD_RE.findall(value.casefold()):
        if any("\u4e00" <= ch <= "\u9fff" for ch in word):
            tokens.update(ch for ch in word if ch.strip())
        elif len(word) >= 2:
            tokens.add(word)
    return tokens


def truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3].rstrip() + "..."
