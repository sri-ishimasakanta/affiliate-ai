"""ArticleReferenceFact — 記事レベルの **参照文献エビデンス** (append-only)。

責務分離:

- :class:`~app.models.article_fact.ArticleFact`
  = **比較対象 (製品・サービス) についての事実**。``subject_ref`` に紐づく。
- :class:`ArticleReferenceFact`
  = **記事そのものが依拠する参照文献の記述**。ガイドライン・標準・官公庁の公表資料など、
  製品ではない一次情報を扱う。

この 2 つを分けるのは、解説記事 (informational) が「製品」ではなく「文書」を根拠に
書かれるためである。参照文献を偽の比較対象や偽の affiliate 案件として登録すると、
readiness / prompt / validator のすべてが誤った前提で動く。

``source_id`` は必須。エビデンスは必ず出典を名指しする (出典の無い記述は保存できない)。
取得日時は :class:`~app.models.source.Source` の ``checked_at`` が持つ。

append-only。``updated_at`` を持たず PATCH / DELETE も無い。内容が変われば新しい行を
append し、読み出しは ``reference_key`` ごとの最新行を採用する (ArticleFact と同じ
"latest wins" semantics)。``statement_hash`` の UNIQUE 制約により、同じ内容の二重登録は
DB 層で防がれる。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
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
    from app.models.source import Source


class ArticleReferenceFact(Base):
    __tablename__ = "article_reference_facts"

    __table_args__ = (
        UniqueConstraint(
            "article_id",
            "reference_key",
            "statement_hash",
            name="uq_article_reference_facts_article_key_statement",
        ),
        Index(
            "ix_article_reference_facts_article_position_id",
            "article_id",
            "position",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 出典は必須。参照されている限り Source は削除できない。
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # この記事の中でエビデンスを識別するキー (例: guideline_version / common_principles)。
    # 製品 fact の FactKey とは別の名前空間で、記事ごとに編集者が決める。
    reference_key: Mapped[str] = mapped_column(String(80), nullable=False)

    # 出典が裏づける簡潔な記述。長文の引き写しではなく、要点を 1 つ書く。
    statement: Mapped[str] = mapped_column(Text, nullable=False)

    # 出典内の位置 (例: 「第2部 C. 共通の指針」)。任意。
    section_label: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 決定的な並び順。同値なら id で解決する。
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # statement の SHA-256 hex。同一内容の二重登録を DB 層で防ぐ。
    statement_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    article: Mapped[Article] = relationship(back_populates="reference_facts")
    source: Mapped[Source] = relationship()
