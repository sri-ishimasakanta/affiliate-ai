"""RevenueOptimizationRun -- 収益最適化の評価 1 回分の append-only な記録 (C7)。

C6 の :class:`SeoImprovementRun` と同じ形。既存 run は書き換えず、評価のたびに
新しい run を append する。

判断の再現に必要な文脈を凍結する -- 特に **``trusted_measurement_start_at``**:
どの時点以降のクリックを読者行動として扱ったかが分からないと、「なぜこの日は
行動系の候補が出なかったのか」を後から説明できない。

この run は記事も WordPress も一切変更しない。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class RevenueOptimizationRun(Base):
    __tablename__ = "revenue_optimization_runs"

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_revenue_optimization_runs_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    policy_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)

    #: この時点以降のクリックだけを読者行動として扱った、という境界。
    trusted_measurement_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    ga4_data_through: Mapped[date | None] = mapped_column(Date, nullable=True)
    affiliate_click_data_through: Mapped[date | None] = mapped_column(Date, nullable=True)
    commission_data_through: Mapped[date | None] = mapped_column(Date, nullable=True)

    raw_clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clean_clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    evaluated_article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monetized_article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    candidates: Mapped[list[RevenueOptimizationCandidate]] = relationship(  # noqa: F821
        back_populates="run"
    )
