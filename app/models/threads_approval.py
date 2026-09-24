"""ThreadsApprovalDigest / ThreadsQueueControlEvent (T4.2)。

**まとめ送り (digest)** は「複数の承認依頼を 1 通のメールで届けた」という事実の記録
である。決定の単位ではない。1 通に 3 件入っていても、承認/却下はセッションごとに
独立しており、digest 全体を承認する操作は存在しない。

**queue 操作の履歴** は append-only。保留・解除・次に優先・時刻制約の変更を、
誰が・いつ・なぜ行ったかを残す。現在の状態は提案の行 (``held_at`` など) が持ち、
ここは説明のための履歴だけを持つ。秘密情報は入れない。
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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# -- digest outcomes -----------------------------------------------------------
DIGEST_SENT = "sent"
#: メールが届かなかった。作ったセッションは失効させ、提案は在庫に戻す。
DIGEST_FAILED = "failed"
DIGEST_OUTCOMES = (DIGEST_SENT, DIGEST_FAILED)

# -- queue control actions -----------------------------------------------------
QC_HOLD = "hold"
QC_RELEASE = "release"
QC_PREFER_NEXT = "prefer_next"
QC_CLEAR_PREFERENCE = "clear_preference"
QC_SET_TIMING = "set_timing"
QC_ACTIONS = (QC_HOLD, QC_RELEASE, QC_PREFER_NEXT, QC_CLEAR_PREFERENCE, QC_SET_TIMING)


class ThreadsApprovalDigest(Base):
    __tablename__ = "threads_approval_digests"

    __table_args__ = (
        CheckConstraint("outcome IN ('sent', 'failed')", name="threads_approval_digests_outcome"),
        Index("ix_threads_approval_digests_sent_at", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 送信の操作を始めた時刻。承認の期限はこの操作の時刻から数える。
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: メールが届いた時刻。失敗なら NULL。
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notification_delivery_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_deliveries.id", ondelete="RESTRICT"), nullable=True
    )
    #: 入れた提案 (id と選んだ理由)。本文は入れない (提案の行が本文の権威)。
    selected_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    #: 見送った提案と理由 (在庫には残る)。
    suppressed_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 人が CLI で通知窓 (08:00-21:00) を明示的に上書きして送った場合の理由。
    #: NULL = 上書きなし (通常の送信)。飛ばしたのは窓だけで、他の規則はすべて効いている。
    window_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 上書きを伴う送信の操作をした時刻 (上書きなしなら NULL)。
    window_override_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ThreadsQueueControlEvent(Base):
    __tablename__ = "threads_queue_control_events"

    __table_args__ = (
        CheckConstraint(
            "action IN ('hold', 'release', 'prefer_next', 'clear_preference', 'set_timing')",
            name="threads_queue_control_events_action",
        ),
        Index("ix_threads_queue_control_events_proposal", "proposal_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("threads_post_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    #: 操作した主体 (例: "human-cli")。自動では付けない。
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 変更前後の値など、説明に必要な事実だけ。
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "DIGEST_FAILED",
    "DIGEST_OUTCOMES",
    "DIGEST_SENT",
    "QC_ACTIONS",
    "QC_CLEAR_PREFERENCE",
    "QC_HOLD",
    "QC_PREFER_NEXT",
    "QC_RELEASE",
    "QC_SET_TIMING",
    "ThreadsApprovalDigest",
    "ThreadsQueueControlEvent",
]
