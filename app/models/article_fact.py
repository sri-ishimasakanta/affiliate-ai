from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.affiliate_program import AffiliateProgram
    from app.models.article import Article
    from app.models.source import Source


class ArticleFact(Base):
    """記事の比較対象 (tool) について公式 Source で確認した事実の **immutable 履歴**。

    - `updated_at` は持たない。事実の「更新」は新しい行の append で表す。
    - 現在値は `(article_id, subject_ref, fact_key)` ごとに
      `checked_at DESC, id DESC` の先頭 (KeywordSignal と同じ latest semantics)。
      `is_current` フラグは持たない (二重管理を避ける)。
    - Article 削除時は ORM/FK cascade で削除される。
    - `value_status` は verified / unknown / not_applicable。**missing (行が無い) と
      unknown は別概念** — unknown は「公式を調査したが確認できなかった」。
    """

    __tablename__ = "article_facts"

    __table_args__ = (
        Index(
            "ix_article_facts_article_subject_key",
            "article_id",
            "subject_ref",
            "fact_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 比較対象 tool の正準識別子 (affiliate candidate 名 or 非 affiliate tool 名)。
    subject_ref: Mapped[str] = mapped_column(String(200), nullable=False)

    # subject が affiliate 対象なら関連付ける。非 affiliate tool は NULL。
    affiliate_program_id: Mapped[int | None] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # app/article/fact_keys.py の FactKey (文字列保存、native ENUM は使わない)。
    fact_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # verified 時のみ非 null。scalar / bool / list[str] を JSON で保持。
    fact_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)

    # verified / unknown / not_applicable
    value_status: Mapped[str] = mapped_column(String(20), nullable=False)

    # unknown / not_applicable の理由 (unknown では必須・非空)。
    unknown_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # この事実を確認した公式 Source。verified / unknown では必須。
    # 参照されている Source の削除は SourceService が 409 で拒否する
    # (DB レベルの RESTRICT には依存しない)。
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id"),
        nullable=True,
        index=True,
    )

    # 事実を読み取った日時 (freshness / latest 判定の基準)。
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    article: Mapped[Article] = relationship(back_populates="facts")
    affiliate_program: Mapped[AffiliateProgram | None] = relationship()
    source: Mapped[Source | None] = relationship()
