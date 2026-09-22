"""ArticleContentSubject — 記事が比較 / 調査する **編集上の対象** (C3)。

「この記事が比較・調査する対象」と「どの affiliate program に link しているか」を分ける。
C2 までは比較対象 = link した ``AffiliateProgram`` だったため、catalog に案件が無い
supporting の比較記事 (RPA おすすめ 等) が構造的に書けなかった。

- ``subject_key``    : 編集カタログ (``app/config/content_subjects.json``) 上の安定キー
- ``display_name``   : 本文・fact の ``subject_ref`` に使う表示名 (承認時点の値を固定する)
- ``affiliate_program_id``: affiliate 案件に裏付けられた対象のときだけ設定 (nullable)
- ``subject_source`` : ``affiliate_catalog`` / ``editorial``

承認時に **その時点の選択を行として固定** するので、あとから config を変えても
承認済み記事の比較対象は動かない (deterministic drafting)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.affiliate_program import AffiliateProgram
    from app.models.article import Article

#: subject_source の値
SUBJECT_SOURCE_AFFILIATE = "affiliate_catalog"
SUBJECT_SOURCE_EDITORIAL = "editorial"
SUBJECT_SOURCES = (SUBJECT_SOURCE_AFFILIATE, SUBJECT_SOURCE_EDITORIAL)


class ArticleContentSubject(Base, TimestampMixin):
    """記事 × 比較対象 (affiliate とは独立)。"""

    __tablename__ = "article_content_subjects"
    __table_args__ = (
        UniqueConstraint(
            "article_id", "subject_key", name="uq_article_content_subjects_article_subject"
        ),
        UniqueConstraint(
            "article_id", "display_name", name="uq_article_content_subjects_article_name"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: 編集カタログ上の安定キー (config が消えても行は残る)
    subject_key: Mapped[str] = mapped_column(String(100), nullable=False)

    #: fact の subject_ref と一致させる表示名 (承認時点で固定)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)

    #: affiliate 案件に裏付けられた対象のときだけ設定する
    affiliate_program_id: Mapped[int | None] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="RESTRICT")
    )

    subject_source: Mapped[str] = mapped_column(String(32), nullable=False)

    #: 本文・比較表での並び順 (決定論のため明示する)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    article: Mapped[Article] = relationship(back_populates="content_subjects")
    affiliate_program: Mapped[AffiliateProgram | None] = relationship()
