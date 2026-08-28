from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.keyword import Keyword


class KeywordSignal(Base):
    """component 値の「根拠」となる観測データの履歴 (immutable)。

    - Keyword : KeywordSignal = 1 : N
    - ``updated_at`` は持たない (履歴は原則 immutable)。
    - Keyword 削除時は Signal も削除される (FK ``ondelete=CASCADE`` + ORM cascade)。
    - **正規化ロジックは持たない。** ``normalized_value`` は collector / client が
      算出済みの 0〜100 値を受け取るだけ (Phase 2B-2 以降で provider 別 normalizer)。
    """

    __tablename__ = "keyword_signals"

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

    # KeywordSignalComponent。DB 上は文字列で保存し、native PostgreSQL ENUM は使わない。
    component: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )

    # collector / client が算出済みの 0〜100 正規化値。
    normalized_value: Mapped[float] = mapped_column(Float, nullable=False)

    # 取得元 (例: manual / google_ads / google_trends / dataforseo / asp / serp / ai)。
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    # provider 固有の取得値。SQLite / PostgreSQL 双方で使える generic JSON 型。
    raw_data: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    # URL や provider 側 ID 等の参照情報。
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)

    # provider データの観測日時。
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # 指標が特定期間を表す場合に使用。
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    keyword: Mapped[Keyword] = relationship(back_populates="signals")

    __table_args__ = (
        CheckConstraint(
            "normalized_value BETWEEN 0 AND 100",
            name="normalized_value_range",
        ),
    )
