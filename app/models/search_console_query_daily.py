"""SearchConsoleQueryDaily — query × page × date の Search Console 日次メトリクス。

country / device dimension は V1 では持たない (まず query + page 粒度)。
同一 (property, date, page, query) は UPSERT。``source_import_run_id`` は現在その行の値を
占めている最新 run を指す。
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


class SearchConsoleQueryDaily(Base):
    __tablename__ = "search_console_query_daily"

    __table_args__ = (
        UniqueConstraint(
            "property_uri",
            "metric_date",
            "page",
            "query",
            name="uq_search_console_query_daily_property_date_page_query",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    property_uri: Mapped[str] = mapped_column(String(255), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    page: Mapped[str] = mapped_column(String(2048), nullable=False)
    query: Mapped[str] = mapped_column(String(512), nullable=False)

    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
