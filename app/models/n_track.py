"""N 系の手元の記録 (N2 / N3)。**外のサービスには問い合わせない。手で写した数だけ。**

- ``note_pieces`` (N2): note の 1 本ずつの記録。``reports/note/drafts/`` の下書きから同期する
  (``draft_id`` で一意)。状態・公開の形・本文の hash・承認・公開の証拠 (人が note で公開した
  URL) と、重複の検査に使う本文。公開済みは URL が無ければ付けられない。
- ``manual_metric_entries`` (N3): チャネルに依らない、手で写した数の記録 (閲覧・スキ・売上・
  購読者の数・試行の数など)。出どころは ``manual_entry`` だけ (本物の書き出しを見るまでは、
  provider の形を仮定した取り込みを作らない)。数は 0 以上。同じ対象・指標・期間・観測の時刻は
  1 行 (``entry_key``)。直すときは消さずに、新しい行が古い行を ``supersedes_entry_id`` で指す。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    Float,
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

NOTE_PIECE_STATUSES = ("draft", "review_ready", "approved", "published", "rejected")
NOTE_ACCESS_MODES = ("free", "paid")

MM_PROVENANCE_MANUAL = "manual_entry"
MM_PROVENANCES = (MM_PROVENANCE_MANUAL,)
MM_SUBJECT_KINDS = ("note_piece", "channel", "product", "pilot")


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


class NotePiece(Base):
    __tablename__ = "note_pieces"
    __table_args__ = (
        UniqueConstraint("draft_id", name="uq_note_pieces_draft_id"),
        UniqueConstraint("published_url", name="uq_note_pieces_published_url"),
        CheckConstraint(f"status IN ({_in(NOTE_PIECE_STATUSES)})", name="note_piece_status"),
        CheckConstraint(f"access_mode IN ({_in(NOTE_ACCESS_MODES)})", name="note_piece_access"),
        CheckConstraint("status != 'published' OR published_url IS NOT NULL",
                        name="note_piece_published_has_url"),  # fmt: skip
        CheckConstraint("status NOT IN ('approved', 'published') OR approved_hash IS NOT NULL",
                        name="note_piece_approved_has_hash"),  # fmt: skip
        Index("ix_note_pieces_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    access_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="free")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_ids_json: Mapped[Any | None] = mapped_column(JSON)
    edited_by_human: Mapped[bool] = mapped_column(nullable=False, default=False)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_hash: Mapped[str | None] = mapped_column(String(64))
    approval_json: Mapped[Any | None] = mapped_column(JSON)
    published_url: Mapped[str | None] = mapped_column(String(500))
    published_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    draft_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class ManualMetricEntry(Base):
    __tablename__ = "manual_metric_entries"
    __table_args__ = (
        UniqueConstraint("entry_key", name="uq_manual_metric_entries_entry_key"),
        CheckConstraint(f"subject_kind IN ({_in(MM_SUBJECT_KINDS)})", name="manual_metric_subject"),
        CheckConstraint(f"provenance IN ({_in(MM_PROVENANCES)})", name="manual_metric_provenance"),
        CheckConstraint("value >= 0", name="manual_metric_value"),
        CheckConstraint("period_end IS NULL OR period_start IS NULL OR period_end >= period_start",
                        name="manual_metric_period"),  # fmt: skip
        Index("ix_manual_metric_entries_subject", "subject_kind", "subject_ref", "metric"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_key: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provenance: Mapped[str] = mapped_column(String(32), nullable=False)
    source_description: Mapped[str] = mapped_column(String(300), nullable=False)
    entered_by: Mapped[str] = mapped_column(String(64), nullable=False)
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    supersedes_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("manual_metric_entries.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


__all__ = ["MM_PROVENANCES", "MM_PROVENANCE_MANUAL", "MM_SUBJECT_KINDS", "ManualMetricEntry",
           "NOTE_ACCESS_MODES", "NOTE_PIECE_STATUSES", "NotePiece"]
