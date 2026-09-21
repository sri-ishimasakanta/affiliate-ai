"""AffiliateCommissionImportRun — ASP (現状 Make のみ) commission API 取り込みの
auditable な実行記録 (append-only)。

``affiliate_click_import_run.py`` と同じ設計方針: 「どの provider / program / 期間
(date range) を要求し、何ページ・何件受領して何件 insert / update / unchanged したか」
を後日 join なしで再現・監査できるようにする。read-only GET のみなので
:class:`AffiliateClickImportRun` と同様、remote mutation ambiguity は無い
(ambiguous outcome 状態は不要)。

- ``updated_at`` を持たない。再取得は新しい run を append する。
- API token / Authorization ヘッダ値 / raw response body は保存しない。
  ``response_snapshot`` は安全な count / date range のみ。
- ``requested_date_from`` / ``requested_date_to`` は run 作成後 immutable
  (Make API はどちらも optional なので両方 NULL もありうる = 全期間取り込み)。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# status lifecycle。GET のみなので ambiguous state は不要 (D-D2 click import と同型)。
ACIR_RUNNING = "running"
ACIR_SUCCEEDED = "succeeded"
ACIR_FAILED = "failed"
ACIR_STATUSES = frozenset({ACIR_RUNNING, ACIR_SUCCEEDED, ACIR_FAILED})
ACIR_TERMINAL_STATUSES = frozenset({ACIR_SUCCEEDED, ACIR_FAILED})

ACIR_TRANSITIONS: dict[str, frozenset[str]] = {
    ACIR_RUNNING: frozenset({ACIR_SUCCEEDED, ACIR_FAILED}),
    ACIR_SUCCEEDED: frozenset(),
    ACIR_FAILED: frozenset(),
}


def acir_transition_allowed(current: str, target: str) -> bool:
    """``current`` から ``target`` への status 遷移が許可されているか (同一 status は不可)。"""

    return target in ACIR_TRANSITIONS.get(current, frozenset())


# run 作成後 immutable な identity フィールド。
FROZEN_FIELDS = (
    "provider",
    "affiliate_program_id",
    "requested_date_from",
    "requested_date_to",
)


class AffiliateCommissionImportRun(Base):
    __tablename__ = "affiliate_commission_import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # "make" (将来の他 ASP を見越して namespace を持つ -- D-F1/D-F2A と同じ理由)。
    provider: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    affiliate_program_id: Mapped[int] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # 要求した date range (immutable identity)。Make API はどちらも optional。
    requested_date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    requested_date_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- 実行結果 (lifecycle に沿って populate) ---
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inserted_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unchanged_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

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
