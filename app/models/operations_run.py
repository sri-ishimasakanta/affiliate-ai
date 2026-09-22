"""OperationsRun / OperationsStepRun / OperationsAlert / OperationsLock (C8)。

定期実行の記録を append-only で残す。既存の run 行は書き換えず、実行のたびに
新しい run を append する (C6/C7 と同じ方針)。

``OperationsLock`` だけは append-only ではない -- **同時実行を防ぐための排他** で
あり、その性質上 1 行を取得・解放する必要がある。ただし所有者 (run id) と取得時刻
を必ず持ち、プロセスが死んでも古いロックは時間経過で回収できる (恒久デッドロックに
しない)。

このモジュールのどの表にも credential / tracking URL / ``/go/`` token を保存しない。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
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

# -- run / step status ---------------------------------------------------------
OPS_PREPARED = "prepared"
OPS_RUNNING = "running"
OPS_SUCCEEDED = "succeeded"
OPS_PARTIAL = "partial"
OPS_FAILED = "failed"
OPS_SKIPPED = "skipped"

OPS_STATUSES = frozenset(
    {OPS_PREPARED, OPS_RUNNING, OPS_SUCCEEDED, OPS_PARTIAL, OPS_FAILED, OPS_SKIPPED}
)
OPS_TERMINAL_STATUSES = frozenset({OPS_SUCCEEDED, OPS_PARTIAL, OPS_FAILED, OPS_SKIPPED})

PROFILE_DAILY = "daily"
PROFILE_WEEKLY = "weekly"
PROFILES = (PROFILE_DAILY, PROFILE_WEEKLY)

_SEVERITY_CHECK = "severity IN ('info', 'warning', 'error')"


class OperationsRun(Base):
    __tablename__ = "operations_runs"

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_operations_runs_idempotency_key"),
        Index("ix_operations_runs_profile_date", "profile", "effective_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    profile: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 運用タイムゾーン (ポリシー由来) における対象日。
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: manual / scheduler / test など、何がこの run を起動したか。
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    #: 再現のための環境情報 (取得できなければ NULL)。secret は含まない。
    git_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    step_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list[OperationsStepRun]] = relationship(  # noqa: F821
        back_populates="run", order_by="OperationsStepRun.id"
    )


class OperationsStepRun(Base):
    """1 ステップの実行結果。どの source run を作ったかまで辿れるようにする。"""

    __tablename__ = "operations_step_runs"

    __table_args__ = (Index("ix_operations_step_runs_run_step", "operations_run_id", "step_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    operations_run_id: Mapped[int] = mapped_column(
        ForeignKey("operations_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    step_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    #: 実際に作られた取り込み/評価 run の id (source 側の table は step ごとに異なる)。
    source_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_received: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_changed: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: 例外の型名など、機械可読で secret を含まない分類。
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[OperationsRun] = relationship(back_populates="steps")  # noqa: F821


class OperationsAlert(Base):
    """運用アラート。

    ``fingerprint`` は「同じ問題」を表す決定的なキー。同じ問題が続いている間は
    ``last_seen_at`` を進めるだけで、毎日新しい通知を出さない。
    """

    __tablename__ = "operations_alerts"

    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_operations_alerts_fingerprint"),
        Index("ix_operations_alerts_type_severity", "alert_type", "severity"),
        CheckConstraint(_SEVERITY_CHECK, name="operations_alerts_severity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    alert_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    affiliate_program_id: Mapped[int | None] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    #: open / acknowledged / resolved。最小限の状態のみ (課題管理はしない)。
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")

    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 直近で通知を送った時刻 (cooldown の判定に使う)。
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    operations_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("operations_runs.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OperationsLock(Base):
    """同時実行を防ぐ排他ロック。

    append-only ではない (排他の性質上 1 行を取得・解放する)。所有者と取得時刻を
    必ず持ち、プロセスが死んでも ``stale_lock_after_minutes`` を過ぎれば回収できる。
    """

    __tablename__ = "operations_locks"

    __table_args__ = (UniqueConstraint("lock_name", name="uq_operations_locks_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lock_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ロックを保持している operations run の id。
    owner_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
