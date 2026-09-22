"""ArticleReferenceFact の作成・読み出し (transaction owner)。

参照文献エビデンスは記事に紐づく。affiliate 案件にも比較対象にも依存しない。
出典 (Source) は必須で、その Source が同じ記事のものであることを強制する
(別記事の出典を借りてくることはできない)。
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.exceptions import EntityNotFoundError, FactValidationError
from app.models import ArticleReferenceFact
from app.repositories.article_reference_fact_repository import (
    ArticleReferenceFactRepository,
)
from app.repositories.article_repository import ArticleRepository
from app.repositories.source_repository import SourceRepository

_ARTICLE = "Article"
_SOURCE = "Source"
_MAX_STATEMENT = 2000


class ArticleReferenceFactService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._sources = SourceRepository(session)
        self._repo = ArticleReferenceFactRepository(session)

    def create(
        self,
        article_id: int,
        *,
        source_id: int,
        reference_key: str,
        statement: str,
        section_label: str | None = None,
        position: int = 0,
    ) -> ArticleReferenceFact:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)

        source = self._sources.get_by_id(source_id)
        if source is None:
            raise EntityNotFoundError(_SOURCE, source_id)
        if source.article_id != article_id:
            raise FactValidationError(
                f"source {source_id} belongs to article {source.article_id}, "
                f"not {article_id}"
            )

        key = (reference_key or "").strip()
        if not key:
            raise FactValidationError("reference_key must not be blank")
        text = (statement or "").strip()
        if not text:
            raise FactValidationError("statement must not be blank")
        if len(text) > _MAX_STATEMENT:
            raise FactValidationError(
                f"statement is too long ({len(text)} > {_MAX_STATEMENT}); "
                "store a concise supported point, not the whole document"
            )

        statement_hash = compute_text_hash(text)
        existing = self._repo.find_by_statement_hash(article_id, key, statement_hash)
        if existing is not None:
            return existing  # 同一内容の再登録は no-op

        try:
            entity = self._repo.add(
                article_id=article_id,
                source_id=source_id,
                reference_key=key,
                statement=text,
                statement_hash=statement_hash,
                section_label=(section_label or None),
                position=position,
            )
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self._repo.find_by_statement_hash(article_id, key, statement_hash)
            if existing is not None:
                return existing
            raise
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(entity)
        return entity

    # -- read -------------------------------------------------------
    def list_for_article(self, article_id: int) -> list[ArticleReferenceFact]:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        return self._repo.get_latest_for_article(article_id)
