"""Threads のトピック (T6.3.2)。投稿の種類だけで決まる、決定的な方針。

- 記事から作る通常の投稿 (``article``): いつも ``THREADS_NORMAL_TOPIC_TAG`` ("AI Threads")
- アカウントを育てる投稿 (``account_growth``、T6.3.3): T6.3.3b からいつも
  ``THREADS_GROWTH_TOPIC_TAG`` ("インサイト祭り")。公開のときに付ける (前へ進むだけ: 公開済みの
  投稿は変えない)

トピックは Threads API の ``topic_tag`` (コンテナ作成の引数) で渡す **メタデータ** であり、
本文には足さない。本文・提案の hash・500 字の数え方は変わらない。Luna・きっかけ・切り口・
記事のカテゴリ・link_mode では変わらない。

公式 (2026-09-28 に確認): ``POST /{threads-user-id}/threads`` の任意の文字列 ``topic_tag``。
1〜50 字。ピリオド (.) とアンパサンド (&) は使えない。
"""

from __future__ import annotations

from collections.abc import Mapping

#: 通常の投稿 (記事から作る投稿) に必ず付けるトピック。人が決めた固定の値 (秘密ではない)。
THREADS_NORMAL_TOPIC_TAG = "AI Threads"
#: T6.3.3b: Growth Post に必ず付けるトピック (人が決めた固定の値)。
THREADS_GROWTH_TOPIC_TAG = "インサイト祭り"

CONTENT_KIND_ARTICLE = "article"
CONTENT_KIND_ACCOUNT_GROWTH = "account_growth"

#: 投稿の種類 → トピック。種類をここに足さない限り、公開は止まる (未知の種類は通さない)。
TOPIC_BY_CONTENT_KIND: Mapping[str, str | None] = {
    CONTENT_KIND_ARTICLE: THREADS_NORMAL_TOPIC_TAG,
    CONTENT_KIND_ACCOUNT_GROWTH: THREADS_GROWTH_TOPIC_TAG,
}
CONTENT_KINDS = tuple(TOPIC_BY_CONTENT_KIND)

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
    if explicit is not None and explicit not in TOPIC_BY_CONTENT_KIND:
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


def topic_tag_for(kind: str) -> str | None:
    """種類 → トピック (``None`` はトピックなし)。未知の種類・不正なトピックは例外。"""

    if kind not in TOPIC_BY_CONTENT_KIND:
        raise TopicPolicyError(f"unknown Threads content kind {kind!r}")
    tag = TOPIC_BY_CONTENT_KIND[kind]
    if tag is not None:
        validate_topic_tag(tag)
    return tag


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
    "NO_TOPIC_LABEL",
    "THREADS_GROWTH_TOPIC_TAG",
    "THREADS_NORMAL_TOPIC_TAG",
    "TOPIC_BY_CONTENT_KIND",
    "TopicPolicyError",
    "content_kind",
    "explicit_content_kind",
    "is_account_growth",
    "topic_label",
    "topic_tag_for",
    "validate_topic_tag",
]
