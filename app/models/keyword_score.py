from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.keyword import Keyword
    from app.models.keyword_score_signal import KeywordScoreSignal


def _range_check(column: str) -> CheckConstraint:
    """``column`` が 0〜100 の範囲であることを保証する CHECK 制約。

    ``BETWEEN`` は SQLite / PostgreSQL の双方で自然に動作する。
    """

    return CheckConstraint(f"{column} BETWEEN 0 AND 100", name=f"{column}_range")


class KeywordScore(Base):
    """Opportunity Score の計算履歴。

    - Keyword : KeywordScore = 1 : N
    - 履歴レコードは原則 immutable。``updated_at`` は持たない。
    - Keyword 削除時は履歴も削除される (FK ``ondelete=CASCADE`` + ORM cascade)。
    """

    __tablename__ = "keyword_scores"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    keyword_id: Mapped[int] = mapped_column(
        ForeignKey("keywords.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 各コンポーネント (0〜100)。100 に近いほど良い。
    # competition_ease は「100 に近いほど競合が弱い」向きに統一。
    search_demand: Mapped[float] = mapped_column(Float, nullable=False)
    commercial_intent: Mapped[float] = mapped_column(Float, nullable=False)
    affiliate_opportunity: Mapped[float] = mapped_column(Float, nullable=False)
    competition_ease: Mapped[float] = mapped_column(Float, nullable=False)
    trend: Mapped[float] = mapped_column(Float, nullable=False)
    originality: Mapped[float] = mapped_column(Float, nullable=False)
    site_relevance: Mapped[float] = mapped_column(Float, nullable=False)

    # サーバー側で計算した Opportunity Score (0〜100)。
    total_score: Mapped[float] = mapped_column(Float, nullable=False)

    # スコアバージョン (V1 では "v1")。クライアント入力ではない。
    score_version: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="v1",
    )

    # 入力値の出所。将来 collector / ai / imported 等を保存する単純な文字列。
    input_source: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="manual",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    keyword: Mapped[Keyword] = relationship(back_populates="scores")

    # このスコア計算に使った KeywordSignal への provenance リンク。
    signal_links: Mapped[list[KeywordScoreSignal]] = relationship(
        back_populates="score",
        cascade="all, delete-orphan",
        order_by="KeywordScoreSignal.id",
    )

    __table_args__ = (
        _range_check("search_demand"),
        _range_check("commercial_intent"),
        _range_check("affiliate_opportunity"),
        _range_check("competition_ease"),
        _range_check("trend"),
        _range_check("originality"),
        _range_check("site_relevance"),
        _range_check("total_score"),
    )
