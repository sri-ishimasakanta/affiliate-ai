"""RevenueOptimizationCandidate -- 1 件の収益改善候補のスナップショット (C7、append-only)。

候補は **提案** であって作業指示ではない。状態遷移も担当者も期限も持たない
(ワークフロー管理ではない)。

``article_id`` / ``affiliate_program_id`` はどちらも nullable:

- 記事単位の候補は ``article_id`` を持つ。
- プログラム単位の候補 (報酬シグナルなど) は ``affiliate_program_id`` を持つ。
- 計測全体のデータ品質に関する候補はどちらも持たない。

``evidence_basis`` で証拠の出所 (structural / behavioral / program_level /
data_quality) を区別し、構造的な事実と読者の行動を混同できないようにする。

``evidence_json`` に記事本文・credential は入れない。
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


class RevenueOptimizationCandidate(Base):
    __tablename__ = "revenue_optimization_candidates"

    __table_args__ = (
        UniqueConstraint(
            "revenue_optimization_run_id",
            "dedupe_key",
            name="uq_revenue_optimization_candidates_identity",
        ),
        Index("ix_revenue_optimization_candidates_type", "candidate_type"),
        Index(
            "ix_revenue_optimization_candidates_article_run",
            "article_id",
            "revenue_optimization_run_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    revenue_optimization_run_id: Mapped[int] = mapped_column(
        ForeignKey("revenue_optimization_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    affiliate_program_id: Mapped[int | None] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    candidate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    #: high / medium / low。明示規則からのみ決まる (重み付きスコアではない)。
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    #: structural / behavioral / program_level / data_quality。
    evidence_basis: Mapped[str] = mapped_column(String(32), nullable=False)

    suggested_action: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[RevenueOptimizationRun] = relationship(back_populates="candidates")  # noqa: F821
