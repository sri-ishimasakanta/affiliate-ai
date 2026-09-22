"""Ga4PageDaily -- GA4 の **ページ x 日 x チャネル scope** の日次指標 (UPSERT)。

``(property_id, metric_date, page_path, channel_scope)`` が一意。同じ期間を
再取り込みしても行は重複せず、最新の取り込み値で上書きされる
(:class:`SearchConsolePageDaily` と同じ refresh セマンティクス)。

**channel_scope を行の次元として持つ** のが設計上の要点:

- ``all``            -- そのページの全トラフィック
- ``organic_search`` -- ``sessionDefaultChannelGroup == "Organic Search"`` の部分集合

全トラフィックをオーガニックと呼ばないために、両者を別 scope の行として保持し、
合算・按分・推定は一切しない。``organic_search`` の値は常に ``all`` 以下である
はずで、そうでなければデータ品質チェックが検出する。

``metric_date`` は **property のタイムゾーンの暦日** (GA4 の ``date`` dimension を
そのまま保持する)。タイムゾーン自体は :class:`Ga4ImportRun` に凍結する。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Ga4PageDaily(Base):
    __tablename__ = "ga4_page_daily"

    __table_args__ = (
        UniqueConstraint(
            "property_id",
            "metric_date",
            "page_path",
            "channel_scope",
            name="uq_ga4_page_daily_identity",
        ),
        Index("ix_ga4_page_daily_property_date", "property_id", "metric_date"),
        CheckConstraint(
            "channel_scope IN ('all', 'organic_search')", name="ga4_page_daily_channel_scope"
        ),
        CheckConstraint(
            "sessions >= 0 AND active_users >= 0 AND new_users >= 0 "
            "AND engaged_sessions >= 0 AND screen_page_views >= 0",
            name="ga4_page_daily_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    property_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    # GA4 の pagePath (query string を含まない正規形。origin は property 側に属する)。
    page_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    channel_scope: Mapped[str] = mapped_column(String(32), nullable=False)

    sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engaged_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    screen_page_views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # GA4 が返す比率/秒数をそのまま保持する (こちらで再計算しない)。
    engagement_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    average_engagement_time_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )

    source_import_run_id: Mapped[int] = mapped_column(
        ForeignKey("ga4_import_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
