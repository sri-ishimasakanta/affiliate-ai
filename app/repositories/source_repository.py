"""Source (公式ページの観測記録) の永続化アクセス。

責務は SQLAlchemy ``Session`` を用いた DB アクセスのみ。``commit`` は行わず
``flush`` のみ。Source は原則 immutable のため update メソッドは持たない。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.models import ArticleFact, Source


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        article_id: int,
        source_type: str,
        source_url: str,
        title: str | None,
        checked_at: datetime,
    ) -> Source:
        entity = Source(
            article_id=article_id,
            source_type=source_type,
            source_url=source_url,
            title=title,
            checked_at=to_storage_utc(checked_at),
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, source_id: int) -> Source | None:
        return self._session.get(Source, source_id)

    def list_by_article(self, article_id: int) -> list[Source]:
        statement = (
            select(Source)
            .where(Source.article_id == article_id)
            .order_by(Source.id)
        )
        return list(self._session.scalars(statement).all())

    def count_by_article(self, article_id: int) -> int:
        statement = (
            select(func.count()).select_from(Source).where(Source.article_id == article_id)
        )
        return int(self._session.scalar(statement) or 0)

    def find_observation(
        self, *, article_id: int, source_url: str, checked_at: datetime
    ) -> Source | None:
        """同一 (article, URL, checked_at) の観測記録 (CLI idempotency 用)。"""

        statement = select(Source).where(
            Source.article_id == article_id,
            Source.source_url == source_url,
            Source.checked_at == to_storage_utc(checked_at),
        )
        return self._session.scalars(statement).first()

    def is_referenced_by_fact(self, source_id: int) -> bool:
        statement = (
            select(func.count())
            .select_from(ArticleFact)
            .where(ArticleFact.source_id == source_id)
        )
        return int(self._session.scalar(statement) or 0) > 0

    def delete(self, entity: Source) -> None:
        self._session.delete(entity)
        self._session.flush()
