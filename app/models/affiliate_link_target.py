"""AffiliateLinkTarget — control-plane が承認した
「記事 × 案件 × 不変 destination × opaque token」の binding。

- 公開 redirect runtime (WordPress/XServer) は本テーブルの **projection** だけを受け取る。
  ここに click event / IP / User-Agent / Referer / cookie / session は一切持たない。
- ``destination_url`` は Human 入力 (:attr:`AffiliateProgram.tracking_url`) の exact 文字列。
  canonical 化・書き換えをしない。``destination_host`` はセキュリティ判定用の正規化派生値。
- ``destination_url`` を変えるときは in-place update せず **supersede**
  (old: active -> superseded / disabled_at set / superseded_by_id set、new: 新 token で active)。
- ``updated_at`` を持たない。狭い lifecycle 遷移のみ。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import AffiliateLinkTargetStatus

ALT_ACTIVE = AffiliateLinkTargetStatus.ACTIVE.value
ALT_DISABLED = AffiliateLinkTargetStatus.DISABLED.value
ALT_SUPERSEDED = AffiliateLinkTargetStatus.SUPERSEDED.value

ALT_STATUSES = frozenset({ALT_ACTIVE, ALT_DISABLED, ALT_SUPERSEDED})
ALT_TERMINAL_STATUSES = frozenset({ALT_DISABLED, ALT_SUPERSEDED})

# 許可された status 遷移。汎用ワークフローエンジンは作らない。
ALT_TRANSITIONS: dict[str, frozenset[str]] = {
    ALT_ACTIVE: frozenset({ALT_DISABLED, ALT_SUPERSEDED}),
    ALT_DISABLED: frozenset(),
    ALT_SUPERSEDED: frozenset(),
}


def alt_transition_allowed(current: str, target: str) -> bool:
    """``current`` から ``target`` への status 遷移が許可されているか (同一 status は不可)。"""

    return target in ALT_TRANSITIONS.get(current, frozenset())


# 作成後 immutable な identity フィールド。
FROZEN_FIELDS = (
    "token",
    "article_id",
    "affiliate_program_id",
    "destination_url",
    "destination_host",
    "link_identity_hash",
    "idempotency_key",
)


class AffiliateLinkTarget(Base):
    __tablename__ = "affiliate_link_targets"

    __table_args__ = (
        # 同一 (article, program) に active な target は 1 つだけ (partial unique)。
        # disabled / superseded の履歴行は共存可。
        Index(
            "uq_affiliate_link_targets_active_article_program",
            "article_id",
            "affiliate_program_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # /go/{token} の routing 識別子。opaque・非連番・非意味的。
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    affiliate_program_id: Mapped[int] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Human 入力の exact affiliate URL。書き換えない。
    destination_url: Mapped[str] = mapped_column(Text, nullable=False)
    # セキュリティ判定用の正規化ホスト名 (IDNA-ascii / lowercase)。
    destination_host: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ALT_ACTIVE)

    # destination 変更時、この target を置き換えた新 target を指す (履歴を残す)。
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("affiliate_link_targets.id", ondelete="RESTRICT"), nullable=True
    )

    # article + program + exact destination_url の binding。
    link_identity_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # updated_at は持たない。
