"""SeoImprovementCandidate -- 1 件の SEO 改善候補のスナップショット (append-only、C6)。

候補は **提案** であって作業指示ではない。この table はワークフロー管理ではなく、
「その日の証拠でそう判断した」という履歴として使う -- だから状態遷移も担当者も
期限も持たない。

``dedupe_key`` は (run をまたいで安定な) 候補の identity で、
``(article_id, candidate_type, 対象/クエリ等)`` から決まる。同じ run 内での重複を
DB 層で禁止し、run 間では「同じ候補が何日続いているか」を追える。

``evidence_json`` には判断に使った指標・ゲート値・クエリ証拠を入れる。記事本文や
credential は入れない。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

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


class SeoImprovementCandidate(Base):
    __tablename__ = "seo_improvement_candidates"

    __table_args__ = (
        UniqueConstraint(
            "seo_improvement_run_id", "dedupe_key", name="uq_seo_improvement_candidates_identity"
        ),
        Index("ix_seo_improvement_candidates_type", "candidate_type"),
        Index("ix_seo_improvement_candidates_article_run", "article_id", "seo_improvement_run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    seo_improvement_run_id: Mapped[int] = mapped_column(
        ForeignKey("seo_improvement_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    candidate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    #: high / medium / low。明示規則からのみ決まる (重み付きスコアではない)。
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    #: performance_derived / structural / source_metadata / index_observation。
    evidence_strength: Mapped[str] = mapped_column(String(32), nullable=False)

    suggested_action: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[SeoImprovementRun] = relationship(back_populates="candidates")  # noqa: F821
