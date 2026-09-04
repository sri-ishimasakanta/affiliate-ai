"""ArticleDraftPromotion — Human が承認して Article draft へ採用した **immutable な採用記録**。

責務分離 (§2):

- :class:`~app.models.draft_generation_run.DraftGenerationRun`
  = モデルが「生成したもの」(parsed_body / parsed_meta_description は変更不可)。
- :class:`ArticleDraftPromotion`
  = Human が「承認して採用したもの」(生成物と 1:1 とは限らない — Human 修正版を採用しうる)。
- :class:`~app.models.article.Article`
  = 現在選択されている記事ドラフト本文。

append-only。``updated_at`` を持たず、PATCH / DELETE も無い。内容が変われば新しい行を
append する (Source / ArticleFact / DraftInputSnapshot と同じ immutable history semantics)。
生成 run 側は一切変更しない (terminal semantics を壊さない)。
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
    from app.models.draft_generation_run import DraftGenerationRun


class ArticleDraftPromotion(Base):
    __tablename__ = "article_draft_promotions"

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_article_draft_promotions_idempotency_key",
        ),
        Index(
            "ix_article_draft_promotions_article_created_id",
            "article_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 採用先の Article。Article 削除で採用記録も消える。
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 由来の生成 run。run が参照される限り run は削除不能 (defense-in-depth)。
    # run 自体の削除は Article 削除経由の cascade が担当するため、ここでは cascade を張らない。
    source_run_id: Mapped[int] = mapped_column(
        ForeignKey("draft_generation_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # 採用時点で確認した source run の frozen prompt 束縛 (監査用のコピー)。
    source_prompt_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_rendered_prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Human が承認した exact な本文 / meta (生成物と同一とは限らない)。
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    meta_description: Mapped[str] = mapped_column(String(400), nullable=False)

    # exact 文字列そのものの SHA-256 hex。
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    meta_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # 意味的入力 (article_id + source_run_id + body + meta) の canonical JSON の SHA-256。
    # timestamps / notes / idempotency_key / validation_report は含めない。
    candidate_content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )

    # 採用候補に対して再実行した editorial validator の結果 (hash 対象外)。
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Human のレビューメモ (任意, hash 対象外)。
    human_review_notes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Human が採用操作を行った UTC instant (hash 対象外)。
    promoted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # updated_at は持たない (immutable)。

    article: Mapped[Article] = relationship(back_populates="draft_promotions")
    source_run: Mapped[DraftGenerationRun] = relationship(
        back_populates="draft_promotions"
    )
