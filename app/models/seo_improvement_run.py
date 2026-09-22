"""SeoImprovementRun -- SEO 改善候補の評価 1 回分の append-only な記録 (C6)。

「今日」「3 日後」「7 日後」「30 日後」の判断を後から比較できるようにするため、
評価のたびに新しい run を append する。既存 run を書き換える手段は持たない。

run には **判断の再現に必要な文脈** を凍結する:

- ``policy_version`` -- どの閾値で判断したか
- ``gsc_data_through`` / ``ga4_data_through`` -- どこまでのデータを見たか
- ``window_start`` / ``window_end`` -- どの期間を集計したか

これらが無いと、後から「なぜあの日は候補が出なかったのか」を説明できない。

この run は記事を一切変更しない。評価は content にも analytics のソース表にも
書き込まない (:class:`SeoImprovementCandidate` を足すだけ)。
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


class SeoImprovementRun(Base):
    __tablename__ = "seo_improvement_runs"

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_seo_improvement_runs_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    policy_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)

    # 判断に使ったデータがどこまで届いていたか (欠けていれば NULL)。
    gsc_data_through: Mapped[date | None] = mapped_column(Date, nullable=True)
    ga4_data_through: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: URL Inspection のスナップショット時刻 (取っていなければ NULL)。
    index_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    evaluated_article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 成熟度の分布など、run 全体の要約 (記事本文は含まない)。
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    candidates: Mapped[list[SeoImprovementCandidate]] = relationship(  # noqa: F821
        back_populates="run"
    )
