"""Article Planning V1 のオーケストレーション Service。

- :meth:`plan_for_keyword` は **read-only / 決定論**。Keyword + 最新 7 Signal +
  live Affiliate Catalog + originality provenance から :class:`ArticlePlanDTO` を毎回
  生成する。DB へは書かない。
- :meth:`approve` は **atomic**。1 transaction で「plan 再生成 → validation →
  Article 作成 → idea→planned → affiliate links 作成 → primary 設定 → commit」を行い、
  途中失敗は全て rollback する (partial state を作らない)。
- LLM / 外部 API を呼ばない。primary affiliate を自動確定しない。
- ``scoring.py`` / 7 Signal / Keyword workflow は変更しない。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.article import planning
from app.article.planning import ArticleType
from app.article.schemas import (
    AffiliateCandidateRead,
    ArticleAffiliateProgramRead,
    ArticlePlanApproveRequest,
    ArticlePlanDTO,
    ArticleRead,
    CannibalizationInfo,
    ComparisonAxisRead,
    PlanReadiness,
    PlanSectionRead,
)
from app.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    PlanApprovalError,
)
from app.keyword.affiliate_matching import ProgramFacts, match_programs
from app.keyword.scoring import COMPONENT_NAMES
from app.models import AffiliateProgram, Article, Keyword
from app.models.enums import ArticleStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_affiliate_program_repository import (
    ArticleAffiliateProgramRepository,
)
from app.repositories.article_repository import ArticleRepository
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.status_transitions import ARTICLE_TRANSITIONS, ensure_transition_allowed

_KEYWORD = "Keyword"
_ARTICLE = "Article"
_ACTIVE_CATALOG_LIMIT = 100_000
# 非 archived とみなす Article status (同一 keyword への二重企画を拒否する対象)。
_LIVE_ARTICLE_STATUSES = frozenset(
    s for s in ArticleStatus if s is not ArticleStatus.ARCHIVED
)


@dataclass(frozen=True)
class _Candidate:
    read: AffiliateCandidateRead
    program_id: int


class ArticlePlanService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._keywords = KeywordRepository(session)
        self._signals = KeywordSignalRepository(session)
        self._programs = AffiliateProgramRepository(session)
        self._articles = ArticleRepository(session)
        self._links = ArticleAffiliateProgramRepository(session)

    # -- read-only plan ------------------------------------------------
    def plan_for_keyword(self, keyword_id: int) -> ArticlePlanDTO:
        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError(_KEYWORD, keyword_id)

        readiness = self._readiness(keyword)
        type_result = planning.classify_article_type(keyword.keyword)
        article_type = type_result.article_type

        candidates, live_ids = self._affiliate_candidates(keyword.keyword)
        snapshot_available, snapshot_ids = self._snapshot_program_ids(keyword_id)
        # snapshot 情報が無い (Signal 不在 / matched_program_ids キー欠落) 場合は
        # drift 判定不可 -> false 扱い。明示的な空配列 [] は "0 件マッチだった" として
        # 有効な snapshot なので live との比較対象にする。
        catalog_drift = (
            snapshot_available and sorted(snapshot_ids) != sorted(live_ids)
        )

        cannibalization = self._cannibalization(keyword, article_type)

        proposed_slug = planning.suggest_slug(
            keyword.keyword, article_type, is_taken=self._slug_taken
        )
        slug_available = self._articles.get_by_slug(proposed_slug) is None

        primary_goal, secondary_goals = planning.goals(article_type)
        warnings = self._warnings(
            article_type=article_type,
            readiness=readiness,
            catalog_drift=catalog_drift,
            snapshot_available=snapshot_available,
            cannibalization=cannibalization,
            proposed_slug=proposed_slug,
            slug_available=slug_available,
            keyword=keyword.keyword,
        )

        return ArticlePlanDTO(
            keyword_id=keyword.id,
            keyword=keyword.keyword,
            readiness=readiness,
            working_title=planning.working_title(keyword.keyword, article_type),
            proposed_slug=proposed_slug,
            slug_available=slug_available,
            article_type=article_type,
            target_reader=planning.target_reader(article_type),
            search_intent_summary=planning.search_intent_summary(
                keyword.keyword, article_type
            ),
            primary_goal=primary_goal,
            secondary_goals=list(secondary_goals),
            outline=[
                PlanSectionRead(
                    level=s.level,
                    heading=s.heading,
                    purpose=s.purpose,
                    required_elements=list(s.required_elements),
                )
                for s in planning.build_outline(keyword.keyword, article_type)
            ],
            comparison_axes=[
                ComparisonAxisRead(axis=axis, data_availability=avail)
                for axis, avail in planning.comparison_axes()
            ],
            affiliate_candidates=[c.read for c in candidates],
            catalog_drift=catalog_drift,
            catalog_snapshot_available=snapshot_available,
            snapshot_program_ids=list(snapshot_ids),
            live_program_ids=list(live_ids),
            cta_strategy=planning.cta_strategy(article_type),
            cannibalization=cannibalization,
            compliance_checklist=list(planning.COMPLIANCE_CHECKLIST),
            quality_guardrails=list(planning.QUALITY_GUARDRAILS),
            source_requirements=list(planning.source_requirements(article_type)),
            warnings=warnings,
            notes=None,
        )

    # -- atomic approval --------------------------------------------
    def approve(
        self, keyword_id: int, payload: ArticlePlanApproveRequest
    ) -> ArticleRead:
        # 1) current plan を再生成 (read-only)
        plan = self.plan_for_keyword(keyword_id)  # keyword 不在なら 404

        # 2) validation (書き込み前に全て済ませる)
        self._validate_incomplete(plan, payload)
        self._validate_cannibalization(plan, payload)
        self._validate_no_live_article(keyword_id)
        self._validate_slug(payload.slug)
        primary_id, secondary_ids = self._validate_affiliates(plan, payload)

        # 3) writes (単一 transaction)
        try:
            article = self._articles.create(
                title=payload.title,
                slug=payload.slug,
                keyword_id=keyword_id,
            )
            ensure_transition_allowed(
                _ARTICLE,
                ArticleStatus(article.status),
                ArticleStatus.PLANNED,
                ARTICLE_TRANSITIONS,
            )
            self._articles.update(article, {"status": ArticleStatus.PLANNED})
            if primary_id is not None:
                self._links.create(
                    article_id=article.id,
                    affiliate_program_id=primary_id,
                    is_primary=True,
                )
            for program_id in secondary_ids:
                self._links.create(
                    article_id=article.id,
                    affiliate_program_id=program_id,
                    is_primary=False,
                )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(article)
        return self._article_read(article)

    def links_for_article(self, article_id: int) -> list[ArticleAffiliateProgramRead]:
        return [
            ArticleAffiliateProgramRead(
                id=link.id,
                article_id=link.article_id,
                affiliate_program_id=link.affiliate_program_id,
                is_primary=link.is_primary,
                created_at=link.created_at,
            )
            for link in self._links.list_by_article(article_id)
        ]

    # -- validation helpers -----------------------------------------
    def _validate_incomplete(
        self, plan: ArticlePlanDTO, payload: ArticlePlanApproveRequest
    ) -> None:
        if plan.readiness.complete:
            return
        if payload.acknowledge_incomplete_plan:
            return
        missing = ", ".join(plan.readiness.missing_components)
        raise PlanApprovalError(
            "plan is incomplete (missing signals: "
            f"{missing}); set acknowledge_incomplete_plan=true to override"
        )

    def _validate_cannibalization(
        self, plan: ArticlePlanDTO, payload: ArticlePlanApproveRequest
    ) -> None:
        if not plan.cannibalization.acknowledgment_required:
            return
        if payload.acknowledge_cannibalization:
            return
        raise PlanApprovalError(
            "originality below threshold "
            f"({plan.cannibalization.originality}); review differentiation and "
            "set acknowledge_cannibalization=true"
        )

    def _validate_no_live_article(self, keyword_id: int) -> None:
        for article in self._articles.list_by_keyword(keyword_id):
            if ArticleStatus(article.status) in _LIVE_ARTICLE_STATUSES:
                raise DuplicateEntityError(_ARTICLE, "keyword_id", keyword_id)

    def _validate_slug(self, slug: str) -> None:
        if self._articles.get_by_slug(slug) is not None:
            raise DuplicateEntityError(_ARTICLE, "slug", slug)

    def _validate_affiliates(
        self, plan: ArticlePlanDTO, payload: ArticlePlanApproveRequest
    ) -> tuple[int | None, list[int]]:
        candidate_ids = {c.program_id for c in plan.affiliate_candidates}
        primary_id = payload.primary_affiliate_program_id
        secondary_ids = list(payload.secondary_affiliate_program_ids)

        if primary_id is not None and primary_id not in candidate_ids:
            raise PlanApprovalError(
                f"primary_affiliate_program_id {primary_id} is not an active "
                "matched candidate for this keyword"
            )
        if len(secondary_ids) != len(set(secondary_ids)):
            raise PlanApprovalError("secondary_affiliate_program_ids contains duplicates")
        if primary_id is not None and primary_id in secondary_ids:
            raise PlanApprovalError(
                "primary_affiliate_program_id must not also appear in "
                "secondary_affiliate_program_ids"
            )
        invalid = [pid for pid in secondary_ids if pid not in candidate_ids]
        if invalid:
            raise PlanApprovalError(
                f"secondary_affiliate_program_ids {invalid} are not active matched "
                "candidates for this keyword"
            )
        return primary_id, secondary_ids

    # -- building blocks ------------------------------------------
    def _readiness(self, keyword: Keyword) -> PlanReadiness:
        present: list[str] = []
        missing: list[str] = []
        for component in COMPONENT_NAMES:
            if self._signals.get_latest(keyword.id, component) is not None:
                present.append(component)
            else:
                missing.append(component)
        return PlanReadiness(
            complete=not missing,
            present_components=present,
            missing_components=missing,
            opportunity_score=keyword.opportunity_score,
        )

    def _affiliate_candidates(
        self, keyword_text: str
    ) -> tuple[list[_Candidate], list[int]]:
        programs = self._programs.list_active(limit=_ACTIVE_CATALOG_LIMIT)
        facts = [_to_facts(p) for p in programs]
        matched = match_programs(keyword_text, facts)

        def order_key(m: object) -> tuple[int, float, str, int]:
            has_pct = (
                (m.commission_type or "").strip().lower() == "percentage"
                and m.commission_value is not None
                and m.commission_value >= 0
            )
            has_any = m.commission_type is not None and m.commission_value is not None
            group = 0 if has_pct else (1 if has_any else 2)
            neg_value = -(m.commission_value or 0.0)
            return (group, neg_value, m.name.casefold(), m.program_id)

        ordered = sorted(matched, key=order_key)
        candidates: list[_Candidate] = []
        for m in ordered:
            group = order_key(m)[0]
            role = (
                "primary_candidate"
                if group == 0
                else ("secondary_candidate" if group == 1 else "comparison_candidate")
            )
            candidates.append(
                _Candidate(
                    program_id=m.program_id,
                    read=AffiliateCandidateRead(
                        program_id=m.program_id,
                        name=m.name,
                        provider=m.provider,
                        commission_type=m.commission_type,
                        commission_value=m.commission_value,
                        currency=m.currency,
                        matched_terms=list(m.matched_terms),
                        monetization_data_available=(
                            m.commission_type is not None
                            and m.commission_value is not None
                        ),
                        recommended_role=role,
                    ),
                )
            )
        return candidates, [c.program_id for c in candidates]

    def _snapshot_program_ids(self, keyword_id: int) -> tuple[bool, list[int]]:
        """``(snapshot_available, ids)`` を返す。

        - Signal 不在 / ``raw_data`` が dict でない / ``matched_program_ids`` キーが
          list でない -> ``(False, [])`` (snapshot 情報そのものが無い)。
        - ``matched_program_ids`` が list (空を含む) -> ``(True, [ints...])``
          (空配列 ``[]`` は「生成時点で 0 件マッチ」という有効な snapshot)。
        """

        signal = self._signals.get_latest(keyword_id, "affiliate_opportunity")
        if signal is None or not isinstance(signal.raw_data, dict):
            return False, []
        raw_ids = signal.raw_data.get("matched_program_ids")
        if not isinstance(raw_ids, list):
            return False, []
        return True, [int(x) for x in raw_ids if isinstance(x, int)]

    def _cannibalization(
        self, keyword: Keyword, article_type: ArticleType | None
    ) -> CannibalizationInfo:
        signal = self._signals.get_latest(keyword.id, "originality")
        if signal is None:
            return CannibalizationInfo(
                originality=None,
                corpus_available=None,
                max_similarity=None,
                most_similar_kind=None,
                most_similar_keyword_id=None,
                most_similar_keyword_text=None,
                guidance=planning.cannibalization_guidance(
                    keyword.keyword, article_type, None, None
                ),
                acknowledgment_required=False,
            )
        raw = signal.raw_data if isinstance(signal.raw_data, dict) else {}
        originality = signal.normalized_value
        similar_text = raw.get("most_similar_keyword_text")
        return CannibalizationInfo(
            originality=originality,
            corpus_available=raw.get("corpus_available"),
            max_similarity=raw.get("max_similarity"),
            most_similar_kind=raw.get("most_similar_kind"),
            most_similar_keyword_id=raw.get("most_similar_keyword_id"),
            most_similar_keyword_text=similar_text,
            guidance=planning.cannibalization_guidance(
                keyword.keyword, article_type, originality, similar_text
            ),
            acknowledgment_required=originality < planning.CANNIBALIZATION_THRESHOLD,
        )

    def _slug_taken(self, slug: str) -> bool:
        return self._articles.get_by_slug(slug) is not None

    def _warnings(
        self,
        *,
        article_type: ArticleType | None,
        readiness: PlanReadiness,
        catalog_drift: bool,
        snapshot_available: bool,
        cannibalization: CannibalizationInfo,
        proposed_slug: str,
        slug_available: bool,
        keyword: str,
    ) -> list[str]:
        warnings: list[str] = []
        if article_type is None:
            warnings.append(
                "article_type_undetermined: keyword に明示 intent marker が無い。"
                "human が記事タイプを確定すること"
            )
        if not readiness.complete:
            warnings.append(
                "incomplete_plan: "
                f"missing {', '.join(readiness.missing_components)}"
            )
        if catalog_drift:
            warnings.append(
                "catalog_drift: affiliate_opportunity Signal 生成時点と live catalog の "
                "matched program が一致しない。Signal 再導出を検討"
            )
        elif not snapshot_available:
            warnings.append(
                "catalog_snapshot_unavailable: affiliate_opportunity Signal に "
                "matched_program_ids が無く drift を判定できない (未 drift として扱う)"
            )
        if cannibalization.acknowledgment_required:
            warnings.append(
                "cannibalization_acknowledgment_required: "
                f"originality={cannibalization.originality}"
            )
        base = planning.suggest_slug(keyword, article_type)
        if proposed_slug != base or not slug_available:
            warnings.append(
                f"slug_collision: 基本案 '{base}' が使用済みのため '{proposed_slug}' を提案。"
                "approve request で override 可"
            )
        return warnings

    # -- read mappers -----------------------------------------------
    @staticmethod
    def _article_read(entity: Article) -> ArticleRead:
        return ArticleRead(
            id=entity.id,
            keyword_id=entity.keyword_id,
            title=entity.title,
            slug=entity.slug,
            status=ArticleStatus(entity.status),
            draft_content=entity.body,
            published_url=entity.published_url,
            wordpress_id=entity.wordpress_post_id,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            published_at=entity.published_at,
        )


def _to_facts(program: AffiliateProgram) -> ProgramFacts:
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
