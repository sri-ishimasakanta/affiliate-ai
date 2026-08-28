"""ArticleFact (immutable 事実履歴) の永続化アクセス。

``commit`` は行わず ``flush`` のみ。事実の "更新" は新しい行の append で表す
(``update`` メソッドは持たない)。latest 判定は ``checked_at DESC, id DESC``。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.models import ArticleFact

_LATEST_ORDER = (ArticleFact.checked_at.desc(), ArticleFact.id.desc())


def _same_instant(a: datetime, b: datetime) -> bool:
    """SQLite は tz を落とすため、naive は UTC とみなして瞬間を比較する。"""

    aa = a if a.tzinfo is not None else a.replace(tzinfo=UTC)
    bb = b if b.tzinfo is not None else b.replace(tzinfo=UTC)
    return aa == bb


class ArticleFactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        article_id: int,
        subject_ref: str,
        affiliate_program_id: int | None,
        fact_key: str,
        fact_value: object | None,
        value_status: str,
        unknown_reason: str | None,
        source_id: int | None,
        checked_at: datetime,
    ) -> ArticleFact:
        entity = ArticleFact(
            article_id=article_id,
            subject_ref=subject_ref,
            affiliate_program_id=affiliate_program_id,
            fact_key=fact_key,
            fact_value=fact_value,
            value_status=value_status,
            unknown_reason=unknown_reason,
            source_id=source_id,
            checked_at=to_storage_utc(checked_at),
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, fact_id: int) -> ArticleFact | None:
        return self._session.get(ArticleFact, fact_id)

    def list_by_article(
        self,
        article_id: int,
        *,
        subject_ref: str | None = None,
        fact_key: str | None = None,
    ) -> list[ArticleFact]:
        statement = select(ArticleFact).where(ArticleFact.article_id == article_id)
        if subject_ref is not None:
            statement = statement.where(ArticleFact.subject_ref == subject_ref)
        if fact_key is not None:
            statement = statement.where(ArticleFact.fact_key == fact_key)
        statement = statement.order_by(ArticleFact.id)
        return list(self._session.scalars(statement).all())

    def list_by_subject(self, article_id: int, subject_ref: str) -> list[ArticleFact]:
        return self.list_by_article(article_id, subject_ref=subject_ref)

    def list_history(
        self, article_id: int, subject_ref: str, fact_key: str
    ) -> list[ArticleFact]:
        """(article, subject, key) の全履歴を latest 順で返す。"""

        statement = (
            select(ArticleFact)
            .where(
                ArticleFact.article_id == article_id,
                ArticleFact.subject_ref == subject_ref,
                ArticleFact.fact_key == fact_key,
            )
            .order_by(*_LATEST_ORDER)
        )
        return list(self._session.scalars(statement).all())

    def get_latest(
        self, article_id: int, subject_ref: str, fact_key: str
    ) -> ArticleFact | None:
        statement = (
            select(ArticleFact)
            .where(
                ArticleFact.article_id == article_id,
                ArticleFact.subject_ref == subject_ref,
                ArticleFact.fact_key == fact_key,
            )
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(statement).first()

    def get_latest_facts_for_article(self, article_id: int) -> list[ArticleFact]:
        """記事内の全 (subject_ref, fact_key) の現在値 (latest) を返す。"""

        rows = self._session.scalars(
            select(ArticleFact)
            .where(ArticleFact.article_id == article_id)
            .order_by(*_LATEST_ORDER)
        ).all()
        latest: dict[tuple[str, str], ArticleFact] = {}
        for row in rows:
            key = (row.subject_ref, row.fact_key)
            if key not in latest:  # 既に latest 順なので最初が現在値
                latest[key] = row
        return list(latest.values())

    def find_exact(
        self,
        *,
        article_id: int,
        subject_ref: str,
        fact_key: str,
        checked_at: datetime,
        value_status: str,
        fact_value: object | None,
        source_id: int | None,
    ) -> ArticleFact | None:
        """完全一致の既存行 (idempotency: exact duplicate は skip)。"""

        for row in self.list_history(article_id, subject_ref, fact_key):
            if (
                _same_instant(row.checked_at, checked_at)
                and row.value_status == value_status
                and row.fact_value == fact_value
                and row.source_id == source_id
            ):
                return row
        return None
