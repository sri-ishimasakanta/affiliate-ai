"""MobileApprovalSession / MobileApprovalEvent (C8.8)。

外出先の携帯から「承認だけ」を行うための封筒。**承認は承認であって適用ではない。**
このモデルは WordPress にも記事にも触れず、人の決定を運ぶ経路の状態を持つだけで
ある。

権限の境界:

- **ローカルの affiliate-ai が唯一の権威**である。提案 hash / 版 / 記事の状態 /
  陳腐化判定 / 承認レコード / 適用は、すべてこちら側の C9 service が決める。
- 公開側 (WordPress) は **決定の中継** にすぎない。中継が記事を書き換えることは
  構造上ありえない (中継は記事に触れる術を持たない)。

汎用性:

``subject_type`` を持つ封筒にしてあるので、あとで Threads の投稿提案
(``threads_post``) を同じ経路に載せられる。いまは C9 の ``change_request`` だけを
実装する。中継側の token/決定の仕組みに記事固有の前提を入れない。

capability (レビュー URL の secret) は **digest しか保存しない**。生の値は
DB にもログにも通知履歴にも例外にも出さない。
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

# -- subject types -------------------------------------------------------------
SUBJECT_CHANGE_REQUEST = "change_request"
#: まだ実装しないが、封筒としては表現できる (C8.8 の設計要件)。
SUBJECT_THREADS_POST = "threads_post"
SUBJECT_TYPES = (SUBJECT_CHANGE_REQUEST, SUBJECT_THREADS_POST)
#: V1 で実際にセッションを作れるのはこれだけ。
SUBJECT_TYPES_SUPPORTED_IN_V1 = (SUBJECT_CHANGE_REQUEST,)

# -- states --------------------------------------------------------------------
MA_PENDING = "pending"
MA_APPROVED_REMOTE = "approved_remote"
MA_REJECTED_REMOTE = "rejected_remote"
MA_SYNCHRONIZED = "synchronized"
MA_EXPIRED = "expired"
MA_REVOKED = "revoked"
MA_STALE = "stale"
MA_FAILED = "failed"

MA_STATES = frozenset(
    {
        MA_PENDING,
        MA_APPROVED_REMOTE,
        MA_REJECTED_REMOTE,
        MA_SYNCHRONIZED,
        MA_EXPIRED,
        MA_REVOKED,
        MA_STALE,
        MA_FAILED,
    }
)
#: まだ人の決定を受け付けられる状態。
MA_OPEN_STATES = frozenset({MA_PENDING})
#: 決定は届いたが、まだローカルへ反映していない状態。
MA_DECIDED_STATES = frozenset({MA_APPROVED_REMOTE, MA_REJECTED_REMOTE})

MA_TRANSITIONS: dict[str, frozenset[str]] = {
    MA_PENDING: frozenset(
        {MA_APPROVED_REMOTE, MA_REJECTED_REMOTE, MA_EXPIRED, MA_REVOKED, MA_STALE}
    ),
    # 決定が届いたあとでも、ローカルが陳腐化していれば反映せず stale にする。
    MA_APPROVED_REMOTE: frozenset({MA_SYNCHRONIZED, MA_STALE, MA_FAILED}),
    MA_REJECTED_REMOTE: frozenset({MA_SYNCHRONIZED, MA_STALE, MA_FAILED}),
    # 反映に失敗しても決定は消さない。直してから再試行できる。
    MA_FAILED: frozenset({MA_SYNCHRONIZED, MA_STALE}),
    MA_SYNCHRONIZED: frozenset(),
    MA_EXPIRED: frozenset(),
    MA_REVOKED: frozenset(),
    MA_STALE: frozenset(),
}


def mobile_approval_transition_allowed(current: str, target: str) -> bool:
    return target in MA_TRANSITIONS.get(current, frozenset())


# -- decisions -----------------------------------------------------------------
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
DECISIONS = (DECISION_APPROVED, DECISION_REJECTED)

#: 承認レコードに残す決定者。人の決定であることを保ったまま、経路を区別する。
DECIDED_BY_MOBILE = "human-mobile"


class MobileApprovalSession(Base):
    __tablename__ = "mobile_approval_sessions"

    __table_args__ = (
        UniqueConstraint("relay_session_id", name="uq_mobile_approval_relay_session"),
        UniqueConstraint("capability_digest", name="uq_mobile_approval_capability"),
        Index("ix_mobile_approval_subject", "subject_type", "subject_id"),
        Index("ix_mobile_approval_state", "state"),
        CheckConstraint(
            "subject_type IN ('change_request', 'threads_post')",
            name="mobile_approval_subject_type",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approved', 'rejected')",
            name="mobile_approval_decision",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # -- 封筒 (subject に依存しない) -----------------------------------------
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 承認対象の内容 identity。C9 では ``proposal_hash``。
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: 中継側のセッション識別子 (UUID)。URL に出るのはこちらで、secret ではない。
    relay_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: capability の sha256。**生の capability は保存しない**。
    capability_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: capability が subject/hash/版/期限に束縛されていることを示す hash。
    capability_binding: Mapped[str] = mapped_column(String(64), nullable=False)

    state: Mapped[str] = mapped_column(String(24), nullable=False, default=MA_PENDING)
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synchronized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: 反映の結果できたローカルの承認/却下レコード。
    change_request_approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("change_request_approvals.id", ondelete="RESTRICT"), nullable=True
    )
    #: 承認依頼メールの送信履歴。
    notification_delivery_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_deliveries.id", ondelete="RESTRICT"), nullable=True
    )

    #: 中継へ渡した sanitized なスナップショット (人が読む用。secret を含まない)。
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    events: Mapped[list[MobileApprovalEvent]] = relationship(  # noqa: F821
        back_populates="session", order_by="MobileApprovalEvent.id"
    )


class MobileApprovalEvent(Base):
    """セッションに起きた事実 1 件 (append-only)。"""

    __tablename__ = "mobile_approval_events"

    __table_args__ = (Index("ix_mobile_approval_events_session", "mobile_approval_session_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    mobile_approval_session_id: Mapped[int] = mapped_column(
        ForeignKey("mobile_approval_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: secret を含まない補足 (件数・理由コードなど)。
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    session: Mapped[MobileApprovalSession] = relationship(back_populates="events")  # noqa: F821
