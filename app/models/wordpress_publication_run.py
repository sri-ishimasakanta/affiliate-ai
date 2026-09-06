"""WordPressPublicationRun — 既存 WordPress draft を publish する **auditable な実行記録**
(append-only)。

「どの Article / どの succeeded draft run / どの WordPress post を、凍結した設置先へ、
どの exact な publish request で publish する/した か」を後日 join なしで再現・監査できる
ようにする。

- ``updated_at`` を持たない。retry は新しい run を append する。
- prepare 後、identity フィールド群は immutable。実行フィールドのみ lifecycle に沿って
  populate される。
- ``renderer_version`` / ``rendered_content_hash`` は持たない。WordPress は保存時に
  inline style を正当に sanitize 済みのため、publication の drift 検証は
  ``wordpress_raw_content_hash`` (WordPress が実際に保存している content.raw の SHA-256)
  と local の ``canonical_body_hash`` / ``canonical_meta_hash`` で行う。
- Article 削除は ORM cascade (Article 側) 経由でのみ。source draft run の単独削除は
  FK ``ON DELETE RESTRICT`` で防ぐ (defense-in-depth)。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
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

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.wordpress_draft_run import WordPressDraftRun

# status lifecycle (§5)。
WP_PUBRUN_PREPARED = "prepared"
WP_PUBRUN_RUNNING = "running"
WP_PUBRUN_SUCCEEDED = "succeeded"
WP_PUBRUN_FAILED = "failed"
WP_PUBRUN_CANCELLED = "cancelled"
WP_PUBRUN_STATUSES = frozenset(
    {
        WP_PUBRUN_PREPARED,
        WP_PUBRUN_RUNNING,
        WP_PUBRUN_SUCCEEDED,
        WP_PUBRUN_FAILED,
        WP_PUBRUN_CANCELLED,
    }
)
WP_PUBRUN_TERMINAL_STATUSES = frozenset(
    {WP_PUBRUN_SUCCEEDED, WP_PUBRUN_FAILED, WP_PUBRUN_CANCELLED}
)
WP_PUBRUN_ACTIVE_STATUSES = frozenset({WP_PUBRUN_PREPARED, WP_PUBRUN_RUNNING})

# 許可された status 遷移 (§5)。汎用ワークフローエンジンは作らない。
WP_PUBRUN_TRANSITIONS: dict[str, frozenset[str]] = {
    WP_PUBRUN_PREPARED: frozenset({WP_PUBRUN_RUNNING, WP_PUBRUN_CANCELLED}),
    WP_PUBRUN_RUNNING: frozenset({WP_PUBRUN_SUCCEEDED, WP_PUBRUN_FAILED}),
    WP_PUBRUN_SUCCEEDED: frozenset(),
    WP_PUBRUN_FAILED: frozenset(),
    WP_PUBRUN_CANCELLED: frozenset(),
}


def wp_publication_run_transition_allowed(current: str, target: str) -> bool:
    """``current`` から ``target`` への status 遷移が許可されているか。

    同一 status への変更は許可しない (append-only lifecycle)。
    """

    return target in WP_PUBRUN_TRANSITIONS.get(current, frozenset())


# prepare 後 immutable な identity フィールド。
FROZEN_PREPARED_FIELDS = (
    "article_id",
    "source_wordpress_draft_run_id",
    "target_base_url",
    "wordpress_post_id",
    "method",
    "endpoint_path",
    "publish_payload_json",
    "publish_payload_hash",
    "publication_request_identity_hash",
    "target_publication_request_identity_hash",
    "canonical_body_hash",
    "canonical_meta_hash",
    "wordpress_raw_content_hash",
    "expected_pre_publish_status",
    "idempotency_key",
)


class WordPressPublicationRun(Base):
    __tablename__ = "wordpress_publication_runs"

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_wordpress_publication_runs_idempotency_key"
        ),
        Index(
            "ix_wordpress_publication_runs_article_created_id",
            "article_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_wordpress_draft_run_id: Mapped[int] = mapped_column(
        ForeignKey("wordpress_draft_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # 凍結した設置先 (canonical。credential は含まない)。
    target_base_url: Mapped[str] = mapped_column(String(1024), nullable=False)

    # 凍結した publish 対象 WordPress post id (draft run と型を揃えて String)。
    wordpress_post_id: Mapped[str] = mapped_column(String(64), nullable=False)

    method: Mapped[str] = mapped_column(String(10), nullable=False)
    endpoint_path: Mapped[str] = mapped_column(String(255), nullable=False)

    # 送信予定の exact な publish JSON body (canonical serialization そのもの)。
    publish_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    publish_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_request_identity_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    target_publication_request_identity_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )

    canonical_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_meta_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # WordPress が実際に保存している content.raw の SHA-256 (post-sanitation identity)。
    wordpress_raw_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # publish 直前に期待する WordPress post status。V1 は常に "draft"。
    expected_pre_publish_status: Mapped[str] = mapped_column(String(40), nullable=False)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- 実行結果 (lifecycle に沿って populate。credential は入れない) ---
    wordpress_post_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    wordpress_post_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # published_at をどこから採ったか ("wordpress_date_gmt" / "local_confirmed")。
    published_at_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # updated_at は持たない (append-only)。

    article: Mapped[Article] = relationship(
        back_populates="wordpress_publication_runs"
    )
    source_wordpress_draft_run: Mapped[WordPressDraftRun] = relationship(
        foreign_keys=[source_wordpress_draft_run_id]
    )
