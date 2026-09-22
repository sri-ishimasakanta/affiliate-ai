"""ArticleReferenceFact の永続化アクセス。

``commit`` は行わず ``flush`` のみ。append-only のため update / delete メソッドを持たない
(内容変更は新しい行の append)。読み出しは ``reference_key`` ごとの最新行 ("latest wins")。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ArticleReferenceFact

# 同じ reference_key の中で「新しい方」を決める順序。
_RECENCY = (ArticleReferenceFact.created_at.desc(), ArticleReferenceFact.id.desc())
# 記事内の決定的な並び順。
_DISPLAY = (ArticleReferenceFact.position.asc(), ArticleReferenceFact.id.asc())


class ArticleReferenceFactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        article_id: int,
        source_id: int,
        reference_key: str,
        statement: str,
        statement_hash: str,
        section_label: str | None,
        position: int,
    ) -> ArticleReferenceFact:
        entity = ArticleReferenceFact(
            article_id=article_id,
            source_id=source_id,
            reference_key=reference_key,
            statement=statement,
            statement_hash=statement_hash,
            section_label=section_label,
            position=position,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    # -- read -----------------------------------------------------
    def list_all_for_article(self, article_id: int) -> list[ArticleReferenceFact]:
        statement = (
            select(ArticleReferenceFact)
            .where(ArticleReferenceFact.article_id == article_id)
            .order_by(*_DISPLAY)
        )
        return list(self._session.scalars(statement).all())

    def get_latest_for_article(self, article_id: int) -> list[ArticleReferenceFact]:
        """``reference_key`` ごとの最新行を position / id 順で返す。"""

        rows = list(
            self._session.scalars(
                select(ArticleReferenceFact)
                .where(ArticleReferenceFact.article_id == article_id)
                .order_by(*_RECENCY)
            ).all()
        )
        latest: dict[str, ArticleReferenceFact] = {}
        for row in rows:
            latest.setdefault(row.reference_key, row)
        return sorted(latest.values(), key=lambda r: (r.position, r.id))

    def find_by_statement_hash(
        self, article_id: int, reference_key: str, statement_hash: str
    ) -> ArticleReferenceFact | None:
        return self._session.scalars(
            select(ArticleReferenceFact).where(
                ArticleReferenceFact.article_id == article_id,
                ArticleReferenceFact.reference_key == reference_key,
                ArticleReferenceFact.statement_hash == statement_hash,
            )
        ).first()
