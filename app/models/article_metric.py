from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.article import Article


class ArticleMetric(Base, TimestampMixin):
    """記事ごと・日付ごとの成果指標。

    将来的に Search Console / GA4 から取り込むが、取り込み処理はまだ実装しない。
    ここでは保存先のスキーマのみを定義する。
    """

    __tablename__ = "article_metrics"

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

    metric_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )

    # 指標の取得元: search_console / ga4 / manual
    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="manual",
    )

    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_position: Mapped[float | None] = mapped_column(Float)
    sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conversions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revenue: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    article: Mapped[Article] = relationship(
        back_populates="metrics",
    )

    # --- 派生値 ---------------------------------------------------------------
    # CTR / CVR は impressions・clicks・conversions から一意に決まる。
    # DB に保存すると不整合の原因になるため、算出のみ行う。

    @property
    def ctr(self) -> float | None:
        """クリック率 (clicks / impressions)。impressions が 0 なら None。"""

        if not self.impressions:
            return None

        return self.clicks / self.impressions

    @property
    def conversion_rate(self) -> float | None:
        """コンバージョン率 (conversions / clicks)。clicks が 0 なら None。"""

        if not self.clicks:
            return None

        return self.conversions / self.clicks

    __table_args__ = (
        UniqueConstraint(
            "article_id",
            "metric_date",
            "provider",
            name="article_date_provider",
        ),
    )
