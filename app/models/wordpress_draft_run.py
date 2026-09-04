"""WordPressDraftRun — 初回 WordPress draft 作成の **auditable な実行記録** (append-only)。

「どの Article / どの Human 採用 / 承認済み request / 凍結した設置先へ、何を送る/送った
か」を後日 join なしで再現・監査できるようにする。

- ``updated_at`` を持たない。retry は新しい行を append する。
- prepare 後、request identity フィールド群は immutable。実行フィールドのみ lifecycle
  に沿って populate される。
- Article / Promotion 削除は ORM cascade (Article 側) 経由でのみ。単独削除は FK
  ``ON DELETE RESTRICT`` で防ぐ (defense-in-depth)。
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
    from app.models.article_draft_promotion import ArticleDraftPromotion

# status lifecycle (§7)。
WP_RUN_PREPARED = "prepared"
WP_RUN_RUNNING = "running"
WP_RUN_SUCCEEDED = "succeeded"
WP_RUN_FAILED = "failed"
WP_RUN_CANCELLED = "cancelled"
WP_RUN_STATUSES = frozenset(
    {
        WP_RUN_PREPARED,
        WP_RUN_RUNNING,
        WP_RUN_SUCCEEDED,
        WP_RUN_FAILED,
        WP_RUN_CANCELLED,
    }
)
WP_RUN_TERMINAL_STATUSES = frozenset(
    {WP_RUN_SUCCEEDED, WP_RUN_FAILED, WP_RUN_CANCELLED}
)
WP_RUN_ACTIVE_STATUSES = frozenset({WP_RUN_PREPARED, WP_RUN_RUNNING})

# 許可された status 遷移 (§7)。汎用ワークフローエンジンは作らない。
WP_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    WP_RUN_PREPARED: frozenset({WP_RUN_RUNNING, WP_RUN_CANCELLED}),
    WP_RUN_RUNNING: frozenset({WP_RUN_SUCCEEDED, WP_RUN_FAILED}),
    WP_RUN_SUCCEEDED: frozenset(),
    WP_RUN_FAILED: frozenset(),
    WP_RUN_CANCELLED: frozenset(),
}

def wp_run_transition_allowed(current: str, target: str) -> bool:
    """``current`` から ``target`` への status 遷移が許可されているか。

    同一 status への変更は許可しない (WordPressDraftRun は append-only lifecycle)。
    """

    return target in WP_RUN_TRANSITIONS.get(current, frozenset())


# prepare 後 immutable な request identity フィールド (§8)。
FROZEN_PREPARED_FIELDS = (
    "article_id",
    "source_promotion_id",
    "target_base_url",
    "method",
    "endpoint_path",
    "payload_json",
    "payload_hash",
    "request_identity_hash",
    "target_request_identity_hash",
    "canonical_body_hash",
    "canonical_meta_hash",
    "renderer_version",
    "rendered_content_hash",
    "idempotency_key",
)


class WordPressDraftRun(Base):
    __tablename__ = "wordpress_draft_runs"

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_wordpress_draft_runs_idempotency_key"
        ),
        Index(
            "ix_wordpress_draft_runs_article_created_id",
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
    source_promotion_id: Mapped[int] = mapped_column(
        ForeignKey("article_draft_promotions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # 凍結した設置先 (canonical。credential は含まない)。
    target_base_url: Mapped[str] = mapped_column(String(1024), nullable=False)

    method: Mapped[str] = mapped_column(String(10), nullable=False)
    endpoint_path: Mapped[str] = mapped_column(String(255), nullable=False)

    # 送信予定の exact な logical JSON body (canonical serialization そのもの)。
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_request_identity_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )

    canonical_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_meta_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(40), nullable=False)
    rendered_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- 実行結果 (lifecycle に沿って populate。credential は入れない) ---
    wordpress_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wordpress_post_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    wordpress_post_url: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    article: Mapped[Article] = relationship(back_populates="wordpress_draft_runs")
    source_promotion: Mapped[ArticleDraftPromotion] = relationship(
        back_populates="wordpress_draft_runs"
    )
