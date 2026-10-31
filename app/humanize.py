"""Pacing helpers that make outbound traffic look hand-typed.

Nothing here knows about WeChat or the LLM: these are pure functions so the
timing rules stay testable and identical across transports.
"""

from __future__ import annotations

import random
import re
from typing import Callable

Rand = Callable[[], float]

# The model marks where it would hit "send" in a real chat. Any of these spellings
# is accepted because models are inconsistent with self-closing tags.
SPLIT_MARKER = "<split/>"
_MARKER_RE = re.compile(r"<\s*/?\s*split\s*/?\s*>|\[\s*split\s*\]", re.IGNORECASE)

# One sentence per match, keeping the delimiter. Newlines end a sentence too,
# because people press Enter instead of typing a full stop.
_SENTENCE_RE = re.compile(r"[^。！？!?；;…\n]*[。！？!?；;…]+|[^。！？!?；;…\n]+")

# A trailing bubble shorter than this reads like an accident, so merge it up.
_MIN_PART_CHARS = 8
# A first sentence this short usually stands alone as its own bubble.
_SHORT_OPENER_CHARS = 30


def strip_split_markers(text: str) -> str:
    """Remove split markers, for cases where the reply is sent in one piece."""

    return _MARKER_RE.sub("", text or "").strip()


# Prefixes left behind when a length cap cuts through a marker.
_DANGLING_MARKER_TAILS = frozenset(
    {"", "s", "sp", "spl", "spli", "split", "split/", "/s", "/sp", "/spl", "/spli", "/split"}
)


def drop_dangling_marker(text: str) -> str:
    """Drop a ``<split...`` fragment that truncation left at the very end."""

    index = text.rfind("<")
    if index == -1:
        return text
    candidate = re.sub(r"\s+", "", text[index:]).lower().strip("<>")
    if candidate in _DANGLING_MARKER_TAILS:
        return text[:index].rstrip()
    return text


def parse_marked_reply(text: str, *, max_parts: int = 3) -> tuple[list[str], bool]:
    """Split on the model's own ``<split/>`` markers.

    Returns ``(parts, used_markers)``. ``used_markers`` is False when the reply
    had no usable marker, so the caller can fall back to the punctuation
    heuristic. Markers at the very start/end, doubled markers, and an absurd
    number of markers are all tolerated instead of leaking into the message.
    """

    original = (text or "").strip()
    if not original or not _MARKER_RE.search(original):
        return ([original] if original else []), False

    limit = max(1, max_parts)
    raw = _MARKER_RE.split(original)
    if len(raw) > limit:
        # Keep the first bubbles and glue the rest back together, so text is
        # never dropped just because the model was chatty with markers.
        raw = raw[: limit - 1] + ["".join(raw[limit - 1 :])]

    parts = [part.strip() for part in raw if part.strip()]
    if len(parts) <= 1:
        # Marker-only reply, or a single marker somewhere harmless.
        cleaned = strip_split_markers(original)
        return ([cleaned] if cleaned else []), False
    return parts, True


def split_sentences(text: str) -> list[str]:
    """Split into sentences/clauses, whitespace-only pieces dropped."""

    parts = [match.group(0).strip() for match in _SENTENCE_RE.finditer(text or "")]
    return [part for part in parts if part]


def split_reply(
    text: str,
    *,
    min_chars: int = 160,
    max_parts: int = 3,
    probability: float = 0.8,
    rand: Rand | None = None,
) -> list[str]:
    """Fallback splitter for replies the model did not mark itself.

    Breaks at sentence boundaries only, but prefers the shape people actually
    type: a short opener standing alone ("我刚看了下。"), then the body in
    unevenly sized bubbles rather than two paragraphs of equal length.
    """

    draw = rand or random.random
    text = (text or "").strip()
    if not text:
        return []
    if len(text) < min_chars or max_parts < 2:
        return [text]

    sentences = split_sentences(text)
    if len(sentences) < 2:
        return [text]
    if draw() >= probability:
        return [text]

    groups: list[str] = []
    body = sentences
    if len(sentences[0]) <= _SHORT_OPENER_CHARS:
        groups.append(sentences[0])
        body = sentences[1:]

    body_slots = max(1, max_parts - len(groups))
    # Uneven targets: each call is a little different, and no two bubbles are
    # the same size, which is what a person's typing looks like.
    spread = 0.75 + 0.5 * draw()
    target = max(1.0, len("".join(body)) / body_slots) * spread

    body_groups: list[list[str]] = [[]]
    accumulated = 0
    for index, sentence in enumerate(body):
        body_groups[-1].append(sentence)
        accumulated += len(sentence)
        remaining_sentences = len(body) - index - 1
        remaining_parts = body_slots - len(body_groups)
        if (
            remaining_parts > 0
            and accumulated >= target
            and remaining_sentences >= remaining_parts
        ):
            body_groups.append([])
            accumulated = 0

    parts = groups + ["".join(group).strip() for group in body_groups if group]
    while len(parts) > 1 and len(parts[-1]) < _MIN_PART_CHARS:
        tail = parts.pop()
        parts[-1] = f"{parts[-1]}{tail}"
    return [part for part in parts if part] or [text]


def typing_delay_seconds(
    text: str,
    *,
    base_seconds: float,
    per_char_seconds: float,
    jitter_seconds: float,
    max_seconds: float,
    rand: Rand | None = None,
) -> float:
    """Delay before a send: reading time + typing time + random hesitation.

    ``base_seconds <= 0`` disables the simulation entirely (used by tests and
    by anyone who wants raw throughput).
    """

    if base_seconds <= 0:
        return 0.0
    draw = rand or random.random
    delay = base_seconds + per_char_seconds * len(text or "") + jitter_seconds * draw()
    if max_seconds > 0:
        delay = min(delay, max_seconds)
    return max(0.0, delay)


def gap_seconds(
    minimum: float, maximum: float, *, rand: Rand | None = None
) -> float:
    """Uniform pause inside ``[minimum, maximum]`` (order-insensitive)."""

    low, high = (minimum, maximum) if minimum <= maximum else (maximum, minimum)
    if high <= 0:
        return 0.0
    draw = rand or random.random
    return low + (high - low) * draw()


def jittered_seconds(
    base_seconds: float, ratio: float, *, rand: Rand | None = None
) -> float:
    """``base`` stretched/shrunk by up to ``ratio`` in either direction."""

    if base_seconds <= 0:
        return 0.0
    draw = rand or random.random
    spread = max(0.0, ratio)
    return max(0.0, base_seconds * (1.0 - spread + 2.0 * spread * draw()))
