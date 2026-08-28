from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.affiliate_program import AffiliateProgram
    from app.models.article import Article


class ArticleAffiliateProgram(Base):
    """Article と AffiliateProgram の多対多を表す中間モデル。

    中間テーブルではなく中間モデルとすることで、``is_primary`` のような
    関連自体の属性を後から追加しやすくしている。
    """

    __tablename__ = "article_affiliate_programs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    affiliate_program_id: Mapped[int] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # その記事における主案件かどうか
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    article: Mapped[Article] = relationship(
        back_populates="affiliate_program_links",
    )

    affiliate_program: Mapped[AffiliateProgram] = relationship(
        back_populates="article_links",
    )

    __table_args__ = (
        UniqueConstraint(
            "article_id",
            "affiliate_program_id",
            name="article_program",
        ),
    )
