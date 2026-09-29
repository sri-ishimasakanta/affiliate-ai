"""夜の分析の実行の記録と、発見した語の候補 (C10-2)。**手元の記録だけ。**

- ``nightly_analysis_runs``: 1 日 1 回の分析の実行 (``run_key`` = ``nightly:<日付>`` で一意。
  同じ日の 2 回目は前の結果を返す。失敗した実行はやり直せる: ``attempt_count``)。数・外の
  取り直しの計画・失敗の理由を持つ。
- ``content_discovery_candidates``: 発見した語 (``phrase_key`` で一意)。**Keyword にはしない**
  (人が決める)。最初と最後に見た時刻・出所・根拠・クラスタ・重なりの状態・取り直しの要る
  出所。既にある Keyword と同じ語は ``suppressed``、後で Keyword になったら ``promoted``。
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

NAR_RUNNING = "running"
NAR_SUCCEEDED = "succeeded"
NAR_FAILED = "failed"
NAR_STATUSES = (NAR_RUNNING, NAR_SUCCEEDED, NAR_FAILED)

CDC_TRACKED = "tracked"
CDC_SUPPRESSED = "suppressed"
CDC_PROMOTED = "promoted"
CDC_DISMISSED = "dismissed"
CDC_STATUSES = (CDC_TRACKED, CDC_SUPPRESSED, CDC_PROMOTED, CDC_DISMISSED)


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


class NightlyAnalysisRun(Base):
    __tablename__ = "nightly_analysis_runs"
    __table_args__ = (
        UniqueConstraint("run_key", name="uq_nightly_analysis_runs_run_key"),
        CheckConstraint(f"status IN ({_in(NAR_STATUSES)})", name="nightly_analysis_status"),
        CheckConstraint("attempt_count >= 1", name="nightly_analysis_attempts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_key: Mapped[str] = mapped_column(String(64), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    budget: Mapped[int] = mapped_column(Integer, nullable=False)
    universe_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analyzed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deferred_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refresh_required_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    counts_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    refresh_plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())  # fmt: skip


class ContentDiscoveryCandidate(Base):
    __tablename__ = "content_discovery_candidates"
    __table_args__ = (
        UniqueConstraint("phrase_key", name="uq_content_discovery_candidates_phrase_key"),
        Index("ix_content_discovery_candidates_status", "status"),
        CheckConstraint(f"status IN ({_in(CDC_STATUSES)})", name="content_discovery_status"),
        CheckConstraint("seen_count >= 1", name="content_discovery_seen"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phrase_key: Mapped[str] = mapped_column(String(255), nullable=False)
    phrase: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duplicate_state: Mapped[str] = mapped_column(String(32), nullable=False)
    duplicate_ref_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cluster_key: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cluster_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sources_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    refresh_needs_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("nightly_analysis_runs.id", ondelete="SET NULL"), nullable=True)
    last_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("nightly_analysis_runs.id", ondelete="SET NULL"), nullable=True)
    keyword_id: Mapped[int | None] = mapped_column(
        ForeignKey("keywords.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())  # fmt: skip
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["CDC_DISMISSED", "CDC_PROMOTED", "CDC_STATUSES", "CDC_SUPPRESSED", "CDC_TRACKED",
           "ContentDiscoveryCandidate", "NAR_FAILED", "NAR_RUNNING", "NAR_STATUSES",
           "NAR_SUCCEEDED", "NightlyAnalysisRun"]
