"""AffiliateOutboundClick — WordPress ``bfl_outbound_clicks`` の **provider-faithful な
imported replica** (append-only)。

- WordPress が source of truth。ここはローカルの分析用コピー。ack / delete / mutation は
  WordPress へ返さない。
- ``source_click_id`` (WordPress の raw click id) が不変の source identity。同一 id を
  再取得しても上書きしない (同一なら no-op、内容が違えば source drift エラー)。
- ``token`` は export が返した値をそのまま保持する。attribution
  (token -> AffiliateLinkTarget -> article / program) は将来の集計層の責務。
  **AffiliateLinkTarget への FK は張らない** — local control-plane が一時的に
  不整合でも click を faithfully に取り込めるようにするため。
- IP / hashed IP / User-Agent / Referer / cookie / session / user id / email /
  device / browser query / destination_url / article_id / affiliate_program_id /
  source_event_hash は **持たない**。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AffiliateOutboundClick(Base):
    __tablename__ = "affiliate_outbound_clicks"

    __table_args__ = (
        UniqueConstraint(
            "source_click_id",
            name="uq_affiliate_outbound_clicks_source_click_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # WordPress bfl_outbound_clicks.id。不変の source identity。
    source_click_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )

    # export が返した token を無改変で保持 ([A-Za-z0-9_-]{16,64})。
    token: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # WordPress の gmdate('Y-m-d H:i:s') を UTC として parse し、
    # to_storage_utc で naive UTC wall-clock として保存する。
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # この行を **最初に** 取り込んだ run (不変 provenance。再取り込みで更新しない)。
    source_import_run_id: Mapped[int] = mapped_column(
        ForeignKey("affiliate_click_import_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # updated_at は持たない (append-only replica)。
