"""Threads のトピック (T6.3.2)。投稿の種類と **公開の日 (JST)** で決まる、決定的な方針。

- 記事から作る通常の投稿 (``article``): いつも ``THREADS_NORMAL_TOPIC_TAG`` ("AI Threads")
- アカウントを育てる投稿 (``account_growth``、T6.3.3): 公開の JST の日付で決まる
  (T6.3.3c、人が決めた):

  - 2026-10-04 (JST) まで (その日を含む): ``THREADS_GROWTH_TOPIC_TAG`` ("インサイト祭り")
  - 2026-10-05 00:00 (JST) から: **トピックなし** (方針として送らない。断られたから外すの
    ではない)。別のトピックを自動では選ばない (人が次に決めるまで、なしのまま)

  公開のときに付ける (前へ進むだけ: 公開済みの投稿・提案・記録は変えない)

トピックは Threads API の ``topic_tag`` (コンテナ作成の引数) で渡す **メタデータ** であり、
本文には足さない。本文・提案の hash・500 字の数え方は変わらない。Luna・きっかけ・切り口・
記事のカテゴリ・link_mode では変わらない。

公式 (2026-09-28 に確認): ``POST /{threads-user-id}/threads`` の任意の文字列 ``topic_tag``。
1〜50 字。ピリオド (.) とアンパサンド (&) は使えない。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

#: 通常の投稿 (記事から作る投稿) に必ず付けるトピック。人が決めた固定の値 (秘密ではない)。
THREADS_NORMAL_TOPIC_TAG = "AI Threads"
#: T6.3.3b: Growth Post のトピック (人が決めた値)。T6.3.3c から、公開の日が
#: ``GROWTH_TOPIC_LAST_DAY`` までのときだけ付ける。
THREADS_GROWTH_TOPIC_TAG = "インサイト祭り"

CONTENT_KIND_ARTICLE = "article"
CONTENT_KIND_ACCOUNT_GROWTH = "account_growth"
#: 知っている種類。ここに無い種類は公開しない (未知の種類は通さない)。
CONTENT_KINDS = (CONTENT_KIND_ARTICLE, CONTENT_KIND_ACCOUNT_GROWTH)

#: T6.3.3c: Growth のトピックを決める時計 (UTC の日付では決めない)。
GROWTH_TOPIC_TIMEZONE = ZoneInfo("Asia/Tokyo")
#: この JST の日 (を含む) まで Growth Post に "インサイト祭り" を付ける。翌日 00:00 からなし。
GROWTH_TOPIC_LAST_DAY = date(2026, 10, 4)
#: なしになった後に、別のトピックを自動で選ばない (人が決める)。
GROWTH_TOPIC_AUTO_REPLACEMENT = False

#: 公開の記録に残す、トピックの決め方 (方針でなし ≠ 断られて外した)。
TOPIC_DECISION_TAGGED = "policy_topic"
TOPIC_DECISION_NO_TOPIC = "policy_no_topic"

#: 提案に種類を明示するときの場所 (``learning_guidance_json`` の鍵。migration なし)。
CONTENT_KIND_KEY = "content_kind"

TOPIC_TAG_MAX_LENGTH = 50
TOPIC_TAG_FORBIDDEN = (".", "&")

#: 表示用 (承認のメール)。
NO_TOPIC_LABEL = "なし"


class TopicPolicyError(ValueError):
    """種類またはトピックが方針に合わない。公開しない (fail closed)。"""


def explicit_content_kind(proposal) -> str | None:
    """提案に明示された種類 (無ければ ``None``)。本文からは推測しない。"""

    guidance = getattr(proposal, "learning_guidance_json", None)
    return guidance.get(CONTENT_KIND_KEY) if isinstance(guidance, Mapping) else None


def content_kind(proposal) -> str:
    """提案の種類。**記事の有無と明示の印** だけで決める (T6.3.3)。

    - 印なし + 記事あり → ``article`` (T6.3.3 より前の提案はすべてこれ)
    - ``account_growth`` + 記事なし → ``account_growth`` (印は必須)
    - 印なし + 記事なし・``account_growth`` + 記事あり・``article`` + 記事なし・未知の印
      → 例外 (公開しない)
    """

    explicit = explicit_content_kind(proposal)
    source = getattr(proposal, "source_article_id", None)
    if explicit is not None and explicit not in CONTENT_KINDS:
        raise TopicPolicyError(f"unknown Threads content kind {explicit!r}")
    if explicit == CONTENT_KIND_ACCOUNT_GROWTH:
        if source is not None:
            raise TopicPolicyError("an account_growth post must not have a source article")
        return CONTENT_KIND_ACCOUNT_GROWTH
    if source is None:
        raise TopicPolicyError(
            "a proposal without a source article must declare content_kind=account_growth"
        )
    return CONTENT_KIND_ARTICLE


def is_account_growth(proposal) -> bool:
    """アカウントを育てる投稿か (例外を出さない判定。印と記事なしの両方が要る)。"""

    return (
        explicit_content_kind(proposal) == CONTENT_KIND_ACCOUNT_GROWTH
        and getattr(proposal, "source_article_id", None) is None
    )


def growth_topic_on(day: date) -> str | None:
    """その JST の日に公開する Growth Post のトピック (10/4 まで "インサイト祭り"、以後なし)。"""

    return THREADS_GROWTH_TOPIC_TAG if day <= GROWTH_TOPIC_LAST_DAY else None


def topic_tag_for(kind: str, *, at: datetime | None = None) -> str | None:
    """種類 (+ 公開の時刻) → トピック (``None`` はトピックなし)。未知の種類・不正なトピックは例外。

    ``account_growth`` は公開の時刻 ``at`` (時差つき) が要る (JST の日付で決める)。
    """

    if kind not in CONTENT_KINDS:
        raise TopicPolicyError(f"unknown Threads content kind {kind!r}")
    if kind == CONTENT_KIND_ARTICLE:
        tag = THREADS_NORMAL_TOPIC_TAG
    else:
        if at is None or at.tzinfo is None:
            raise TopicPolicyError("the growth topic needs a timezone-aware publication time")
        tag = growth_topic_on(at.astimezone(GROWTH_TOPIC_TIMEZONE).date())
    if tag is not None:
        validate_topic_tag(tag)
    return tag


def topic_decision(tag: str | None) -> str:
    return TOPIC_DECISION_TAGGED if tag is not None else TOPIC_DECISION_NO_TOPIC


def proposal_publication_time(proposal) -> datetime | None:
    """承認の画面に出すトピックを決める時刻: Growth Post は公開してよい日のはじめ
    (``not_before``。無ければ ``growth.date_jst`` の 07:00 JST)。Growth Post はその JST の日の
    中でだけ公開されるので、公開のときと同じになる。"""

    moment = getattr(proposal, "not_before", None)
    if moment is not None:
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    guidance = getattr(proposal, "learning_guidance_json", None)
    growth = guidance.get("growth") if isinstance(guidance, Mapping) else None
    day = growth.get("date_jst") if isinstance(growth, Mapping) else None
    try:
        return datetime.combine(date.fromisoformat(str(day)), time(7, 0),
                                tzinfo=GROWTH_TOPIC_TIMEZONE)  # fmt: skip
    except ValueError:
        return None


def topic_tag_for_proposal(proposal, *, at: datetime | None = None) -> str | None:
    """提案 → トピック。``at`` が無ければ、Growth Post は ``proposal_publication_time``。"""

    kind = content_kind(proposal)
    if kind == CONTENT_KIND_ACCOUNT_GROWTH and at is None:
        at = proposal_publication_time(proposal)
    return topic_tag_for(kind, at=at)


def validate_topic_tag(tag: str) -> str:
    """公式の制約 (1〜50 字、``.`` と ``&`` なし) を満たすか。満たさなければ例外。"""

    if not isinstance(tag, str) or not (1 <= len(tag) <= TOPIC_TAG_MAX_LENGTH):
        raise TopicPolicyError(f"the topic tag must be 1-{TOPIC_TAG_MAX_LENGTH} characters")
    bad = [ch for ch in TOPIC_TAG_FORBIDDEN if ch in tag]
    if bad:
        raise TopicPolicyError(f"the topic tag must not contain {' '.join(bad)}")
    return tag


def topic_label(tag: str | None) -> str:
    return tag if tag else NO_TOPIC_LABEL


__all__ = [
    "CONTENT_KINDS",
    "CONTENT_KIND_ACCOUNT_GROWTH",
    "CONTENT_KIND_ARTICLE",
    "CONTENT_KIND_KEY",
    "GROWTH_TOPIC_AUTO_REPLACEMENT",
    "GROWTH_TOPIC_LAST_DAY",
    "GROWTH_TOPIC_TIMEZONE",
    "NO_TOPIC_LABEL",
    "THREADS_GROWTH_TOPIC_TAG",
    "THREADS_NORMAL_TOPIC_TAG",
    "TOPIC_DECISION_NO_TOPIC",
    "TOPIC_DECISION_TAGGED",
    "TopicPolicyError",
    "content_kind",
    "explicit_content_kind",
    "growth_topic_on",
    "is_account_growth",
    "proposal_publication_time",
    "topic_decision",
    "topic_label",
    "topic_tag_for",
    "topic_tag_for_proposal",
    "validate_topic_tag",
]
