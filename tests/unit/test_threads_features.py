"""T6.5A: 本文からの決定的な特徴 (自分の投稿と外の投稿で同じ抽出を使う)。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.social.threads.features import (
    CTA_CLASSES,
    FEATURE_VERSION,
    STRUCTURE_CLASSES,
    char_bucket,
    cta_class,
    emoji_count,
    extract,
    structure_class,
)


def test_extraction_is_deterministic_and_versioned() -> None:
    text = "AIで作業が3割減った話。\n\nみなさんはどう使っていますか？"
    first = extract(text, topic="AI Threads")
    assert first == extract(text, topic="AI Threads")
    assert first.feature_version == FEATURE_VERSION
    assert first.source == "rule"
    assert first.as_dict()["topic"] == "AI Threads"


def test_counts_and_flags() -> None:
    text = "月額1,980円と980円を比べた。\n\n結論は2つ🙂\nhttps://bizfluxlab.com/x/"
    features = extract(text)
    assert features.has_url is True
    assert features.price_count == 2
    assert features.has_number is True
    assert features.numeric_facts_count >= 3
    assert features.emoji_count == 1
    assert features.paragraph_count == 2
    # 長さは URL を除いた本文で測る (文字数は生の長さ)。
    assert features.body_length < features.character_count


def test_time_is_jst_and_unknown_time_stays_unknown() -> None:
    posted = datetime(2026, 9, 27, 23, 30, tzinfo=UTC)  # JST 9/28 (月) 8:30
    features = extract("本文", posted_at=posted)
    assert (features.weekday, features.hour) == (0, 8)
    assert extract("本文").weekday is None
    assert extract("本文").hour is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("気軽にフォローしてください。", "follow_connect"),
        ("コメントで教えてください。", "reply_request"),
        ("あなたはどう思いますか？", "opinion_request"),
        ("これって本当？", "question"),
        ("今日は晴れ。", "none"),
    ],
)
def test_cta_classes(text: str, expected: str) -> None:
    assert cta_class(text) == expected
    assert expected in CTA_CLASSES


def test_structure_classes_are_from_the_closed_list() -> None:
    assert structure_class("・一つ目\n・二つ目\n・三つ目", body_length=20) == "checklist_like"
    assert structure_class("短い一言。", body_length=5) == "short_single_point"
    long_text = "説明の文章。" * 40
    assert structure_class(long_text, body_length=len(long_text)) in STRUCTURE_CLASSES


@pytest.mark.parametrize(
    ("length", "bucket"),
    [(0, "0-119"), (119, "0-119"), (120, "120-199"), (279, "200-279"), (360, "280-360"),
     (361, "361+")],
)  # fmt: skip
def test_char_buckets(length: int, bucket: str) -> None:
    assert char_bucket(length) == bucket


def test_emoji_count_ignores_plain_japanese() -> None:
    assert emoji_count("日本語と記号！？") == 0
    assert emoji_count("🎉🎉") == 2
