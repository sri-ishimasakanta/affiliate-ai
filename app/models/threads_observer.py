"""外の Threads の観察 (T6.5B、読むだけ)。自分の公開の表とは別に持つ。

- 保存するのは **公開されていて画面に見えた情報だけ**。表示回数 (views) など外のアカウントの
  見えない値は列として持たない (推測で埋めさせないため)。
- 同じ投稿を何度観察しても、前の値を上書きしない (観察ごとに 1 行。T6.5E の速さの素材)。
- 見えなかった指標は NULL。0 と「見えなかった」を混ぜない。
- 実行 (run) ごとに、収集の版・selector の版・結果・止まった理由を残す。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

SOURCE_FOR_YOU = "for_you"
#: T6.5B.2: トピックの一覧 (検索の最初の画面の「おすすめのトピック」) から読んだ投稿。
#: このアカウント向けの一覧であって、世の中のトレンドではない (値も ``topic_for_you``)。
#: ``SOURCE_TRENDING_TOPIC`` は古い名前 (同じ値)。本番にこの値の行はまだ無い (2026-09-29)。
SOURCE_TOPIC_FOR_YOU = "topic_for_you"
SOURCE_TRENDING_TOPIC = SOURCE_TOPIC_FOR_YOU
SOURCE_SEARCH = "search"
SOURCE_CUSTOM_FEED = "custom_feed"
SOURCE_KNOWN_ACCOUNT = "known_account"
OBSERVER_SOURCES = (
    SOURCE_FOR_YOU, SOURCE_TRENDING_TOPIC, SOURCE_SEARCH, SOURCE_CUSTOM_FEED, SOURCE_KNOWN_ACCOUNT,
)  # fmt: skip

RUN_SUCCEEDED = "succeeded"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"
RUN_LOGIN_REQUIRED = "login_required"
RUN_DOM_UNRECOGNIZED = "dom_unrecognized"
#: T6.5B.1: 候補の勘定が合わない (黙って消えた投稿がある)。その実行の投稿は保存しない。
#: (``status`` の列は 24 文字なので ``candidate_accounting_mismatch`` ではなくこの名前。)
RUN_ACCOUNTING_MISMATCH = "accounting_mismatch"
RUN_STATUSES = (RUN_SUCCEEDED, RUN_PARTIAL, RUN_FAILED, RUN_LOGIN_REQUIRED, RUN_DOM_UNRECOGNIZED,
                RUN_ACCOUNTING_MISMATCH)  # fmt: skip


class ThreadsObserverRun(Base):
    """観察の 1 回。何を・どこまで・どの版で集め、なぜ止まったか。"""

    __tablename__ = "threads_observer_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=RUN_FAILED)
    source_types_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    item_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    items_collected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    collector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    selector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 画面の証拠 (スクリーンショットの相対パスなど)。任意。
    artifacts_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ThreadsExternalPost(Base):
    """外の投稿 1 件 (安定した鍵で 1 行)。指標は観察の表に積む。"""

    __tablename__ = "threads_external_posts"
    __table_args__ = (
        UniqueConstraint("external_post_key", name="uq_threads_external_post_key"),
        Index("ix_threads_external_posts_author", "author_handle"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 例: ``threads:<post code>`` (permalink から)。
    external_post_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 公開されている表示名の handle (見えたときだけ)。
    author_handle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permalink: Mapped[str | None] = mapped_column(String(512), nullable=True)
    post_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    has_link: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    features_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    feature_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ThreadsExternalObservation(Base):
    """外の投稿を 1 回観察した値 (上書きしない)。見えなかった指標は NULL。"""

    __tablename__ = "threads_external_observations"
    __table_args__ = (
        Index("ix_threads_external_observations_post", "post_id", "observed_at"),
        Index("ix_threads_external_observations_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("threads_external_posts.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("threads_observer_runs.id", ondelete="RESTRICT"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_query: Mapped[str | None] = mapped_column(String(256), nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replies: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reposts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quotes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extraction_status: Mapped[str] = mapped_column(String(24), nullable=False)
    collector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    selector_version: Mapped[str] = mapped_column(String(64), nullable=False)


class ThreadsTrendingTopic(Base):
    """観察したトピックの一覧の名前 (1 つの信号にすぎない。良い・悪いは決めない)。

    2026-09-28 の確認: Web で見える一覧は検索の最初の画面の「おすすめのトピック」
    (``source_type = topic_for_you``)。Threads はこれを「トレンド」と表示していない。
    表の名前は元のまま (``threads_trending_topics``)。一覧の順・リンクは実行の記録に残る。
    """

    __tablename__ = "threads_trending_topics"
    __table_args__ = (
        UniqueConstraint("topic_name", "source_type", name="uq_threads_trending_topic"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sample_post_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = [
    "OBSERVER_SOURCES",
    "RUN_DOM_UNRECOGNIZED",
    "RUN_FAILED",
    "RUN_LOGIN_REQUIRED",
    "RUN_PARTIAL",
    "RUN_STATUSES",
    "RUN_SUCCEEDED",
    "SOURCE_CUSTOM_FEED",
    "SOURCE_FOR_YOU",
    "SOURCE_KNOWN_ACCOUNT",
    "SOURCE_SEARCH",
    "SOURCE_TRENDING_TOPIC",
    "ThreadsExternalObservation",
    "ThreadsExternalPost",
    "ThreadsObserverRun",
    "ThreadsTrendingTopic",
]
