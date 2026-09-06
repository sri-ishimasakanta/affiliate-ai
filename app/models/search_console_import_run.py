"""SearchConsoleImportRun — Google Search Console 取り込みの auditable な実行記録
(append-only)。

「どの property を / どの期間・どの dimension set で取り込んだ / 何行受領・upsert したか」
を後日 join なしで再現・監査できるようにする。

- ``updated_at`` を持たない。retry は新しい run を append する。
- prepare 後、identity フィールド群は immutable。実行フィールドのみ lifecycle に沿って
  populate される。
- Google の credential / 生レスポンスは保存しない。``response_snapshot`` は安全な
  metadata / count のみ。
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

# status lifecycle。
SC_IMPORT_PREPARED = "prepared"
SC_IMPORT_RUNNING = "running"
SC_IMPORT_SUCCEEDED = "succeeded"
SC_IMPORT_FAILED = "failed"
SC_IMPORT_CANCELLED = "cancelled"
SC_IMPORT_STATUSES = frozenset(
    {
        SC_IMPORT_PREPARED,
        SC_IMPORT_RUNNING,
        SC_IMPORT_SUCCEEDED,
        SC_IMPORT_FAILED,
        SC_IMPORT_CANCELLED,
    }
)
SC_IMPORT_TERMINAL_STATUSES = frozenset(
    {SC_IMPORT_SUCCEEDED, SC_IMPORT_FAILED, SC_IMPORT_CANCELLED}
)
SC_IMPORT_ACTIVE_STATUSES = frozenset({SC_IMPORT_PREPARED, SC_IMPORT_RUNNING})

# 許可された status 遷移。汎用ワークフローエンジンは作らない。
SC_IMPORT_TRANSITIONS: dict[str, frozenset[str]] = {
    SC_IMPORT_PREPARED: frozenset({SC_IMPORT_RUNNING, SC_IMPORT_CANCELLED}),
    SC_IMPORT_RUNNING: frozenset({SC_IMPORT_SUCCEEDED, SC_IMPORT_FAILED}),
    SC_IMPORT_SUCCEEDED: frozenset(),
    SC_IMPORT_FAILED: frozenset(),
    SC_IMPORT_CANCELLED: frozenset(),
}


def sc_import_transition_allowed(current: str, target: str) -> bool:
    """``current`` から ``target`` への status 遷移が許可されているか (同一 status は不可)。"""

    return target in SC_IMPORT_TRANSITIONS.get(current, frozenset())


# prepare 後 immutable な identity フィールド。
FROZEN_PREPARED_FIELDS = (
    "property_uri",
    "start_date",
    "end_date",
    "dimensions_json",
    "import_identity_hash",
    "idempotency_key",
)


class SearchConsoleImportRun(Base):
    __tablename__ = "search_console_import_runs"

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_search_console_import_runs_idempotency_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # canonical Search Console property identity (Domain property は "sc-domain:<host>")。
    property_uri: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # 取り込み対象の dimension set 群 (例: [["page"], ["page","query"]])。
    dimensions_json: Mapped[str] = mapped_column(Text, nullable=False)
    import_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- 実行結果 (lifecycle に沿って populate) ---
    page_rows_received: Mapped[int | None] = mapped_column(Integer, nullable=True)
    query_rows_received: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_rows_upserted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    query_rows_upserted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
