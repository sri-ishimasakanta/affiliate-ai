"""承認した Growth Action を既存の流れへ渡す、手元の依頼 (C9-B)。

1 つの表に 3 つの流れの依頼を持つ (どれも **依頼だけ**。外には書かない):

- ``threads_generation``: 記事 (と切り口) を指定した Threads の提案の生成の依頼。在庫の保守が、
  **生成が要るときに**、公平の順の 1 つの代わりに使う (上限・1 記事 1 本・待ちの依頼・直近の切り口は
  在庫の規則のまま。呼び出しの数は増えない)。使うかどうかは方針のスイッチ (既定は使わない)。
  提案は既存の承認・公開の流れのまま。
- ``article_planning``: 記事の計画の依頼。人が承認して、既存の流れで記事を作ったら記事と結ぶ
  (この依頼が記事を作ることはない)。
- ``change_preparation``: 記事の変更 (本文・メタディスクリプション・アフィリエイトの配置) の準備の
  依頼。**具体的な変更の中身は入っていない** (生成しない・推測しない)。人が具体的な変更の依頼
  (ChangeRequest) を作るまでの記録で、変更の依頼は独自の承認と適用のまま。

Growth Action の承認は、どの流れの承認でもない。
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

GH_THREADS_GENERATION = "threads_generation"
GH_ARTICLE_PLANNING = "article_planning"
GH_CHANGE_PREPARATION = "change_preparation"
GH_WORKFLOWS = (GH_THREADS_GENERATION, GH_ARTICLE_PLANNING, GH_CHANGE_PREPARATION)

#: Threads の生成の依頼の種類。
GH_LANE_REGULAR = "regular"
GH_LANE_ALTERNATIVE_ANGLE = "alternative_angle"
GH_LANES = (GH_LANE_REGULAR, GH_LANE_ALTERNATIVE_ANGLE)

#: 変更の準備の種類 (ChangeRequest V2 の種類)。
GH_CHANGE_BODY_UPDATE = "body_update"
GH_CHANGE_META_DESCRIPTION = "meta_description"
GH_CHANGE_AFFILIATE_PLACEMENT = "affiliate_placement"
GH_CHANGE_TYPES = (GH_CHANGE_BODY_UPDATE, GH_CHANGE_META_DESCRIPTION,
                   GH_CHANGE_AFFILIATE_PLACEMENT)  # fmt: skip

# -- 状態 ------------------------------------------------------------------------------------------
GH_PENDING = "pending"
GH_CLAIMED = "claimed"  # Threads: 在庫の保守が生成の依頼にした
GH_PROPOSAL_CREATED = "proposal_created"  # Threads: 提案ができた (承認は提案の流れ)
GH_APPROVED = "approved"  # 記事の計画: 人が承認した (まだ記事は無い)
GH_MATERIALIZED = "materialized"  # 記事の計画: 既存の流れで作った記事と結んだ
GH_PREPARED = "prepared"  # 変更の準備: 人が具体的な変更の依頼を作って結んだ
GH_REJECTED = "rejected"
GH_CANCELLED = "cancelled"
GH_STATUSES = (GH_PENDING, GH_CLAIMED, GH_PROPOSAL_CREATED, GH_APPROVED, GH_MATERIALIZED,
               GH_PREPARED, GH_REJECTED, GH_CANCELLED)  # fmt: skip
#: 流れごとの状態の遷移 (これ以外は断る)。
GH_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    GH_THREADS_GENERATION: {
        GH_PENDING: frozenset({GH_CLAIMED, GH_CANCELLED}),
        GH_CLAIMED: frozenset({GH_PROPOSAL_CREATED, GH_PENDING, GH_CANCELLED}),
        GH_PROPOSAL_CREATED: frozenset(),
        GH_CANCELLED: frozenset(),
    },
    GH_ARTICLE_PLANNING: {
        GH_PENDING: frozenset({GH_APPROVED, GH_REJECTED, GH_CANCELLED}),
        GH_APPROVED: frozenset({GH_MATERIALIZED, GH_CANCELLED}),
        GH_MATERIALIZED: frozenset(),
        GH_REJECTED: frozenset(),
        GH_CANCELLED: frozenset(),
    },
    GH_CHANGE_PREPARATION: {
        GH_PENDING: frozenset({GH_PREPARED, GH_REJECTED, GH_CANCELLED}),
        GH_PREPARED: frozenset(),
        GH_REJECTED: frozenset(),
        GH_CANCELLED: frozenset(),
    },
}
#: まだ終わっていない依頼 (重複を作らないための判定)。
GH_OPEN_STATUSES = frozenset({GH_PENDING, GH_CLAIMED, GH_APPROVED})


def growth_handoff_transition_allowed(workflow: str, current: str, target: str) -> bool:
    return target in GH_TRANSITIONS.get(workflow, {}).get(current, frozenset())


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


class GrowthHandoffRequest(Base):
    __tablename__ = "growth_handoff_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_growth_handoff_requests_idempotency"),
        Index("ix_growth_handoff_requests_workflow_status", "workflow", "status"),
        Index("ix_growth_handoff_requests_article", "article_id"),
        CheckConstraint(f"workflow IN ({_in(GH_WORKFLOWS)})", name="growth_handoff_workflow"),
        CheckConstraint(f"status IN ({_in(GH_STATUSES)})", name="growth_handoff_status"),
        CheckConstraint(f"lane IS NULL OR lane IN ({_in(GH_LANES)})", name="growth_handoff_lane"),
        CheckConstraint(f"change_type IS NULL OR change_type IN ({_in(GH_CHANGE_TYPES)})",
                        name="growth_handoff_change_type"),
    )  # fmt: skip

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    lane: Mapped[str | None] = mapped_column(String(24), nullable=True)
    change_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_growth_action_id: Mapped[int] = mapped_column(
        ForeignKey("growth_action_candidates.id", ondelete="RESTRICT"), nullable=False)  # fmt: skip
    source_review_id: Mapped[int] = mapped_column(
        ForeignKey("growth_action_reviews.id", ondelete="RESTRICT"), nullable=False)  # fmt: skip
    source_candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=True)  # fmt: skip
    keyword_id: Mapped[int | None] = mapped_column(
        ForeignKey("keywords.id", ondelete="SET NULL"), nullable=True)  # fmt: skip
    #: Threads: 依頼の時点で固定した切り口 (``regular`` なら ``None``: 在庫の規則で選ぶ)。
    requested_angle: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 依頼の時点で固定した文脈 (証拠・止める理由・記事の hash・勧めの理由など)。
    frozen_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    frozen_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Threads: 在庫の保守が作った生成の依頼の ID (ファイルの依頼)。
    generation_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 先の実体 (Threads の提案・記事・変更の依頼) の種類と ID。
    downstream_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    downstream_ids_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())  # fmt: skip


__all__ = [
    "GH_APPROVED", "GH_ARTICLE_PLANNING", "GH_CANCELLED", "GH_CHANGE_AFFILIATE_PLACEMENT",
    "GH_CHANGE_BODY_UPDATE", "GH_CHANGE_META_DESCRIPTION", "GH_CHANGE_PREPARATION",
    "GH_CHANGE_TYPES", "GH_CLAIMED", "GH_LANES", "GH_LANE_ALTERNATIVE_ANGLE", "GH_LANE_REGULAR",
    "GH_MATERIALIZED", "GH_OPEN_STATUSES", "GH_PENDING", "GH_PREPARED", "GH_PROPOSAL_CREATED",
    "GH_REJECTED", "GH_STATUSES", "GH_THREADS_GENERATION", "GH_TRANSITIONS", "GH_WORKFLOWS",
    "GrowthHandoffRequest", "growth_handoff_transition_allowed",
]  # fmt: skip
