"""ArticleLinkSubstitutionMapping — Human 承認済みの
「canonical な外部リンク出現 (occurrence) 1 つ」 <-> 「AffiliateLinkTarget 1 つ」の binding。

D-D0/D-D0.1 で確定した設計:

- :class:`~app.models.affiliate_link_target.AffiliateLinkTarget` とは別物 —
  こちらは「どのリンク出現をどの target に差し替えるか」という publication 生成時の
  authoring 情報であり、KPI 帰属は引き続き
  ``AffiliateOutboundClick -> AffiliateLinkTarget`` の直接 join で行う (このテーブルは
  KPI 計算には一切関与しない)。
- **狭い一方向 lifecycle のみ**。厳密な append-only ではない
  (:class:`AffiliateLinkTarget` と同じ性質)。identity/payload フィールドは immutable。
  ``status`` は ``active -> superseded`` または ``active -> revoked`` のみ (どちらも
  terminal、逆行しない、superseded <-> revoked の相互遷移もしない)。
- ``occurrence_identity_hash`` は D-D2 で実装される決定的な算出方法 (artifact schema
  version + canonical_body_hash + renderer_version + occurrence ordinal +
  original_href) の **結果を受け取る側** — D-D1 では形 (64 桁 hex) のみを検証する。
- ``original_href`` は ``render_wordpress_html`` が出力した exact 文字列 (正規化・
  trim・大小文字統一・書き換え一切なし)。
- ``updated_at`` を持たない。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

ALSM_ACTIVE = "active"
ALSM_SUPERSEDED = "superseded"
ALSM_REVOKED = "revoked"

ALSM_STATUSES = frozenset({ALSM_ACTIVE, ALSM_SUPERSEDED, ALSM_REVOKED})
ALSM_TERMINAL_STATUSES = frozenset({ALSM_SUPERSEDED, ALSM_REVOKED})

# 許可された status 遷移。汎用ワークフローエンジンは作らない。
ALSM_TRANSITIONS: dict[str, frozenset[str]] = {
    ALSM_ACTIVE: frozenset({ALSM_SUPERSEDED, ALSM_REVOKED}),
    ALSM_SUPERSEDED: frozenset(),
    ALSM_REVOKED: frozenset(),
}


def alsm_transition_allowed(current: str, target: str) -> bool:
    """``current`` から ``target`` への status 遷移が許可されているか (同一 status は不可)。"""

    return target in ALSM_TRANSITIONS.get(current, frozenset())


# 作成後 immutable な identity/payload フィールド。
FROZEN_FIELDS = (
    "article_id",
    "occurrence_identity_hash",
    "original_href",
    "affiliate_link_target_id",
    "approved_at",
    "idempotency_key",
)


class ArticleLinkSubstitutionMapping(Base):
    __tablename__ = "article_link_substitution_mappings"

    __table_args__ = (
        # 同一 (article, occurrence) に active な mapping は 1 つだけ (partial unique)。
        # superseded / revoked の履歴行は共存可。
        Index(
            "uq_article_link_substitution_mappings_active_occurrence",
            "article_id",
            "occurrence_identity_hash",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # D-D2 の決定的アルゴリズムの出力。D-D1 では 64 桁 hex という形のみ検証する。
    occurrence_identity_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    # render_wordpress_html が出力した exact href (無改変)。
    original_href: Mapped[str] = mapped_column(Text, nullable=False)

    affiliate_link_target_id: Mapped[int] = mapped_column(
        ForeignKey("affiliate_link_targets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ALSM_ACTIVE)

    # supersede 時、この mapping を置き換えた新しい mapping を指す (履歴を残す)。
    # revoked では NULL のまま。
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("article_link_substitution_mappings.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # Human 承認のタイムスタンプ。mapping 自体が承認記録なので作成時に確定し、以降不変。
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # updated_at は持たない。
