"""ArticleContentSubject の永続化アクセス (C3)。

責務は SQLAlchemy ``Session`` を用いた DB アクセスのみ。``commit`` は行わず ``flush`` のみで、
トランザクション境界は Service に委ねる。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ArticleContentSubject


class ArticleContentSubjectRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        article_id: int,
        subject_key: str,
        display_name: str,
        subject_source: str,
        position: int,
        affiliate_program_id: int | None = None,
    ) -> ArticleContentSubject:
        entity = ArticleContentSubject(
            article_id=article_id,
            subject_key=subject_key,
            display_name=display_name,
            subject_source=subject_source,
            position=position,
            affiliate_program_id=affiliate_program_id,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def list_by_article(self, article_id: int) -> list[ArticleContentSubject]:
        """position 昇順 (同順位は id) の決定論的な順序で返す。"""

        statement = (
            select(ArticleContentSubject)
            .where(ArticleContentSubject.article_id == article_id)
            .order_by(ArticleContentSubject.position, ArticleContentSubject.id)
        )
        return list(self._session.scalars(statement).all())

    def delete(self, entity: ArticleContentSubject) -> None:
        self._session.delete(entity)
        self._session.flush()
