"""Article の永続化アクセス。

責務は SQLAlchemy ``Session`` を用いた DB アクセスのみ。
ビジネスルール (重複チェック・status 遷移・keyword 存在確認など) は持たない。
``commit`` は行わず ``flush`` のみ行い、トランザクション境界は Service 側に委ねる。
引数・戻り値はモデル属性名で扱う (Schema との名前対応は Service の責務)。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, Keyword


class ArticleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        title: str,
        slug: str,
        keyword_id: int | None = None,
    ) -> Article:
        entity = Article(title=title, slug=slug, keyword_id=keyword_id)
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, article_id: int) -> Article | None:
        return self._session.get(Article, article_id)

    def get_by_slug(self, slug: str) -> Article | None:
        statement = select(Article).where(Article.slug == slug)
        return self._session.scalars(statement).one_or_none()

    def list(self, *, limit: int = 100, offset: int = 0) -> list[Article]:
        statement = (
            select(Article).order_by(Article.id).limit(limit).offset(offset)
        )
        return list(self._session.scalars(statement).all())

    def count(self, *, keyword_id: int | None = None) -> int:
        statement = select(func.count()).select_from(Article)
        if keyword_id is not None:
            statement = statement.where(Article.keyword_id == keyword_id)
        return int(self._session.scalar(statement) or 0)

    def list_by_keyword(self, keyword_id: int) -> list[Article]:
        statement = (
            select(Article)
            .where(Article.keyword_id == keyword_id)
            .order_by(Article.id)
        )
        return list(self._session.scalars(statement).all())

    def list_originality_candidates(
        self, *, exclude_keyword_id: int, statuses: Collection[str]
    ) -> list[tuple[int, int | None, str | None, str]]:
        """originality 比較用に ``(article_id, keyword_id, linked_keyword_text, title)``。

        指定 status のみ。current keyword に紐づく Article は除外。
        紐づく Keyword.keyword は 1 回の JOIN で取得し N+1 を避ける。本文は取得しない。
        """

        statement = (
            select(Article.id, Article.keyword_id, Keyword.keyword, Article.title)
            .select_from(Article)
            .outerjoin(Keyword, Article.keyword_id == Keyword.id)
            .where(
                Article.status.in_(statuses),
                (Article.keyword_id.is_(None))
                | (Article.keyword_id != exclude_keyword_id),
            )
            .order_by(Article.id)
        )
        return [tuple(row) for row in self._session.execute(statement).all()]

    def update(self, entity: Article, values: Mapping[str, Any]) -> Article:
        for field, value in values.items():
            setattr(entity, field, value)
        self._session.flush()
        return entity

    def delete(self, entity: Article) -> None:
        self._session.delete(entity)
        self._session.flush()
