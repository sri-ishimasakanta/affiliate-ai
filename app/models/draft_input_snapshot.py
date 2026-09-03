"""DraftInputSnapshot — LLM draft 生成前の **入力を凍結した immutable artifact**。

「その draft が何を入力に作られたか」(どの Plan / どの Fact / claim 禁止項目 /
比較 7 tool / primary) を後日 join なしで再現・監査できるようにする。

- ``updated_at`` を持たない。内容が変われば **新しい行を append** する
  (Source / ArticleFact と同じ immutable history semantics)。
- latest = ``(article_id)`` ごとに ``frozen_at DESC, id DESC``。``is_current`` flag は持たない。
- Article 削除時のみ FK/ORM cascade で削除。
- 生成の実行情報 (LLM model / prompt / body / token usage) は持たない。それは
  将来の ``DraftGenerationRun`` の責務 (What we knew/decided と How it ran を分離)。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.draft_generation_run import DraftGenerationRun

# payload / builder / plan-origin の表記揺れを防ぐための定数。
SNAPSHOT_VERSION = "draft_input_v1"
BUILDER_VERSION = "draft_input_builder_v1"
PLAN_SNAPSHOT_ORIGIN = "current_derived__human_confirmed_at_freeze"


class DraftInputSnapshot(Base):
    __tablename__ = "draft_input_snapshots"

    __table_args__ = (
        UniqueConstraint(
            "article_id", "content_hash", name="uq_draft_input_snapshots_article_id"
        ),
        Index(
            "ix_draft_input_snapshots_article_frozen_id",
            "article_id",
            "frozen_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # payload["snapshot_version"] と一致。schema 進化時の解釈切替に使う。
    snapshot_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # builder ロジックのバージョン。意味的出力が変わったら更新する (hash INCLUDE)。
    builder_version: Mapped[str] = mapped_column(String(64), nullable=False)

    # V1 は "current_derived__human_confirmed_at_freeze" 固定
    # (過去の Phase 3A Plan は永続化されていないため)。
    plan_snapshot_origin: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- payload から機械導出する検索用の非正規化コピー (§50) ---
    # freeze 時 gate で primary はちょうど 1 なので値は入るが、将来 AffiliateProgram が
    # 削除されても payload 自体は完全な audit artifact として残す (§3-A)。
    primary_affiliate_program_id: Mapped[int | None] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    comparison_program_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    drafting_allowed_at_freeze: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # --- 凍結した入力そのもの ---
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # payload の semantic 部分 (audit/frozen_at/id 等を除外) の SHA-256 hex。
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # freeze を実行した UTC instant (hash 対象外)。
    frozen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # updated_at は持たない (immutable)。

    article: Mapped[Article] = relationship(back_populates="draft_input_snapshots")
    # この Snapshot を入力とした generation run 群。run が存在する間 Snapshot は
    # FK ``ON DELETE RESTRICT`` で削除不能 (defense-in-depth)。run 自体の削除は
    # Article 削除経由の cascade (Article.draft_generation_runs) が担当するため、
    # ここでは cascade を張らない。
    runs: Mapped[list[DraftGenerationRun]] = relationship(back_populates="snapshot")
