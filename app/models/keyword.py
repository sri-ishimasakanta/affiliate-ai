from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import KeywordStatus

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.keyword_score import KeywordScore
    from app.models.keyword_signal import KeywordSignal


class Keyword(Base, TimestampMixin):
    """記事作成の対象となる検索キーワード。"""

    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    keyword: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )

    search_volume: Mapped[int | None] = mapped_column(Integer)

    # SEO 難易度 (0.0-100.0 を想定)
    difficulty: Mapped[float | None] = mapped_column(Float)

    # クリック単価
    cpc: Mapped[float | None] = mapped_column(Float)

    # 検索意図: informational / commercial / transactional / navigational
    # スキーマ層 (app/keyword/schemas.py) では ``search_intent`` として公開する。
    intent: Mapped[str | None] = mapped_column(String(50))

    # コンテンツ上の分類 (任意のラベル)
    category: Mapped[str | None] = mapped_column(String(100))

    # 最新 Opportunity Score のキャッシュ値。
    # 履歴の正本は keyword_scores テーブル。両者はスコア作成時に
    # 同一 Service トランザクション内で更新する (docs/architecture.md 参照)。
    opportunity_score: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=KeywordStatus.DISCOVERED,
    )

    articles: Mapped[list[Article]] = relationship(
        back_populates="keyword",
    )

    scores: Mapped[list[KeywordScore]] = relationship(
        back_populates="keyword",
        cascade="all, delete-orphan",
        order_by="KeywordScore.id",
    )

    signals: Mapped[list[KeywordSignal]] = relationship(
        back_populates="keyword",
        cascade="all, delete-orphan",
        order_by="KeywordSignal.id",
    )
