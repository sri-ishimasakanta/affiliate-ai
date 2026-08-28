from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.article import Article


class Source(Base, TimestampMixin):
    """記事の情報根拠(引用元・参照元)を表すモデル。

    1 つの記事は複数の情報根拠を持つ (Article : Source = 1 : N)。
    """

    __tablename__ = "sources"

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

    # 例) official / news / research / review / competitor
    source_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="reference",
    )

    source_url: Mapped[str | None] = mapped_column(String(1024))

    title: Mapped[str | None] = mapped_column(String(512))

    # 情報根拠として内容を確認した日時
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    article: Mapped[Article] = relationship(
        back_populates="sources",
    )
