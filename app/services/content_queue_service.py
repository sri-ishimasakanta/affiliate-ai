"""ContentQueueService — cluster 定義から read-only な制作キューを組み立てる (transaction なし)。

**DB write 0 / HTTP 0 / LLM 0。** Keyword / 最新 KeywordScore / Signal / non-archived Article /
ArticleFact 件数 / active AffiliateProgram catalog を読むだけで、判定は純粋ロジック
(:mod:`app.article.cluster_plan`) に委ねる。session に pending 変更が生じた場合は flush 前に
例外で止める (read-only guard)。commit / add / delete は一切呼ばない。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.article.cluster_plan import (
    AffiliateMatch,
    ArticleInput,
    ClusterConfig,
    ContentQueue,
    KeywordInput,
    build_content_queue,
)
from app.keyword.affiliate_matching import ProgramFacts, match_programs
from app.keyword.scoring import COMPONENT_NAMES
from app.models import AffiliateProgram, Article, Keyword
from app.models.enums import ArticleStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_fact_repository import ArticleFactRepository
from app.repositories.keyword_score_repository import KeywordScoreRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository

_CATALOG_LIMIT = 1000


class ContentQueueReadOnlyViolationError(RuntimeError):
    """read-only な service の session に pending 変更 (add/update/delete) が生じた。"""


@contextmanager
def read_only_session(session: Session) -> Iterator[None]:
    def _guard(_session, _flush_context, _instances) -> None:
        raise ContentQueueReadOnlyViolationError(
            "ContentQueueService is read-only; the session has pending changes"
        )

    event.listen(session, "before_flush", _guard)
    try:
        yield
    finally:
        event.remove(session, "before_flush", _guard)


def _program_facts(program: AffiliateProgram) -> ProgramFacts:
    return ProgramFacts(
        program_id=program.id,
        name=program.name,
        provider=program.provider,
        category=program.category,
        commission_type=program.commission_type,
        commission_value=program.commission_value,
        currency=program.currency,
        match_terms=tuple(program.match_terms or ()),
    )


class ContentQueueService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._scores = KeywordScoreRepository(session)
        self._signals = KeywordSignalRepository(session)
        self._programs = AffiliateProgramRepository(session)
        self._facts = ArticleFactRepository(session)

    def build(self, config: ClusterConfig) -> ContentQueue:
        """cluster 定義に対する制作キューを返す。未知 keyword は ``ClusterConfigError``。"""

        with read_only_session(self._session):
            keywords, articles = self._load_inputs()
        return build_content_queue(config, keywords, articles)

    def load_inputs(self) -> tuple[list[KeywordInput], list[ArticleInput]]:
        """read-only guard の下で keyword (score / catalog match 付き) と article を読む。"""

        with read_only_session(self._session):
            return self._load_inputs()

    def load_catalog(self) -> list[ProgramFacts]:
        """active な affiliate catalog (URL を含まない安全な項目のみ) を read-only で読む。"""

        with read_only_session(self._session):
            return [_program_facts(p) for p in self._programs.list_active(limit=_CATALOG_LIMIT)]

    # -- reads ----------------------------------------------------------
    def _load_inputs(self) -> tuple[list[KeywordInput], list[ArticleInput]]:
        keyword_rows = list(self._session.scalars(select(Keyword).order_by(Keyword.id)))
        catalog = [_program_facts(p) for p in self._programs.list_active(limit=_CATALOG_LIMIT)]

        keywords: list[KeywordInput] = []
        for row in keyword_rows:
            score = self._scores.get_latest(row.id)
            components: dict[str, float] | None = None
            missing: tuple[str, ...] = ()
            if score is not None:
                components = {name: float(getattr(score, name)) for name in COMPONENT_NAMES}
            else:
                missing = tuple(
                    name
                    for name in COMPONENT_NAMES
                    if self._signals.get_latest(row.id, name) is None
                )
            matches = tuple(
                AffiliateMatch(program_id=m.program_id, name=m.name, provider=m.provider)
                for m in match_programs(row.keyword, catalog)
            )
            keywords.append(
                KeywordInput(
                    id=row.id,
                    keyword=row.keyword,
                    status=str(row.status),
                    opportunity_score=float(score.total_score) if score is not None else None,
                    components=components,
                    missing_components=missing,
                    affiliate_matches=matches,
                )
            )

        text_by_id = {row.id: row.keyword for row in keyword_rows}
        article_rows = self._session.scalars(
            select(Article)
            .where(Article.status != ArticleStatus.ARCHIVED.value)
            .order_by(Article.id)
        )
        articles = [
            ArticleInput(
                id=a.id,
                keyword_id=a.keyword_id,
                keyword=text_by_id.get(a.keyword_id) if a.keyword_id is not None else None,
                title=a.title,
                status=str(a.status),
                fact_count=len(self._facts.get_latest_facts_for_article(a.id)),
            )
            for a in article_rows
        ]
        return keywords, articles
