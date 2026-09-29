"""Growth Action の履歴・レビュー・出来事 (C9 Batch 2)。

- ``growth_action_candidates``: 候補の **版** (revision)。同じ機会 (``opportunity_key``) の中で、
  証拠 (``evidence_fingerprint``) が変わるたびに新しい版を作る。古い版は ``superseded`` になる
  (上書きしない)。同じ版を再び観測したら ``last_seen_at`` と ``seen_count`` だけを進める。
- ``growth_action_reviews``: 人のレビュー。版の指紋・証拠の指紋・見せた内容を固定する。**承認は
  次の段階へ進めてよいという許可だけ** で、WordPress・Threads・公開・アフィリエイトの設定は
  何も変えない (実行の状態を持たない)。
- ``growth_action_events``: 出来事の記録 (追記だけ)。

ChangeRequest (記事の本文の変更) とは別の表にする: 記事・キーワード・サイトの候補を、偽の値で
記事の変更に詰め込まないため。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
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

# -- 候補の版の状態 -----------------------------------------------------------------------------
#: 観測したが、いまは人に出さない (既存の仕事が担う・止める理由がある・情報)。
GA_OBSERVED = "observed"
GA_ACTIVE = "active"  # 人のレビューに出せる
GA_PENDING_REVIEW = "pending_review"
GA_APPROVED = "approved"  # 次の段階へ進めてよい (まだ何も実行していない)
GA_REJECTED = "rejected"
GA_SUPERSEDED = "superseded"  # 同じ機会に新しい証拠の版がある
GA_CONVERTED = "converted"  # 承認のあと、既存の流れの依頼に変えた (このバッチでは作らない)
GA_DISMISSED = "dismissed"  # 人が見送った (レビューなしで)
GA_STATUSES = (GA_OBSERVED, GA_ACTIVE, GA_PENDING_REVIEW, GA_APPROVED, GA_REJECTED,
               GA_SUPERSEDED, GA_CONVERTED, GA_DISMISSED)  # fmt: skip
#: 人の判断で決まった版 (再び観測しても状態を戻さない)。
GA_DECIDED_STATUSES = frozenset({GA_APPROVED, GA_REJECTED, GA_CONVERTED, GA_DISMISSED})
GA_TRANSITIONS: dict[str, frozenset[str]] = {
    GA_OBSERVED: frozenset({GA_ACTIVE, GA_SUPERSEDED, GA_DISMISSED}),
    GA_ACTIVE: frozenset({GA_OBSERVED, GA_PENDING_REVIEW, GA_SUPERSEDED, GA_DISMISSED}),
    GA_PENDING_REVIEW: frozenset({GA_APPROVED, GA_REJECTED, GA_SUPERSEDED}),
    GA_APPROVED: frozenset({GA_CONVERTED, GA_SUPERSEDED}),
    GA_REJECTED: frozenset({GA_SUPERSEDED}),
    GA_SUPERSEDED: frozenset(),
    GA_CONVERTED: frozenset(),
    GA_DISMISSED: frozenset({GA_SUPERSEDED}),
}


def growth_action_transition_allowed(current: str, target: str) -> bool:
    return target in GA_TRANSITIONS.get(current, frozenset())


# -- レビューの状態 ---------------------------------------------------------------------------
GAR_PENDING = "pending"
GAR_APPROVED = "approved"
GAR_REJECTED = "rejected"
GAR_STALE = "stale"  # レビューの間に版が古くなった (承認できない)
GAR_STATUSES = (GAR_PENDING, GAR_APPROVED, GAR_REJECTED, GAR_STALE)

# -- 出来事 ---------------------------------------------------------------------------------
GAE_OBSERVED = "observed"
GAE_AVAILABILITY_CHANGED = "availability_changed"
GAE_SUPERSEDED = "superseded"
GAE_NOT_OBSERVED = "not_observed"
GAE_REVIEW_REQUESTED = "review_requested"
GAE_APPROVED = "approved"
GAE_REJECTED = "rejected"
GAE_REVIEW_STALE = "review_stale"
GAE_DISMISSED = "dismissed"
GAE_CONVERSION_PLANNED = "conversion_planned"
GAE_TYPES = (GAE_OBSERVED, GAE_AVAILABILITY_CHANGED, GAE_SUPERSEDED, GAE_NOT_OBSERVED,
             GAE_REVIEW_REQUESTED, GAE_APPROVED, GAE_REJECTED, GAE_REVIEW_STALE, GAE_DISMISSED,
             GAE_CONVERSION_PLANNED)  # fmt: skip


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


class GrowthActionCandidate(Base):
    __tablename__ = "growth_action_candidates"
    __table_args__ = (
        UniqueConstraint("candidate_fingerprint", name="uq_growth_action_candidates_fingerprint"),
        UniqueConstraint("opportunity_key", "revision",
                         name="uq_growth_action_candidates_opportunity_revision"),
        Index("ix_growth_action_candidates_opportunity", "opportunity_key"),
        Index("ix_growth_action_candidates_status", "status"),
        CheckConstraint(f"status IN ({_in(GA_STATUSES)})", name="growth_action_status"),
        CheckConstraint("revision >= 1", name="growth_action_revision"),
    )  # fmt: skip

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    opportunity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_schema: Mapped[str] = mapped_column(String(48), nullable=False)
    action_type: Mapped[str] = mapped_column(String(48), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL"), nullable=True)  # fmt: skip
    keyword_id: Mapped[int | None] = mapped_column(
        ForeignKey("keywords.id", ondelete="SET NULL"), nullable=True)  # fmt: skip
    variant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    #: いま動けるか (``app.growth.inbox`` の availability)。観測のたびに見直す (変われば出来事)。
    availability: Mapped[str] = mapped_column(String(32), nullable=False)
    availability_reasons_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    evidence_state: Mapped[str] = mapped_column(String(24), nullable=False)
    #: 最初に観測したときの候補そのもの (根拠・止める理由・成分・鮮度)。上書きしない。
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("growth_action_candidates.id", ondelete="SET NULL"), nullable=True)  # fmt: skip
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())  # fmt: skip


class GrowthActionReview(Base):
    """1 つの版に 1 つのレビュー。見せた内容を固定する (承認は固定した指紋が合うときだけ)。"""

    __tablename__ = "growth_action_reviews"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_growth_action_reviews_candidate"),
        Index("ix_growth_action_reviews_status", "status"),
        CheckConstraint(f"status IN ({_in(GAR_STATUSES)})", name="growth_action_review_status"),
    )  # fmt: skip

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("growth_action_candidates.id", ondelete="RESTRICT"), nullable=False)  # fmt: skip
    review_schema: Mapped[str] = mapped_column(String(48), nullable=False)
    opportunity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(48), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    article_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keyword_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: レビューに見せたもの (根拠・証拠・止める理由・成分・変える先の計画)。
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False, default="human")
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())  # fmt: skip


class GrowthActionEvent(Base):
    """出来事 (追記だけ)。"""

    __tablename__ = "growth_action_events"
    __table_args__ = (
        Index("ix_growth_action_events_candidate", "candidate_id"),
        CheckConstraint(f"event_type IN ({_in(GAE_TYPES)})", name="growth_action_event_type"),
    )  # fmt: skip

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("growth_action_candidates.id", ondelete="RESTRICT"), nullable=True)  # fmt: skip
    review_id: Mapped[int | None] = mapped_column(
        ForeignKey("growth_action_reviews.id", ondelete="RESTRICT"), nullable=True)  # fmt: skip
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())  # fmt: skip


__all__ = [
    "GAE_APPROVED", "GAE_AVAILABILITY_CHANGED", "GAE_CONVERSION_PLANNED", "GAE_DISMISSED",
    "GAE_NOT_OBSERVED", "GAE_OBSERVED", "GAE_REJECTED", "GAE_REVIEW_REQUESTED",
    "GAE_REVIEW_STALE", "GAE_SUPERSEDED", "GAE_TYPES", "GAR_APPROVED", "GAR_PENDING",
    "GAR_REJECTED", "GAR_STALE", "GAR_STATUSES", "GA_ACTIVE", "GA_APPROVED", "GA_CONVERTED",
    "GA_DECIDED_STATUSES", "GA_DISMISSED", "GA_OBSERVED", "GA_PENDING_REVIEW", "GA_REJECTED",
    "GA_STATUSES", "GA_SUPERSEDED", "GA_TRANSITIONS", "GrowthActionCandidate",
    "GrowthActionEvent", "GrowthActionReview", "growth_action_transition_allowed",
]  # fmt: skip
