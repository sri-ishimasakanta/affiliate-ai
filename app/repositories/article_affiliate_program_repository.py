"""ArticleAffiliateProgram (記事 × 広告案件の中間モデル) の永続化アクセス。

責務は SQLAlchemy ``Session`` を用いた DB アクセスのみ。
``commit`` は行わず ``flush`` のみ。トランザクション境界は Service に委ねる。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ArticleAffiliateProgram


class ArticleAffiliateProgramRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        article_id: int,
        affiliate_program_id: int,
        is_primary: bool = False,
    ) -> ArticleAffiliateProgram:
        entity = ArticleAffiliateProgram(
            article_id=article_id,
            affiliate_program_id=affiliate_program_id,
            is_primary=is_primary,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, link_id: int) -> ArticleAffiliateProgram | None:
        return self._session.get(ArticleAffiliateProgram, link_id)

    def list_by_article(self, article_id: int) -> list[ArticleAffiliateProgram]:
        statement = (
            select(ArticleAffiliateProgram)
            .where(ArticleAffiliateProgram.article_id == article_id)
            .order_by(
                ArticleAffiliateProgram.is_primary.desc(),
                ArticleAffiliateProgram.id,
            )
        )
        return list(self._session.scalars(statement).all())

    def get_primary(self, article_id: int) -> ArticleAffiliateProgram | None:
        statement = select(ArticleAffiliateProgram).where(
            ArticleAffiliateProgram.article_id == article_id,
            ArticleAffiliateProgram.is_primary.is_(True),
        )
        return self._session.scalars(statement).first()

    def get_by_article_and_program(
        self, article_id: int, affiliate_program_id: int
    ) -> ArticleAffiliateProgram | None:
        statement = select(ArticleAffiliateProgram).where(
            ArticleAffiliateProgram.article_id == article_id,
            ArticleAffiliateProgram.affiliate_program_id == affiliate_program_id,
        )
        return self._session.scalars(statement).first()

    def update(
        self, entity: ArticleAffiliateProgram, values: Mapping[str, Any]
    ) -> ArticleAffiliateProgram:
        for field, value in values.items():
            setattr(entity, field, value)
        self._session.flush()
        return entity

    def delete(self, entity: ArticleAffiliateProgram) -> None:
        self._session.delete(entity)
        self._session.flush()
