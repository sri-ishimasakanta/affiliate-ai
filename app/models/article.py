from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import ArticleStatus

if TYPE_CHECKING:
    from app.models.affiliate_program import AffiliateProgram
    from app.models.article_affiliate_program import ArticleAffiliateProgram
    from app.models.article_fact import ArticleFact
    from app.models.article_metric import ArticleMetric
    from app.models.draft_generation_run import DraftGenerationRun
    from app.models.draft_input_snapshot import DraftInputSnapshot
    from app.models.keyword import Keyword
    from app.models.source import Source


class Article(Base, TimestampMixin):
    """生成・管理する記事。"""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    keyword_id: Mapped[int | None] = mapped_column(
        ForeignKey("keywords.id", ondelete="SET NULL"),
    )

    title: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )

    slug: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )

    # 記事本文のドラフト。スキーマ層では ``draft_content`` として公開する。
    body: Mapped[str | None] = mapped_column(Text)

    meta_description: Mapped[str | None] = mapped_column(String(320))

    # 公開後の記事 URL
    published_url: Mapped[str | None] = mapped_column(String(1024))

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ArticleStatus.IDEA,
    )

    # WordPress 連携時に投稿 ID を保持する。連携処理自体はまだ実装しない。
    # スキーマ層では ``wordpress_id`` として公開する。
    wordpress_post_id: Mapped[int | None] = mapped_column(Integer)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    keyword: Mapped[Keyword | None] = relationship(
        back_populates="articles",
    )

    sources: Mapped[list[Source]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
    )

    # 記事の検証済み事実 (immutable 履歴)。Article 削除で全削除。
    facts: Mapped[list[ArticleFact]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
    )

    affiliate_program_links: Mapped[list[ArticleAffiliateProgram]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
    )

    affiliate_programs: Mapped[list[AffiliateProgram]] = relationship(
        secondary="article_affiliate_programs",
        viewonly=True,
    )

    metrics: Mapped[list[ArticleMetric]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
    )

    # draft 生成入力の凍結 artifact (immutable 履歴)。Article 削除で全削除。
    draft_input_snapshots: Mapped[list[DraftInputSnapshot]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
    )

    # draft 生成の実行記録 (lifecycle record)。Article 削除で全削除
    # (snapshot より先に削除される必要があるため cascade を Article 側に持つ)。
    draft_generation_runs: Mapped[list[DraftGenerationRun]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
    )
