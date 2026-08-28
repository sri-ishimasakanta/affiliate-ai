"""Keyword の永続化アクセス。

責務は SQLAlchemy ``Session`` を用いた DB アクセスのみ。
ビジネスルール (重複チェック・status 遷移など) は持たない。
``commit`` は行わず ``flush`` のみ行い、トランザクション境界は Service 側に委ねる。
引数・戻り値はモデル属性名で扱う (Schema との名前対応は Service の責務)。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Keyword


class KeywordRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        keyword: str,
        intent: str | None = None,
        category: str | None = None,
    ) -> Keyword:
        entity = Keyword(keyword=keyword, intent=intent, category=category)
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, keyword_id: int) -> Keyword | None:
        return self._session.get(Keyword, keyword_id)

    def get_by_keyword(self, keyword: str) -> Keyword | None:
        statement = select(Keyword).where(Keyword.keyword == keyword)
        return self._session.scalars(statement).one_or_none()

    def list(self, *, limit: int = 100, offset: int = 0) -> list[Keyword]:
        statement = (
            select(Keyword).order_by(Keyword.id).limit(limit).offset(offset)
        )
        return list(self._session.scalars(statement).all())

    def count(self) -> int:
        return int(
            self._session.scalar(select(func.count()).select_from(Keyword)) or 0
        )

    def list_originality_candidates(
        self, *, exclude_id: int, statuses: Collection[str]
    ) -> list[tuple[int, str]]:
        """originality 比較用に ``(id, keyword)`` だけを取得する (read-only)。

        自身 (``exclude_id``) を除外し、指定 status のみ。全カラムはロードしない。
        """

        statement = (
            select(Keyword.id, Keyword.keyword)
            .where(Keyword.id != exclude_id, Keyword.status.in_(statuses))
            .order_by(Keyword.id)
        )
        return [tuple(row) for row in self._session.execute(statement).all()]

    def update(self, entity: Keyword, values: Mapping[str, Any]) -> Keyword:
        for field, value in values.items():
            setattr(entity, field, value)
        self._session.flush()
        return entity

    def delete(self, entity: Keyword) -> None:
        self._session.delete(entity)
        self._session.flush()
