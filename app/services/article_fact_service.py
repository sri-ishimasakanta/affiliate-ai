"""ArticleFact (immutable 事実履歴) のビジネスロジック。

- Article / AffiliateProgram / Source の存在・所有権チェック
- subject_ref の妥当性 (affiliate_program_id 指定時は name と一致)
- value / status / type の検証 (`app.article.fact_validation` を共有)
- **fact は update しない。** 「更新」は新しい行の append。
- exact duplicate (article, subject, key, checked_at, status, value, source) は skip。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.article.fact_validation import validate_fact
from app.article.schemas import ArticleFactCreate, ArticleFactRead
from app.exceptions import EntityNotFoundError, FactValidationError
from app.keyword.affiliate_matching import normalize_for_match
from app.models import ArticleFact
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_fact_repository import ArticleFactRepository
from app.repositories.article_repository import ArticleRepository
from app.repositories.source_repository import SourceRepository

_ENTITY = "ArticleFact"


class ArticleFactService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._facts = ArticleFactRepository(session)
        self._articles = ArticleRepository(session)
        self._programs = AffiliateProgramRepository(session)
        self._sources = SourceRepository(session)

    # -- read --------------------------------------------------------
    def list_facts(
        self,
        article_id: int,
        *,
        subject_ref: str | None = None,
        fact_key: str | None = None,
        latest: bool = False,
    ) -> list[ArticleFactRead]:
        self._ensure_article(article_id)
        if latest:
            rows = self._facts.get_latest_facts_for_article(article_id)
            if subject_ref is not None:
                rows = [r for r in rows if r.subject_ref == subject_ref]
            if fact_key is not None:
                rows = [r for r in rows if r.fact_key == fact_key]
            rows = sorted(rows, key=lambda r: (r.subject_ref, r.fact_key))
        else:
            rows = self._facts.list_by_article(
                article_id, subject_ref=subject_ref, fact_key=fact_key
            )
        return [self._to_read(r) for r in rows]

    def get_fact(self, article_id: int, fact_id: int) -> ArticleFactRead:
        self._ensure_article(article_id)
        entity = self._facts.get_by_id(fact_id)
        if entity is None or entity.article_id != article_id:
            raise EntityNotFoundError(_ENTITY, fact_id)
        return self._to_read(entity)

    # -- write -----------------------------------------------------
    def create_fact(
        self, article_id: int, payload: ArticleFactCreate
    ) -> ArticleFactRead:
        entity, _created = self._append_validated(article_id, payload)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(entity)
        return self._to_read(entity)

    def append_validated(
        self, article_id: int, payload: ArticleFactCreate
    ) -> tuple[ArticleFact, bool]:
        """検証 + append のみ (commit しない)。bulk import から使う。"""

        return self._append_validated(article_id, payload)

    # -- internal ------------------------------------------------
    def _append_validated(
        self, article_id: int, payload: ArticleFactCreate
    ) -> tuple[ArticleFact, bool]:
        self._ensure_article(article_id)
        subject_ref = payload.subject_ref.strip()
        if not subject_ref:
            raise FactValidationError("subject_ref must be non-blank")

        self._validate_subject(subject_ref, payload.affiliate_program_id)
        source = self._resolve_source(article_id, payload.source_id)

        validated = validate_fact(
            fact_key=payload.fact_key,
            value_status=payload.value_status,
            fact_value=payload.fact_value,
            unknown_reason=payload.unknown_reason,
            source_type=source.source_type if source is not None else None,
            source_present=source is not None,
            checked_at=payload.checked_at,
            now=datetime.now(UTC),
        )

        existing = self._facts.find_exact(
            article_id=article_id,
            subject_ref=subject_ref,
            fact_key=str(validated.fact_key),
            checked_at=payload.checked_at,
            value_status=str(validated.value_status),
            fact_value=validated.fact_value,
            source_id=payload.source_id,
        )
        if existing is not None:
            return existing, False

        entity = self._facts.append(
            article_id=article_id,
            subject_ref=subject_ref,
            affiliate_program_id=payload.affiliate_program_id,
            fact_key=str(validated.fact_key),
            fact_value=validated.fact_value,
            value_status=str(validated.value_status),
            unknown_reason=validated.unknown_reason,
            source_id=payload.source_id,
            checked_at=payload.checked_at,
        )
        return entity, True

    def _validate_subject(
        self, subject_ref: str, affiliate_program_id: int | None
    ) -> None:
        if affiliate_program_id is None:
            # 非 affiliate comparison tool を明示的に許可する (docs 記載)。
            if len(subject_ref) > 200:
                raise FactValidationError("subject_ref too long")
            return
        program = self._programs.get_by_id(affiliate_program_id)
        if program is None:
            raise EntityNotFoundError("AffiliateProgram", affiliate_program_id)
        if normalize_for_match(program.name) != normalize_for_match(subject_ref):
            raise FactValidationError(
                "subject_ref must match the linked AffiliateProgram.name"
            )

    def _resolve_source(self, article_id: int, source_id: int | None):
        if source_id is None:
            return None
        source = self._sources.get_by_id(source_id)
        if source is None:
            raise EntityNotFoundError("Source", source_id)
        if source.article_id != article_id:
            raise FactValidationError(
                "source_id belongs to a different Article"
            )
        return source

    def _ensure_article(self, article_id: int) -> None:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError("Article", article_id)

    @staticmethod
    def _to_read(entity: ArticleFact) -> ArticleFactRead:
        return ArticleFactRead(
            id=entity.id,
            article_id=entity.article_id,
            subject_ref=entity.subject_ref,
            affiliate_program_id=entity.affiliate_program_id,
            fact_key=entity.fact_key,
            fact_value=entity.fact_value,
            value_status=entity.value_status,
            unknown_reason=entity.unknown_reason,
            source_id=entity.source_id,
            checked_at=entity.checked_at,
            created_at=entity.created_at,
        )
