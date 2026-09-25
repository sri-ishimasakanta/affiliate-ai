"""ThreadsPublication / ThreadsPublicationAttempt (T3)。

承認された投稿案を **1 度だけ** 外へ出すための記録。

二重投稿を構造的に防ぐ:

- ``threads_publications`` は提案 1 件につき 1 行 (``UNIQUE(proposal_id)``)。
  2 度目の公開はそもそも行を作れない。
- ``threads_media_id`` が分かっている行は、二度と公開しない。
- 応答を取りこぼした場合は ``uncertain`` にして止める。**盲目的に再送しない。**
  照合 (コンテナ状態の照会) を経てからでないと次へ進めない。
- 試行そのものは ``threads_publication_attempts`` に append-only で積む。
  失敗も uncertain も履歴として残り、上書きしない。

access token は **どこにも保存しない**。失敗も分類と sanitized な要約だけを残す。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# -- states --------------------------------------------------------------------
PUB_PLANNED = "planned"
PUB_CREATING = "creating"
PUB_CONTAINER_CREATED = "container_created"
PUB_PUBLISHING = "publishing"
PUB_PUBLISHED = "published"
PUB_FAILED = "failed"
#: 書いたかどうか分からない。照合するまで次へ進めない。
PUB_UNCERTAIN = "uncertain"

PUB_STATES = frozenset(
    {
        PUB_PLANNED,
        PUB_CREATING,
        PUB_CONTAINER_CREATED,
        PUB_PUBLISHING,
        PUB_PUBLISHED,
        PUB_FAILED,
        PUB_UNCERTAIN,
    }
)
#: 外へ出した可能性があり、照合なしに再試行してはいけない状態。
PUB_IN_FLIGHT_STATES = frozenset(
    {PUB_CREATING, PUB_CONTAINER_CREATED, PUB_PUBLISHING, PUB_UNCERTAIN}
)
#: これ以上進まない状態。
PUB_TERMINAL_STATES = frozenset({PUB_PUBLISHED})
#: 照合のうえで再試行してよい状態。
PUB_RETRYABLE_STATES = frozenset({PUB_PLANNED, PUB_FAILED})

# -- T4.3: 公開を始めた主体 ------------------------------------------------------
PUB_TRIGGER_MANUAL = "manual"
PUB_TRIGGER_AUTOMATIC = "automatic"
PUB_TRIGGERS = (PUB_TRIGGER_MANUAL, PUB_TRIGGER_AUTOMATIC)


class ThreadsPublication(Base):
    __tablename__ = "threads_publications"

    __table_args__ = (
        # 提案 1 件につき 1 行。二重投稿を DB の形で防ぐ。
        UniqueConstraint("proposal_id", name="uq_threads_publications_proposal"),
        UniqueConstraint("threads_media_id", name="uq_threads_publications_media"),
        Index("ix_threads_publications_status", "status"),
        CheckConstraint(
            "status IN ('planned', 'creating', 'container_created', 'publishing', "
            "'published', 'failed', 'uncertain')",
            name="threads_publications_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("threads_post_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    #: 承認された提案の identity。ここが一致しない内容は出さない。
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False
    )
    angle: Mapped[str] = mapped_column(String(32), nullable=False)

    #: 実際に送った (送ろうとした) 文字列そのもの。提案から作り直さない。
    exact_published_text: Mapped[str] = mapped_column(Text, nullable=False)
    destination_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    threads_creation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    threads_media_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permalink: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: Threads 側が返した公開時刻 (こちらで推定しない)。
    remote_timestamp: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default=PUB_PLANNED)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- T4.3: 誰が公開を始めたか / 間隔の上書き -----------------------------------
    #: ``manual`` (人が publish_threads_post.py --execute) か ``automatic`` (常駐 worker)。
    trigger: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PUB_TRIGGER_MANUAL, server_default=PUB_TRIGGER_MANUAL
    )
    #: 人が 120 分の間隔を明示的に上書きした理由。NULL = 上書きなし。
    #: 自動の公開は上書きできない (理由があるのは常に人の操作)。
    gap_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    gap_override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 読み戻した本文が承認内容と違ったときに立てる (提案は書き換えない)。
    reconciliation_required: Mapped[bool] = mapped_column(default=False, nullable=False)
    reconciliation_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    publish_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    attempts: Mapped[list[ThreadsPublicationAttempt]] = relationship(  # noqa: F821
        back_populates="publication", order_by="ThreadsPublicationAttempt.id"
    )


class ThreadsPublicationAttempt(Base):
    """公開の試行 1 回分 (append-only)。成功も失敗も上書きしない。"""

    __tablename__ = "threads_publication_attempts"

    __table_args__ = (Index("ix_threads_publication_attempts_pub", "threads_publication_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    threads_publication_id: Mapped[int] = mapped_column(
        ForeignKey("threads_publications.id", ondelete="RESTRICT"), nullable=False
    )
    #: create_container / publish_container / reconcile / readback。
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    #: secret を含まない補足 (返ってきた id や状態など)。
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    publication: Mapped[ThreadsPublication] = relationship(back_populates="attempts")  # noqa: F821
