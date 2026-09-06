"""SearchConsolePageDaily — page × date の Search Console 日次メトリクス (provider-faithful)。

Search Console が返す URL をそのまま保持する (Article へ resolve しない — 未対応 URL も
ありうるため、attribution は将来の集計層の責務)。同一 (property, date, page) は UPSERT で
更新する (Search Console の直近データは後日 settle して変化するため)。
``source_import_run_id`` は現在その行の値を占めている **最新 run** を指す。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SearchConsolePageDaily(Base):
    __tablename__ = "search_console_page_daily"

    __table_args__ = (
        UniqueConstraint(
            "property_uri",
            "metric_date",
            "page",
            name="uq_search_console_page_daily_property_date_page",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    property_uri: Mapped[str] = mapped_column(String(255), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    page: Mapped[str] = mapped_column(String(2048), nullable=False)

    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # provider-returned 値を検証して保持する (再計算して上書きしない)。
    ctr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    position: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    source_import_run_id: Mapped[int] = mapped_column(
        ForeignKey("search_console_import_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
