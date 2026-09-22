"""ArticleEditorialRevision — 既に promote 済みの Article 本文を改稿した **immutable な記録**。

責務分離:

- :class:`~app.models.article_draft_promotion.ArticleDraftPromotion`
  = 生成 run から Article へ **初めて** 本文を採用した記録 (first-promotion only)。
- :class:`ArticleEditorialRevision`
  = 採用後の Article 本文に対する編集上の改訂 (内部リンク追加・事実修正など)。

first-promotion semantics は一切弱めない。promotion が無い Article を revise する
ことはできず、revision は常に「現在の canonical 本文」を起点に append される。

append-only。``updated_at`` を持たず PATCH / DELETE も無い。内容が変われば新しい行を
append する (Source / ArticleFact / DraftInputSnapshot / ArticleDraftPromotion と
同じ immutable history semantics)。

``previous_body_hash`` / ``previous_meta_hash`` は改訂直前の canonical 値であり、
promotion から現在までの改訂列を後から辿れるようにする (provenance chain)。

``article_status_at_revision`` は改訂時点の Article.status。``published`` の記事を
改訂する場合は ``published_update_intent`` を必須にし、「公開済みと知った上で更新する」
という明示的な意図を記録する (WordPress 側との同期が必要になるため)。
"""

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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.article_draft_promotion import ArticleDraftPromotion


class ArticleEditorialRevision(Base):
    __tablename__ = "article_editorial_revisions"

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_article_editorial_revisions_idempotency_key",
        ),
        UniqueConstraint(
            "article_id",
            "revision_content_hash",
            name="uq_article_editorial_revisions_article_content",
        ),
        Index(
            "ix_article_editorial_revisions_article_created_id",
            "article_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # provenance の根。この Article 自身の promotion のみを参照できる
    # (service が article_id 一致を強制するため、cross-article revision は作れない)。
    base_promotion_id: Mapped[int] = mapped_column(
        ForeignKey("article_draft_promotions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # 改訂理由 (人間が読む監査情報)。空文字は service が拒否する。
    revision_reason: Mapped[str] = mapped_column(Text, nullable=False)

    # 改訂時点の Article.status と、published 記事に必須の明示的更新意図。
    article_status_at_revision: Mapped[str] = mapped_column(String(20), nullable=False)
    published_update_intent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 改訂直前の canonical 値 (drift guard に使った期待値そのもの)。
    previous_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_meta_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # 改訂後の exact な本文 / meta。
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    meta_description: Mapped[str] = mapped_column(Text, nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    meta_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # (article_id, body, meta) の canonical identity。同一内容の再適用を no-op にする。
    revision_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    editor_notes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    revised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    article: Mapped[Article] = relationship(back_populates="editorial_revisions")
    base_promotion: Mapped[ArticleDraftPromotion] = relationship()
