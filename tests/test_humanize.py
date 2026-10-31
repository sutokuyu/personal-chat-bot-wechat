from __future__ import annotations

import pytest

from app.humanize import (
    drop_dangling_marker,
    gap_seconds,
    jittered_seconds,
    parse_marked_reply,
    split_reply,
    split_sentences,
    strip_split_markers,
    typing_delay_seconds,
)

SENTENCES = [
    "今天下午三点在楼下集合，别迟到，地铁口那个便利店门口等。",
    "记得带身份证和水，门口可能要登记，登记完再上楼。",
    "到了先找小王拿牌子，牌子上写着我们公司的名字。",
    "路上别走散，手机保持开机，有事群里说一声。",
]
TEXT = "".join(SENTENCES)


def test_split_sentences_keeps_delimiters_and_drops_blank_pieces():
    assert split_sentences("甲。乙！丙？\n\n丁") == ["甲。", "乙！", "丙？", "丁"]


def test_short_reply_is_left_alone():
    assert split_reply("好呀我去看看", min_chars=160) == ["好呀我去看看"]


def test_long_reply_is_split_without_losing_text():
    parts = split_reply(TEXT, min_chars=80, max_parts=3, probability=1.0)
    assert 2 <= len(parts) <= 3
    assert "".join(parts) == TEXT
    assert all(part.endswith(("。", "！", "？")) for part in parts)


def test_short_opening_sentence_becomes_its_own_bubble():
    parts = split_reply(TEXT, min_chars=80, max_parts=3, probability=1.0)
    assert parts[0] == SENTENCES[0]


def test_probability_zero_keeps_one_bubble():
    assert split_reply(TEXT, min_chars=80, probability=0.0) == [TEXT]


def test_text_without_punctuation_is_never_split():
    text = "a" * 300
    assert split_reply(text, min_chars=100, probability=1.0) == [text]


def test_typing_delay_is_proportional_and_capped():
    low = typing_delay_seconds(
        "x" * 10,
        base_seconds=2.0,
        per_char_seconds=0.1,
        jitter_seconds=1.0,
        max_seconds=25.0,
        rand=lambda: 0.0,
    )
    high = typing_delay_seconds(
        "x" * 10,
        base_seconds=2.0,
        per_char_seconds=0.1,
        jitter_seconds=1.0,
        max_seconds=25.0,
        rand=lambda: 1.0,
    )
    assert low == 3.0
    assert high == 4.0

    capped = typing_delay_seconds(
        "x" * 10_000,
        base_seconds=1.0,
        per_char_seconds=0.1,
        jitter_seconds=0.0,
        max_seconds=25.0,
    )
    assert capped == 25.0


def test_zero_base_disables_typing_delay():
    assert (
        typing_delay_seconds(
            "x" * 500,
            base_seconds=0.0,
            per_char_seconds=0.5,
            jitter_seconds=5.0,
            max_seconds=25.0,
        )
        == 0.0
    )


def test_gap_and_jitter_stay_inside_their_bounds():
    assert gap_seconds(1.0, 3.0, rand=lambda: 0.0) == 1.0
    assert gap_seconds(1.0, 3.0, rand=lambda: 1.0) == 3.0
    assert gap_seconds(3.0, 1.0, rand=lambda: 0.5) == 2.0
    assert gap_seconds(0.0, 0.0) == 0.0

    assert jittered_seconds(10.0, 0.3, rand=lambda: 0.0) == 7.0
    assert jittered_seconds(10.0, 0.3, rand=lambda: 1.0) == pytest.approx(13.0)
    assert jittered_seconds(0.0, 0.5) == 0.0


def test_marked_reply_uses_the_model_break_points():
    parts, marked = parse_marked_reply("等我一下。<split/>我去看看那个文件。")
    assert marked is True
    assert parts == ["等我一下。", "我去看看那个文件。"]


def test_unmarked_reply_reports_no_markers():
    parts, marked = parse_marked_reply("一条普通回复。")
    assert marked is False
    assert parts == ["一条普通回复。"]


def test_marker_variants_and_stray_positions_are_tolerated():
    parts, marked = parse_marked_reply("<split/>先说一下<SPLIT />然后再说</split>")
    assert marked is True
    assert parts == ["先说一下", "然后再说"]


def test_extra_markers_are_merged_instead_of_dropping_text():
    text = "一。<split/>二。<split/>三。<split/>四。"
    parts, marked = parse_marked_reply(text, max_parts=3)
    assert marked is True
    assert len(parts) == 3
    assert "".join(parts) == "一。二。三。四。"


def test_marker_only_reply_falls_back_to_cleaned_text():
    parts, marked = parse_marked_reply("<split/>")
    assert marked is False
    assert parts == []


def test_strip_split_markers_removes_every_variant():
    assert strip_split_markers("甲<split/>乙<split />丙") == "甲乙丙"


def test_dangling_marker_is_dropped_after_truncation():
    assert drop_dangling_marker("说完了。<spl") == "说完了。"
    assert drop_dangling_marker("说完了。<split/") == "说完了。"
    assert drop_dangling_marker("价格是<3") == "价格是<3"
    assert drop_dangling_marker("没有标记") == "没有标记"
