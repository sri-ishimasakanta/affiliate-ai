"""Keyword Signal のビジネスロジック。

- Keyword 存在確認
- 書き込みは Service が commit、失敗時 rollback (既存方針を維持)
- 手動 Signal 登録では正規化ロジックを持たない (受け取った 0〜100 値を保存するだけ)
- ``site_relevance`` / ``affiliate_opportunity`` は外部データを使わない完全ローカルな
  導出のため、純粋 normalizer を呼んでここで Signal を作る
  (Google Ads 由来ではないので Metrics Collection Service には置かない)

DB アクセスは Repository に委譲する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.exceptions import EntityNotFoundError
from app.keyword.affiliate_fit import FIT_CORE, FIT_LOOSE, FIT_UNREVIEWED, default_fit_config
from app.keyword.affiliate_matching import MatchedProgram, ProgramFacts
from app.keyword.affiliate_tiers import (
    CATALOG_MATCH_IGNORES_JAPANESE_SPACING,
    TIER_STRONG,
    TIER_WEAK,
    TieredMatch,
    match_catalog,
    scoring_programs,
)
from app.keyword.normalizers.affiliate_opportunity import (
    COMMISSION_WEIGHT,
    PROGRAM_MATCH_WEIGHT,
    PROVIDER_SPREAD_WEIGHT,
    AffiliateOpportunityResult,
    calculate_affiliate_opportunity,
)
from app.keyword.normalizers.competition_ease import (
    CompetitionEaseResult,
    calculate_competition_ease,
)
from app.keyword.normalizers.originality import (
    ARTICLE_KEYWORD_EVIDENCE_WEIGHT,
    KEYWORD_EVIDENCE_WEIGHT,
    KIND_ARTICLE_KEYWORD,
    KIND_ARTICLE_TITLE,
    KIND_KEYWORD,
    NGRAM_SIZE,
    TITLE_EVIDENCE_WEIGHT,
    OriginalityCandidate,
    OriginalityResult,
    calculate_originality,
)
from app.keyword.normalizers.site_relevance import (
    SiteRelevanceResult,
    calculate_site_relevance,
)
from app.keyword.schemas import (
    CompetitionEaseManualCreate,
    KeywordSignalCreate,
    KeywordSignalRead,
)
from app.models import KeywordSignal
from app.models.enums import KeywordSignalComponent
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_repository import ArticleRepository
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository

_KEYWORD_ENTITY = "Keyword"
_SIGNAL_ENTITY = "KeywordSignal"

# site_relevance はローカル site profile 由来の静的評価。外部 provider ではない。
_SITE_RELEVANCE_PROVIDER = "site_profile"
_SITE_RELEVANCE_SOURCE_REFERENCE = "site-profile:ai-business-automation:v1"

# affiliate_opportunity はローカル Affiliate Catalog 由来の供給側評価。
_AFFILIATE_OPPORTUNITY_PROVIDER = "affiliate_catalog"
# v2 (C2.5.7): strong、または weak かつ core fit の match を score に使う。
# それ以外は raw_data に残す (0 寄与)。
_AFFILIATE_OPPORTUNITY_SOURCE_REFERENCE = "affiliate-catalog:local:v2"
_AFFILIATE_SCORING_POLICY = "strong_or_core_v1"
_ACTIVE_PROGRAM_LIMIT = 100_000

# originality はサイト内部の既存 Keyword / Article corpus 由来。外部 provider ではない。
_ORIGINALITY_PROVIDER = "internal_corpus"
_ORIGINALITY_SOURCE_REFERENCE = "internal-corpus:v1"
_ORIGINALITY_KEYWORD_STATUSES = ("analyzed", "selected", "assigned")
_ORIGINALITY_ARTICLE_STATUSES = ("approved", "published", "rewrite")

# competition_ease は無料ツール等で確認した Organic SEO Keyword Difficulty の手動投入。
_COMPETITION_EASE_PROVIDER = "manual_keyword_difficulty"
_COMPETITION_EASE_SOURCE_REFERENCE = "manual-keyword-difficulty:v1"


def _build_competition_ease_raw_data(
    result: CompetitionEaseResult, *, source_name: str
) -> dict[str, object]:
    """competition_ease の計算根拠を JSON-safe な dict で返す。

    **credential / API key / password / account ID / Google Ads competition /
    tracking parameter は保存しない。**
    """

    return {
        "keyword_difficulty": result.keyword_difficulty,
        "competition_ease": result.normalized_value,
        "difficulty_scale": result.difficulty_scale,
        "source_name": source_name,
        "evidence_available": result.evidence_available,
        "evidence_coverage": result.evidence_coverage,
        "collection_method": "manual",
        "normalizer_version": result.normalizer_version,
        "normalizer": {
            "name": result.normalizer_name,
            "version": result.normalizer_version,
        },
    }


def _build_site_relevance_raw_data(result: SiteRelevanceResult) -> dict[str, object]:
    """site_relevance の計算根拠を JSON-safe な dict で返す (実際に match した語のみ)。"""

    return {
        "base_score": result.base_score,
        "matched_groups": list(result.matched_groups),
        "matched_terms": list(result.matched_terms),
        "business_context_terms": list(result.business_context_terms),
        "out_of_scope_terms": list(result.out_of_scope_terms),
        "multi_group_bonus": result.multi_group_bonus,
        "business_context_bonus": result.business_context_bonus,
        "profile_name": result.profile_name,
        "profile_version": result.profile_version,
        "normalizer_version": result.normalizer_version,
        "normalizer": {
            "name": result.normalizer_name,
            "version": result.normalizer_version,
        },
    }


def _commission_kind(program: MatchedProgram) -> str:
    return (program.commission_type or "").strip().casefold()


def _build_affiliate_opportunity_raw_data(
    matched: list[MatchedProgram],
    result: AffiliateOpportunityResult,
    *,
    catalog_size: int,
    active_catalog_size: int,
    tiered: list[TieredMatch] | None = None,
) -> dict[str, object]:
    """affiliate_opportunity の計算根拠を JSON-safe な dict で返す。

    ``matched`` は **score に使った program (strong、または weak かつ core fit)**。commission /
    provider の項目はこの集合だけから作る。``tiered`` (全 match) は報告と catalog_drift 判定のために
    残す: ``matched_program_ids`` / ``matched_program_names`` / ``matched_terms`` は tier / fit に
    関わらず「現在の candidate 集合」で、ArticlePlan の live candidate と同じ集合になる (tier / fit
    だけでは drift を作らない)。weak + loose / unreviewed は score に寄与しない
    (0。重みは作らない)。

    **tracking_url / landing_page_url / affiliate ID / credential は含めない。**
    """

    every = tiered if tiered is not None else []
    strong_tiers = [t for t in every if t.tier == TIER_STRONG]
    weak_tiers = [t for t in every if t.tier == TIER_WEAK]
    weak_core = [t for t in weak_tiers if t.fit == FIT_CORE]
    weak_loose = [t for t in weak_tiers if t.fit == FIT_LOOSE]
    weak_unreviewed = [t for t in weak_tiers if t.fit not in (FIT_CORE, FIT_LOOSE)]

    def _terms(tiers: list[TieredMatch]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for tier in tiers:
            for term_tier in tier.term_tiers:
                if term_tier.term not in seen:
                    seen.add(term_tier.term)
                    out.append(term_tier.term)
        return sorted(out)

    if tiered is None:  # tier 情報が無い呼び出し (互換): matched をそのまま「全 match」とみなす
        seen_terms: set[str] = set()
        legacy_terms: list[str] = []
        for program in matched:
            for term in program.matched_terms:
                if term not in seen_terms:
                    seen_terms.add(term)
                    legacy_terms.append(term)
        all_terms = sorted(legacy_terms)
    else:
        all_terms = _terms(every)

    percentage_commissions = [
        {"program_id": p.program_id, "name": p.name, "value": p.commission_value}
        for p in matched
        if _commission_kind(p) == "percentage"
        and p.commission_value is not None
        and p.commission_value >= 0
    ]
    # fixed は provenance のみ (V1 score には使わない。FX 換算もしない)。
    fixed_commissions = [
        {
            "program_id": p.program_id,
            "name": p.name,
            "value": p.commission_value,
            "currency": p.currency,
        }
        for p in matched
        if _commission_kind(p) == "fixed"
        and p.commission_value is not None
        and p.commission_value >= 0
    ]
    active_providers = sorted(
        {p.provider.strip() for p in matched if p.provider and p.provider.strip()}
    )

    return {
        "program_match_score": result.program_match_score,
        "commission_score": result.commission_score,
        "provider_spread_score": result.provider_spread_score,
        "program_match_weight": PROGRAM_MATCH_WEIGHT,
        "commission_weight": COMMISSION_WEIGHT,
        "provider_spread_weight": PROVIDER_SPREAD_WEIGHT,
        "available_weight": result.available_weight,
        "evidence_coverage": result.evidence_coverage,
        "market_evidence_available": result.market_evidence_available,
        # ---- C2.5.7: strong-or-core policy。brand tier (strong / weak) と fit (core / loose /
        # unreviewed) は独立。score 対象 = strong、または weak かつ core。それ以外は score 0 だが
        # 全 metadata を raw_data に残す。
        "scoring_policy": _AFFILIATE_SCORING_POLICY,
        "match_semantics": {
            "japanese_spacing": CATALOG_MATCH_IGNORES_JAPANESE_SPACING,
            "strong": "own program name or explicit alias (eligible regardless of fit)",
            "weak": "no brand/product-name intent: generic category/feature/use-case term",
            "eligible": "strong, or weak with core fit",
            "not_eligible": "weak with loose or unreviewed fit (retained, contributes 0)",
            "fit_config_version": default_fit_config().version,
        },
        "scored_program_count": result.matched_program_count,
        "scored_program_ids": [p.program_id for p in matched],
        "strong_program_ids": [t.program_id for t in strong_tiers],
        "strong_program_names": [t.name for t in strong_tiers],
        "strong_terms": _terms(strong_tiers),
        "weak_program_ids": [t.program_id for t in weak_tiers],
        "weak_program_names": [t.name for t in weak_tiers],
        "weak_terms": _terms(weak_tiers),
        "weak_core_program_ids": [t.program_id for t in weak_core],
        "weak_core_program_names": [t.name for t in weak_core],
        "weak_loose_program_ids": [t.program_id for t in weak_loose],
        "weak_loose_program_names": [t.name for t in weak_loose],
        "weak_unreviewed_program_ids": [t.program_id for t in weak_unreviewed],
        "weak_unreviewed_program_names": [t.name for t in weak_unreviewed],
        "matches": [
            {
                "program_id": t.program_id,
                "name": t.name,
                "brand_tier": t.tier,
                "fit": t.fit,
                "scoring_eligible": t.scoring_eligible,
                "primary_eligible": t.primary_eligible,
                "strong_terms": list(t.strong_terms),
                "core_terms": list(t.terms_with_fit(FIT_CORE)),
                "loose_terms": list(t.terms_with_fit(FIT_LOOSE)),
                "unreviewed_terms": list(t.terms_with_fit(FIT_UNREVIEWED)),
                "reason": t.reason,
            }
            for t in every
        ],
        "alias_only_strong_program_ids": [t.program_id for t in every if not t.legacy_matched],
        # 現在の candidate 集合 (strong + weak)。ArticlePlan の live candidate と同じ集合。
        "matched_program_count": len(every) if tiered is not None else result.matched_program_count,
        "matched_program_ids": (
            [t.program_id for t in every] if tiered is not None else [p.program_id for p in matched]
        ),
        "matched_program_names": (
            [t.name for t in every] if tiered is not None else [p.name for p in matched]
        ),
        "matched_terms": all_terms,
        "distinct_provider_count": result.distinct_provider_count,
        "active_providers": active_providers,
        "percentage_commissions": percentage_commissions,
        "fixed_commissions": fixed_commissions,
        "catalog_size": catalog_size,
        "active_catalog_size": active_catalog_size,
        "normalizer_version": result.normalizer_version,
        "normalizer": {
            "name": result.normalizer_name,
            "version": result.normalizer_version,
        },
    }


def _build_originality_raw_data(
    result: OriginalityResult,
    *,
    keyword_candidates_count: int,
    article_keyword_candidates_count: int,
    article_title_candidates_count: int,
    keyword_total: int,
    article_total: int,
    self_excluded_keyword_id: int,
    self_article_exists: bool,
) -> dict[str, object]:
    """originality の計算根拠を JSON-safe な dict で返す。

    **Article.body 全文 / meta_description / URL / credential / 個人情報は保存しない。**
    most similar は Keyword: id + text、Article: id + title まで。
    """

    return {
        "corpus_available": result.corpus_available,
        "evidence_coverage": result.evidence_coverage,
        "candidates_count": result.candidates_count,
        "keyword_candidates_count": keyword_candidates_count,
        "article_keyword_candidates_count": article_keyword_candidates_count,
        "article_title_candidates_count": article_title_candidates_count,
        "keyword_total": keyword_total,
        "article_total": article_total,
        "corpus_size_total": keyword_total + article_total,
        "max_similarity": round(result.max_similarity, 4),
        "raw_similarity": round(result.raw_similarity, 4),
        "bigram_dice": round(result.bigram_dice, 4),
        "sequence_matcher": round(result.sequence_matcher, 4),
        "most_similar_kind": result.most_similar_kind,
        "most_similar_keyword_id": result.most_similar_keyword_id,
        "most_similar_keyword_text": (
            result.most_similar_text
            if result.most_similar_kind in (KIND_KEYWORD, KIND_ARTICLE_KEYWORD)
            else None
        ),
        "most_similar_article_id": result.most_similar_article_id,
        "most_similar_article_title": (
            result.most_similar_text
            if result.most_similar_kind == KIND_ARTICLE_TITLE
            else None
        ),
        "similarity_method": "char_bigram_dice|sequencematcher_max",
        "ngram_size": NGRAM_SIZE,
        "title_evidence_weight": TITLE_EVIDENCE_WEIGHT,
        "status_filter": {
            "keyword": list(_ORIGINALITY_KEYWORD_STATUSES),
            "article": list(_ORIGINALITY_ARTICLE_STATUSES),
        },
        "self_excluded_keyword_id": self_excluded_keyword_id,
        "self_article_exists": self_article_exists,
        "intent_adjustment_applied": False,
        "normalizer_version": result.normalizer_version,
        "normalizer": {
            "name": result.normalizer_name,
            "version": result.normalizer_version,
        },
    }


def to_signal_read(entity: KeywordSignal) -> KeywordSignalRead:
    """``KeywordSignal`` -> ``KeywordSignalRead`` の変換。"""

    return KeywordSignalRead(
        id=entity.id,
        keyword_id=entity.keyword_id,
        component=KeywordSignalComponent(entity.component),
        normalized_value=entity.normalized_value,
        provider=entity.provider,
        raw_data=entity.raw_data,
        source_reference=entity.source_reference,
        observed_at=entity.observed_at,
        period_start=entity.period_start,
        period_end=entity.period_end,
        created_at=entity.created_at,
    )


class KeywordSignalService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._keywords = KeywordRepository(session)
        self._signals = KeywordSignalRepository(session)
        self._programs = AffiliateProgramRepository(session)
        self._articles = ArticleRepository(session)

    # -- write --------------------------------------------------------------
    def create_signal(
        self,
        keyword_id: int,
        payload: KeywordSignalCreate,
    ) -> KeywordSignalRead:
        self._ensure_keyword_exists(keyword_id)

        try:
            entity = self._signals.create(
                keyword_id=keyword_id,
                component=payload.component,
                normalized_value=payload.normalized_value,
                provider=payload.provider,
                observed_at=payload.observed_at,
                raw_data=payload.raw_data,
                source_reference=payload.source_reference,
                period_start=payload.period_start,
                period_end=payload.period_end,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return to_signal_read(entity)

    def derive_site_relevance(self, keyword_id: int) -> KeywordSignalRead:
        """keyword 文字列とサイト profile から site_relevance Signal を導出する。

        完全ローカル・決定論的 (外部 API / LLM なし)。再実行するたびに新しい Signal を
        追記する (KeywordSignal の immutable history 設計を維持)。時系列データでは
        ないため period_start / period_end は None。
        """

        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)

        result = calculate_site_relevance(keyword.keyword)
        raw_data = _build_site_relevance_raw_data(result)
        observed_at = datetime.now(UTC)

        try:
            entity = self._signals.create(
                keyword_id=keyword_id,
                component=KeywordSignalComponent.SITE_RELEVANCE,
                normalized_value=result.normalized_value,
                provider=_SITE_RELEVANCE_PROVIDER,
                observed_at=observed_at,
                raw_data=raw_data,
                source_reference=_SITE_RELEVANCE_SOURCE_REFERENCE,
                period_start=None,
                period_end=None,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return to_signal_read(entity)

    def derive_affiliate_opportunity(self, keyword_id: int) -> KeywordSignalRead:
        """keyword とローカル Affiliate Catalog から affiliate_opportunity Signal を導出する。

        **供給側** の評価 (この keyword で紹介できる active 案件がどれだけ / どの程度
        儲かるか)。検索者の購買意図 (commercial_intent) とは別。catalog は読み取り専用で
        変更しない。再実行で新しい Signal を追記する (immutable history 維持)。
        時系列データではないため period_start / period_end は None。
        """

        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)

        active_rows = self._programs.list_active(limit=_ACTIVE_PROGRAM_LIMIT)
        facts = [
            ProgramFacts(
                program_id=row.id,
                name=row.name,
                provider=row.provider,
                category=row.category,
                commission_type=row.commission_type,
                commission_value=row.commission_value,
                currency=row.currency,
                match_terms=tuple(row.match_terms or ()),
            )
            for row in active_rows
        ]
        # C2.5.7: tier + fit 付き照合 (日本語の分かち書きは expansion / plan / queue と同じ扱い)。
        # score に使うのは strong、または weak かつ core。それ以外は寄与 0 で raw_data に残す
        # (weak の重みは作らない。式・重みは変えない)。
        tiered = match_catalog(keyword.keyword, facts)
        scored = scoring_programs(tiered)
        result = calculate_affiliate_opportunity(scored)

        raw_data = _build_affiliate_opportunity_raw_data(
            scored,
            result,
            catalog_size=self._programs.count(),
            active_catalog_size=len(facts),
            tiered=tiered,
        )
        observed_at = datetime.now(UTC)

        try:
            entity = self._signals.create(
                keyword_id=keyword_id,
                component=KeywordSignalComponent.AFFILIATE_OPPORTUNITY,
                normalized_value=result.normalized_value,
                provider=_AFFILIATE_OPPORTUNITY_PROVIDER,
                observed_at=observed_at,
                raw_data=raw_data,
                source_reference=_AFFILIATE_OPPORTUNITY_SOURCE_REFERENCE,
                period_start=None,
                period_end=None,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return to_signal_read(entity)

    def derive_originality(self, keyword_id: int) -> KeywordSignalRead:
        """keyword とサイト内部の既存 Keyword / Article corpus から originality を導出する。

        カニバリゼーション可能性の逆指標。**Google 検索の外部競合 (`competition_ease`)
        とは別物。** corpus は read-only。current keyword 自身 (id) と、current keyword
        に紐づく Article は比較対象から除外する。再導出で新 Signal を追記 (immutable
        history 維持)。時系列でないため period_start / period_end は None。
        """

        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)

        candidates: list[OriginalityCandidate] = []

        keyword_rows = self._keywords.list_originality_candidates(
            exclude_id=keyword_id, statuses=_ORIGINALITY_KEYWORD_STATUSES
        )
        for other_id, other_text in keyword_rows:
            candidates.append(
                OriginalityCandidate(
                    kind=KIND_KEYWORD,
                    text=other_text,
                    evidence_weight=KEYWORD_EVIDENCE_WEIGHT,
                    keyword_id=other_id,
                )
            )

        article_rows = self._articles.list_originality_candidates(
            exclude_keyword_id=keyword_id, statuses=_ORIGINALITY_ARTICLE_STATUSES
        )
        article_keyword_count = 0
        article_title_count = 0
        for article_id, linked_keyword_id, linked_keyword_text, title in article_rows:
            if linked_keyword_text:
                article_keyword_count += 1
                candidates.append(
                    OriginalityCandidate(
                        kind=KIND_ARTICLE_KEYWORD,
                        text=linked_keyword_text,
                        evidence_weight=ARTICLE_KEYWORD_EVIDENCE_WEIGHT,
                        keyword_id=linked_keyword_id,
                        article_id=article_id,
                    )
                )
            if title:
                article_title_count += 1
                candidates.append(
                    OriginalityCandidate(
                        kind=KIND_ARTICLE_TITLE,
                        text=title,
                        evidence_weight=TITLE_EVIDENCE_WEIGHT,
                        article_id=article_id,
                    )
                )

        result = calculate_originality(candidates, keyword=keyword.keyword)
        raw_data = _build_originality_raw_data(
            result,
            keyword_candidates_count=len(keyword_rows),
            article_keyword_candidates_count=article_keyword_count,
            article_title_candidates_count=article_title_count,
            keyword_total=self._keywords.count(),
            article_total=self._articles.count(),
            self_excluded_keyword_id=keyword_id,
            self_article_exists=self._articles.count(keyword_id=keyword_id) > 0,
        )
        observed_at = datetime.now(UTC)

        try:
            entity = self._signals.create(
                keyword_id=keyword_id,
                component=KeywordSignalComponent.ORIGINALITY,
                normalized_value=result.normalized_value,
                provider=_ORIGINALITY_PROVIDER,
                observed_at=observed_at,
                raw_data=raw_data,
                source_reference=_ORIGINALITY_SOURCE_REFERENCE,
                period_start=None,
                period_end=None,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return to_signal_read(entity)

    def derive_competition_ease_manual(
        self, keyword_id: int, payload: CompetitionEaseManualCreate
    ) -> KeywordSignalRead:
        """手動投入した Organic SEO Keyword Difficulty から competition_ease を導出する。

        外部 API 通信なし。``ease = 100 - keyword_difficulty``。observed_at は入力が
        あればそれ、無ければ生成時 UTC。再投入で新 Signal を追記 (immutable history)。
        """

        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)

        result = calculate_competition_ease(payload.keyword_difficulty)
        raw_data = _build_competition_ease_raw_data(
            result, source_name=payload.source_name
        )
        observed_at = payload.observed_at or datetime.now(UTC)
        source_reference = (
            payload.source_reference or _COMPETITION_EASE_SOURCE_REFERENCE
        )

        try:
            entity = self._signals.create(
                keyword_id=keyword_id,
                component=KeywordSignalComponent.COMPETITION_EASE,
                normalized_value=result.normalized_value,
                provider=_COMPETITION_EASE_PROVIDER,
                observed_at=observed_at,
                raw_data=raw_data,
                source_reference=source_reference,
                period_start=None,
                period_end=None,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return to_signal_read(entity)

    # -- read ---------------------------------------------------------------
    def get_signal(self, keyword_id: int, signal_id: int) -> KeywordSignalRead:
        self._ensure_keyword_exists(keyword_id)
        entity = self._signals.get_by_id(signal_id)
        if entity is None or entity.keyword_id != keyword_id:
            raise EntityNotFoundError(_SIGNAL_ENTITY, signal_id)
        return to_signal_read(entity)

    def get_latest_signal(
        self,
        keyword_id: int,
        component: KeywordSignalComponent,
    ) -> KeywordSignalRead:
        self._ensure_keyword_exists(keyword_id)
        entity = self._signals.get_latest(keyword_id, component)
        if entity is None:
            raise EntityNotFoundError(_SIGNAL_ENTITY, (keyword_id, str(component)))
        return to_signal_read(entity)

    def list_signals(
        self,
        keyword_id: int,
        *,
        component: KeywordSignalComponent | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KeywordSignalRead]:
        self._ensure_keyword_exists(keyword_id)
        if component is None:
            rows = self._signals.list_by_keyword(keyword_id, limit=limit, offset=offset)
        else:
            rows = self._signals.list_by_component(
                keyword_id, component, limit=limit, offset=offset
            )
        return [to_signal_read(row) for row in rows]

    # -- helpers ----------------------------------------------------------
    def _ensure_keyword_exists(self, keyword_id: int) -> None:
        if self._keywords.get_by_id(keyword_id) is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)
