"""Ga4ImportRun -- GA4 Data API 取り込みの auditable な実行記録 (append-only)。

:class:`SearchConsoleImportRun` と同じ 4-state lifecycle
(``prepared -> running -> succeeded|failed``) を採る。retry は新しい run を
append する -- terminal な run を書き換えない。

GA4 固有の注意:

- **日付は property のタイムゾーンの暦日** である。UTC / JST の暦日から変換なしに
  導出してはならないため、``property_timezone`` を run に凍結して記録する。
- GA4 の当日/直近データは確定していない。``end_date`` に lag を置くかどうかは
  呼び出し側の判断であり、モデルは事実 (要求した期間) だけを保持する。
- 「全トラフィック」と「オーガニック検索」は別 scope の行として取り込む
  (:mod:`app.analytics.import_identity` の ``CHANNEL_SCOPES``)。両者を 1 行に
  混ぜない。
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

GA4_IMPORT_PREPARED = "prepared"
GA4_IMPORT_RUNNING = "running"
GA4_IMPORT_SUCCEEDED = "succeeded"
GA4_IMPORT_FAILED = "failed"

GA4_IMPORT_STATUSES = frozenset(
    {GA4_IMPORT_PREPARED, GA4_IMPORT_RUNNING, GA4_IMPORT_SUCCEEDED, GA4_IMPORT_FAILED}
)
GA4_IMPORT_ACTIVE_STATUSES = frozenset({GA4_IMPORT_PREPARED, GA4_IMPORT_RUNNING})
GA4_IMPORT_TERMINAL_STATUSES = frozenset({GA4_IMPORT_SUCCEEDED, GA4_IMPORT_FAILED})

GA4_IMPORT_TRANSITIONS: dict[str, frozenset[str]] = {
    GA4_IMPORT_PREPARED: frozenset({GA4_IMPORT_RUNNING, GA4_IMPORT_FAILED}),
    GA4_IMPORT_RUNNING: frozenset({GA4_IMPORT_SUCCEEDED, GA4_IMPORT_FAILED}),
    GA4_IMPORT_SUCCEEDED: frozenset(),
    GA4_IMPORT_FAILED: frozenset(),
}


def ga4_import_transition_allowed(current: str, target: str) -> bool:
    return target in GA4_IMPORT_TRANSITIONS.get(current, frozenset())


# prepare 後 immutable な identity フィールド。
FROZEN_PREPARED_FIELDS = (
    "property_id",
    "start_date",
    "end_date",
    "request_json",
    "import_identity_hash",
    "idempotency_key",
)


class Ga4ImportRun(Base):
    __tablename__ = "ga4_import_runs"

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ga4_import_runs_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # GA4 property の数値 id ("properties/" 接頭辞なしの正規形)。
    property_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 取り込み時点で観測した property のタイムゾーン (判明していれば)。
    property_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # 要求した dimension / metric / channel scope の canonical 表現。
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    import_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- 実行結果 ---
    page_rows_received: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_rows_upserted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_through_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # updated_at は持たない (append-only)。
