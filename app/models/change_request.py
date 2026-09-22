"""ChangeRequest / ChangeRequestApproval / ChangeApplication (C9)。

助言 (C6/C7 の候補) と、実際の記事変更のあいだに置く **人間の承認境界**。

設計上の約束:

- **承認は 1 つの正確な提案に結び付く。** ``proposal_hash`` は提案の全内容
  (記事・変更前 body hash・アンカー・挿入位置・パッチ・変更後 body hash) から
  決まる。内容が変われば別の ``proposal_version`` の別レコードになる。承認が
  「あとでシステムが作る文章を承認する」意味になることは絶対にない。
- **候補への不変リンク**を凍結する (engine / run / candidate id / dedupe key /
  priority / policy version / evidence)。あとから「なぜこの変更を提案したのか」を
  再現できる。
- ``ChangeApplication`` は append-only。適用の試行は上書きせず積む。失敗も
  ``outcome_unknown`` も履歴として残る。
- 状態遷移は狭い。``approved`` を経ずに ``applied`` にはならない。

このモデル自体は WordPress に触れない。適用は既存の managed な編集改訂 →
WordPress 更新経路に委譲する (C9 は新しい更新スタックを作らない)。
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# -- status --------------------------------------------------------------------
CR_PROPOSED = "proposed"
CR_AWAITING_APPROVAL = "awaiting_approval"
CR_APPROVED = "approved"
CR_REJECTED = "rejected"
CR_STALE = "stale"
CR_APPLIED = "applied"
CR_APPLY_FAILED = "apply_failed"
CR_RECONCILED = "reconciled"

CR_STATUSES = frozenset(
    {
        CR_PROPOSED,
        CR_AWAITING_APPROVAL,
        CR_APPROVED,
        CR_REJECTED,
        CR_STALE,
        CR_APPLIED,
        CR_APPLY_FAILED,
        CR_RECONCILED,
    }
)
#: 人が判断する前の状態。
CR_OPEN_STATUSES = frozenset({CR_PROPOSED, CR_AWAITING_APPROVAL})
#: これ以上自動で進まない状態。
CR_TERMINAL_STATUSES = frozenset({CR_REJECTED, CR_APPLIED, CR_RECONCILED})

CR_TRANSITIONS: dict[str, frozenset[str]] = {
    CR_PROPOSED: frozenset({CR_AWAITING_APPROVAL, CR_REJECTED, CR_STALE}),
    CR_AWAITING_APPROVAL: frozenset({CR_APPROVED, CR_REJECTED, CR_STALE}),
    # 承認しても、適用されるまでは古くなりうる。
    CR_APPROVED: frozenset({CR_APPLIED, CR_APPLY_FAILED, CR_STALE, CR_REJECTED}),
    # 書き込み前に失敗した適用は、人が明示的に再開したときだけ approved へ戻せる
    # (C9.4)。承認をやり直すのではなく、同じ承認のまま再試行するための遷移。
    CR_APPLY_FAILED: frozenset({CR_APPLIED, CR_RECONCILED, CR_STALE, CR_REJECTED, CR_APPROVED}),
    CR_APPLIED: frozenset({CR_RECONCILED}),
    CR_STALE: frozenset({CR_REJECTED}),
    CR_REJECTED: frozenset(),
    CR_RECONCILED: frozenset(),
}


def change_request_transition_allowed(current: str, target: str) -> bool:
    return target in CR_TRANSITIONS.get(current, frozenset())


# -- change types (V1) ---------------------------------------------------------
CHANGE_ADD_INTERNAL_LINK = "add_internal_link"
CHANGE_REMOVE_OR_REPLACE_INTERNAL_LINK = "remove_or_replace_internal_link"
CHANGE_TEXT_EDIT = "text_edit"
CHANGE_AFFILIATE_LINK_CHANGE = "affiliate_link_change"

CHANGE_TYPES = (
    CHANGE_ADD_INTERNAL_LINK,
    CHANGE_REMOVE_OR_REPLACE_INTERNAL_LINK,
    CHANGE_TEXT_EDIT,
    CHANGE_AFFILIATE_LINK_CHANGE,
)
#: V1 で自動生成してよいのはこれだけ (他は表現できるが生成しない)。
CHANGE_TYPES_GENERATED_IN_V1 = (CHANGE_ADD_INTERNAL_LINK,)

ENGINE_SEO = "seo"
ENGINE_REVENUE = "revenue"
ENGINE_MANUAL = "manual"
ENGINES = (ENGINE_SEO, ENGINE_REVENUE, ENGINE_MANUAL)


class ChangeRequest(Base):
    __tablename__ = "change_requests"

    __table_args__ = (
        UniqueConstraint("article_id", "proposal_hash", name="uq_change_requests_article_proposal"),
        UniqueConstraint("idempotency_key", name="uq_change_requests_idempotency_key"),
        Index("ix_change_requests_status", "status"),
        Index("ix_change_requests_candidate", "source_engine", "source_candidate_id"),
        CheckConstraint(
            "source_engine IN ('seo', 'revenue', 'manual')", name="change_requests_engine"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # -- 候補への不変リンク (なぜ提案したかの再現用) -------------------------
    source_engine: Mapped[str] = mapped_column(String(16), nullable=False)
    source_candidate_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_candidate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_candidate_dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_candidate_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_candidate_priority: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: 内部リンクの場合のリンク先 (他の変更種別では NULL)。
    target_article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=True
    )

    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: 提案の全内容から決まる identity。承認はこの値に結び付く。
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: 提案時点の記事 body の hash (適用直前に再検証する)。
    expected_source_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 適用後に期待される body の hash。
    proposed_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 適用する本文そのもの (提案時に確定させ、後から作り直さない)。
    proposed_body: Mapped[str] = mapped_column(Text, nullable=False)

    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    #: アンカー・挿入位置・前後文脈・差分など、人が読んで判断するための素材。
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default=CR_PROPOSED)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    approvals: Mapped[list[ChangeRequestApproval]] = relationship(  # noqa: F821
        back_populates="request", order_by="ChangeRequestApproval.id"
    )
    applications: Mapped[list[ChangeApplication]] = relationship(  # noqa: F821
        back_populates="request", order_by="ChangeApplication.id"
    )


class ChangeRequestApproval(Base):
    """承認/却下の記録 (append-only)。

    ``approved_proposal_hash`` を必ず持つ。提案が作り直されたら hash が変わるので、
    古い承認が新しい提案に **移らない**。
    """

    __tablename__ = "change_request_approvals"

    __table_args__ = (
        Index("ix_change_request_approvals_request", "change_request_id"),
        CheckConstraint("decision IN ('approved', 'rejected')", name="change_approvals_decision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    change_request_id: Mapped[int] = mapped_column(
        ForeignKey("change_requests.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 承認した時点の提案 hash。適用前に request の現在値と突き合わせる。
    approved_proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)

    decided_by: Mapped[str] = mapped_column(String(64), nullable=False, default="human")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 承認時点で候補がまだ存在していたか (消えていても人が決める)。
    candidate_present_at_decision: Mapped[bool | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    request: Mapped[ChangeRequest] = relationship(back_populates="approvals")  # noqa: F821


class ChangeApplication(Base):
    """適用の試行 1 回分 (append-only)。成功も失敗も上書きしない。"""

    __tablename__ = "change_applications"

    __table_args__ = (
        Index("ix_change_applications_request", "change_request_id"),
        UniqueConstraint("idempotency_key", name="uq_change_applications_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    change_request_id: Mapped[int] = mapped_column(
        ForeignKey("change_requests.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    change_request_approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("change_request_approvals.id", ondelete="RESTRICT"), nullable=True
    )
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 適用直前に観測した状態 (巻き戻しの起点にもなる)。
    source_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 変更前の本文そのもの。ロールバックはこれを使う。
    pre_change_body: Mapped[str] = mapped_column(Text, nullable=False)

    wordpress_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wordpress_pre_modified_gmt: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wordpress_post_modified_gmt: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 既存の managed 更新経路が作った run (新しい更新スタックは作らない)。
    editorial_revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_update_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reconciliation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: planned / succeeded / failed / outcome_unknown / reconciled。
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    request: Mapped[ChangeRequest] = relationship(back_populates="applications")  # noqa: F821
