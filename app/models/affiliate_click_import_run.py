"""AffiliateClickImportRun — WordPress runtime からの outbound click 取り込みの
auditable な実行記録 (append-only)。

「どの cursor / limit で export GET を叩き、何件受領して何件 replica を書いたか / なぜ
失敗したか」を後日 join なしで再現・監査できるようにする。

- ``updated_at`` を持たない。再取得は新しい run を append する。
- 取り込み先は :class:`~app.models.affiliate_outbound_click.AffiliateOutboundClick`
  (provider-faithful な replica)。
- secret / signature / raw response body / request headers は保存しない。
  ``response_snapshot`` は安全な count / cursor のみ。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# status lifecycle。GET は read-only なので remote mutation ambiguity は無い
# (ambiguous state は不要)。
ACI_RUNNING = "running"
ACI_SUCCEEDED = "succeeded"
ACI_FAILED = "failed"
ACI_STATUSES = frozenset({ACI_RUNNING, ACI_SUCCEEDED, ACI_FAILED})
ACI_TERMINAL_STATUSES = frozenset({ACI_SUCCEEDED, ACI_FAILED})

ACI_TRANSITIONS: dict[str, frozenset[str]] = {
    ACI_RUNNING: frozenset({ACI_SUCCEEDED, ACI_FAILED}),
    ACI_SUCCEEDED: frozenset(),
    ACI_FAILED: frozenset(),
}


def aci_transition_allowed(current: str, target: str) -> bool:
    """``current`` から ``target`` への status 遷移が許可されているか (同一 status は不可)。"""

    return target in ACI_TRANSITIONS.get(current, frozenset())


# run 作成後 immutable な cursor identity フィールド。
FROZEN_FIELDS = ("requested_since_id", "requested_limit")


class AffiliateClickImportRun(Base):
    __tablename__ = "affiliate_click_import_runs"

    __table_args__ = (
        Index("ix_affiliate_click_import_runs_status", "status"),
        # latest-succeeded lookup 用 (status filter + 新しい順)。
        Index(
            "ix_affiliate_click_import_runs_created_id", "created_at", "id"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # 送った cursor / limit (immutable identity)。
    requested_since_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- 実行結果 (lifecycle に沿って populate) ---
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    server_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_next_since_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    inserted_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duplicate_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unresolved_token_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    first_source_click_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    last_source_click_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    has_more: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )

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
