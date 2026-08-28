from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.keyword_score import KeywordScore
    from app.models.keyword_signal import KeywordSignal


class KeywordScoreSignal(Base):
    """KeywordScore がどの KeywordSignal を使ったかを記録する provenance (association)。

    将来的に「複数 Signal から 1 component を算出する」可能性があるため、
    単純な 1:1 FK ではなく association 方式にしている。
    同じ (score, signal) の組み合わせは重複登録できない。
    余計なカラムはまだ持たせない (``created_at`` のみ、``updated_at`` なし)。
    """

    __tablename__ = "keyword_score_signals"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    keyword_score_id: Mapped[int] = mapped_column(
        ForeignKey("keyword_scores.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    keyword_signal_id: Mapped[int] = mapped_column(
        ForeignKey("keyword_signals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    score: Mapped[KeywordScore] = relationship(back_populates="signal_links")
    signal: Mapped[KeywordSignal] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "keyword_score_id",
            "keyword_signal_id",
            name="score_signal",
        ),
    )
