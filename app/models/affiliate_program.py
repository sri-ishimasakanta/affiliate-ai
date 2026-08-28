from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import AffiliateProgramStatus

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.article_affiliate_program import ArticleAffiliateProgram


class AffiliateProgram(Base, TimestampMixin):
    """紹介対象となるアフィリエイトプログラム(広告案件)。"""

    __tablename__ = "affiliate_programs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # ASP 名など (例: a8 / moshimo / amazon / rakuten)。API 連携は行わない。
    provider: Mapped[str | None] = mapped_column(String(100))

    category: Mapped[str | None] = mapped_column(String(100))

    # fixed / percentage (自由文字列。新規入力では fixed / percentage を推奨)
    commission_type: Mapped[str | None] = mapped_column(String(50))

    commission_value: Mapped[float | None] = mapped_column(Float)

    # commission_value の通貨 (ISO 4217、3 文字大文字)。V1 運用は原則 JPY。
    # 既存レコード互換のため nullable。DB では JPY 固定にしない。
    currency: Mapped[str | None] = mapped_column(String(3))

    landing_page_url: Mapped[str | None] = mapped_column(String(1024))

    tracking_url: Mapped[str | None] = mapped_column(String(1024))

    notes: Mapped[str | None] = mapped_column(Text)

    # keyword と案件を関連付けるための明示的な検索語群 (JSON 配列)。
    # Signal 採点 (Phase 2B-6B) で利用する。URL / secret を入れる用途ではない。
    # 更新時は Service が list 全体を assign する (MutableList は使わない)。
    match_terms: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AffiliateProgramStatus.ACTIVE,
    )

    article_links: Mapped[list[ArticleAffiliateProgram]] = relationship(
        back_populates="affiliate_program",
        cascade="all, delete-orphan",
    )

    articles: Mapped[list[Article]] = relationship(
        secondary="article_affiliate_programs",
        viewonly=True,
    )
