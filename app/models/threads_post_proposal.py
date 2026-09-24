"""ThreadsPostProposal (T2)。

1 本の記事から作られた **不変の Threads 投稿案** 1 件。承認はこの 1 つに結び付く。

C9 の ``ChangeRequest`` と同じ約束を持ち込む:

- 内容 (最終テキスト・リンク先・hash・記事本文 hash) を作成時に凍結する。
- 作り直した提案は **別レコード** になる。古いものは ``superseded`` になる。
- 承認だけでは **何も公開されない**。公開は T3 の別操作である。

公開の記録 (Threads の media id / 指標) はここに置かない。それは T3 の担当で、
提案は「何を承認したか」だけを持つ。
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

# -- states --------------------------------------------------------------------
TP_PROPOSED = "proposed"
TP_AWAITING_APPROVAL = "awaiting_approval"
TP_APPROVED = "approved"
TP_REJECTED = "rejected"
TP_STALE = "stale"
TP_SUPERSEDED = "superseded"

TP_STATES = frozenset(
    {TP_PROPOSED, TP_AWAITING_APPROVAL, TP_APPROVED, TP_REJECTED, TP_STALE, TP_SUPERSEDED}
)
#: 人の判断をまだ待っている状態。
TP_OPEN_STATES = frozenset({TP_PROPOSED, TP_AWAITING_APPROVAL})

TP_TRANSITIONS: dict[str, frozenset[str]] = {
    TP_PROPOSED: frozenset({TP_AWAITING_APPROVAL, TP_REJECTED, TP_STALE, TP_SUPERSEDED}),
    TP_AWAITING_APPROVAL: frozenset({TP_APPROVED, TP_REJECTED, TP_STALE, TP_SUPERSEDED}),
    # 承認しても、公開されるまでは記事が変わって陳腐化しうる。
    TP_APPROVED: frozenset({TP_STALE, TP_SUPERSEDED, TP_REJECTED}),
    TP_REJECTED: frozenset(),
    TP_STALE: frozenset({TP_SUPERSEDED}),
    TP_SUPERSEDED: frozenset(),
}


def threads_proposal_transition_allowed(current: str, target: str) -> bool:
    return target in TP_TRANSITIONS.get(current, frozenset())


class ThreadsPostProposal(Base):
    __tablename__ = "threads_post_proposals"

    __table_args__ = (
        UniqueConstraint("proposal_hash", name="uq_threads_post_proposals_hash"),
        Index("ix_threads_post_proposals_article", "source_article_id"),
        Index("ix_threads_post_proposals_status", "status"),
        CheckConstraint("link_mode IN ('none', 'article')", name="threads_proposals_link_mode"),
        CheckConstraint("character_count > 0", name="threads_proposals_length"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    source_article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False
    )
    #: 生成時点の記事本文 hash。変わっていれば提案は陳腐化する。
    source_article_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    angle: Mapped[str] = mapped_column(String(32), nullable=False)
    link_mode: Mapped[str] = mapped_column(String(16), nullable=False)

    #: 実際に投稿される **そのままの文字列**。人はこれを見て承認する。
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False)
    destination_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    #: URL より先に決まる内容 identity (循環を避けるための中間段)。
    content_seed: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 承認が結び付く最終 identity。
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default=TP_PROPOSED)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 人の確認画面に出す注意 (自動では落とさなかったもの)。
    warnings_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)

    #: 反映された人の判断 (C8.8 の承認レコード)。
    change_request_approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("change_request_approvals.id", ondelete="RESTRICT"), nullable=True
    )
    #: 作り直したときに、どの提案に置き換わったか。
    superseded_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- T4.2: 承認の時刻 (権威ある記録) -----------------------------------------
    #: 人の承認がシステムに **受理された** 時刻。updated_at から推測しない。
    #: 過去の行は、同じ意味を持つ既存の記録 (携帯承認の decided_at) がある場合だけ埋める。
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 承認依頼が **実際に届けられた** 時刻 (メール送信が成功した時刻)。最新の依頼。
    #: NULL = まだ依頼していない (在庫で待っている)。承認の期限はここから数える。
    approval_request_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # -- T4.2: 時刻の制約 (任意) ---------------------------------------------------
    #: この時刻より前には公開の資格を持たない。
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: この時刻を過ぎたら公開に適さない。**常緑の提案には付けない** (古いだけで期限にしない)。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- T4.2: 人の queue 操作 (現在の状態。履歴は threads_queue_control_events) -----
    #: 保留中なら保留を始めた時刻。NULL = 保留していない。
    held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 「次に優先」を指定した時刻。NULL = 指定なし。**安全の条件は一切飛ばさない。**
    preferred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
