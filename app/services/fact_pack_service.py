"""FactPack を read-time に導出する Service (DB write 禁止)。

- ``build(article_id)`` は Source / ArticleFact の **現在値 (latest)** と ArticlePlan
  から :class:`FactPackDTO` を毎回集約する。
- 現在値 = ``(article_id, subject_ref, fact_key)`` ごとに ``checked_at DESC, id DESC``。
  DB に current flag は保存しない。
- 比較対象 subject 集合 = Article の ``ArticleAffiliateProgram`` links に紐づく program
  (V1 固定。human subset 選択 / 非 affiliate tool は将来)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware, is_fresh, max_age_for
from app.article.fact_keys import (
    MIN_LIST_LEN,
    RECOMMENDED_FACT_KEYS,
    REQUIRED_FACT_KEYS,
    FactKey,
    ValueStatus,
)
from app.article.schemas import (
    FactEntry,
    FactPackAffiliateCandidate,
    FactPackDTO,
    FactPackPlanMetadata,
    FactPackReadiness,
    FreshnessReport,
    MissingFact,
    SourceCoverage,
    StaleFact,
    ToolFacts,
    ToolReadiness,
)
from app.exceptions import EntityNotFoundError
from app.models import AffiliateProgram, ArticleFact, Source
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_affiliate_program_repository import (
    ArticleAffiliateProgramRepository,
)
from app.repositories.article_fact_repository import ArticleFactRepository
from app.repositories.article_repository import ArticleRepository
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.source_repository import SourceRepository
from app.services.article_plan_service import ArticlePlanService
from app.services.article_service import ArticleService

_PRICING_STATUS_OK = {ValueStatus.VERIFIED, ValueStatus.UNKNOWN}


class FactPackService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._keywords = KeywordRepository(session)
        self._links = ArticleAffiliateProgramRepository(session)
        self._programs = AffiliateProgramRepository(session)
        self._facts = ArticleFactRepository(session)
        self._sources = SourceRepository(session)

    def build(self, article_id: int, *, now: datetime | None = None) -> FactPackDTO:
        now = now or datetime.now(UTC)
        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError("Article", article_id)

        keyword = (
            self._keywords.get_by_id(article.keyword_id)
            if article.keyword_id is not None
            else None
        )
        plan_metadata = self._plan_metadata(article.keyword_id)

        programs = self._subject_programs(article_id)
        sources_by_id = {s.id: s for s in self._sources.list_by_article(article_id)}
        latest_facts = self._facts.get_latest_facts_for_article(article_id)
        by_subject: dict[str, dict[str, ArticleFact]] = {}
        for row in latest_facts:
            by_subject.setdefault(row.subject_ref, {})[row.fact_key] = row

        tool_facts: list[ToolFacts] = []
        missing_facts: list[MissingFact] = []
        stale_facts: list[StaleFact] = []
        per_tool_readiness: list[ToolReadiness] = []
        warnings: list[str] = []
        tools_with_official_pricing = 0

        for program in programs:
            subject = program.name
            facts_for = by_subject.get(subject, {})
            entries: list[FactEntry] = []
            usable: list[str] = []
            do_not_claim: list[str] = []
            pricing_checked: list[datetime] = []
            verified_checked: list[datetime] = []
            future_keys: list[str] = []

            for key in FactKey:
                row = facts_for.get(str(key))
                if row is None:
                    # missing/not_researched も LLM の言及禁止対象 (do_not_claim)。
                    # FactKey 定義順で列挙するため loop 内で append する。
                    do_not_claim.append(str(key))
                    missing_facts.append(
                        MissingFact(
                            subject_ref=subject, fact_key=str(key), reason="not_researched"
                        )
                    )
                    continue
                checked_at = ensure_aware(row.checked_at)
                if checked_at > now:
                    future_keys.append(str(key))
                fresh = is_fresh(key, checked_at, now=now)
                entries.append(
                    FactEntry(
                        fact_key=str(key),
                        value=row.fact_value,
                        value_status=row.value_status,
                        source_id=row.source_id,
                        source_url=(
                            sources_by_id[row.source_id].source_url
                            if row.source_id in sources_by_id
                            else None
                        ),
                        checked_at=checked_at,
                        unknown_reason=row.unknown_reason,
                        fresh=fresh,
                    )
                )
                if row.value_status == ValueStatus.VERIFIED:
                    usable.append(str(key))
                    verified_checked.append(checked_at)
                else:
                    do_not_claim.append(str(key))
                    reason = (
                        "unknown"
                        if row.value_status == ValueStatus.UNKNOWN
                        else "not_applicable"
                    )
                    missing_facts.append(
                        MissingFact(subject_ref=subject, fact_key=str(key), reason=reason)
                    )
                if key in {
                    FactKey.PRICING_SUMMARY,
                    FactKey.FREE_PLAN_AVAILABLE,
                    FactKey.FREE_TRIAL_AVAILABLE,
                    FactKey.BUSINESS_PLAN_AVAILABLE,
                }:
                    pricing_checked.append(checked_at)

            pricing_row = facts_for.get(str(FactKey.PRICING_SUMMARY))
            if (
                pricing_row is not None
                and ValueStatus(pricing_row.value_status) in _PRICING_STATUS_OK
                and pricing_row.source_id is not None
            ):
                tools_with_official_pricing += 1

            readiness, tool_stale = self._tool_readiness(subject, facts_for, now)
            per_tool_readiness.append(readiness)
            stale_facts.extend(tool_stale)
            warnings.extend(self._recommended_warnings(subject, facts_for))
            if future_keys:
                warnings.append(
                    f"fact_checked_at_in_future[{subject}]: {', '.join(future_keys)} "
                    "(checked_at が現在時刻より未来。fresh 扱いせず readiness も stale とする。"
                    "timezone / import データの確認が必要)"
                )

            tool_facts.append(
                ToolFacts(
                    subject_ref=subject,
                    affiliate_program_id=program.id,
                    facts=entries,
                    usable_claims=usable,
                    do_not_claim=do_not_claim,
                    pricing_checked_at=max(pricing_checked) if pricing_checked else None,
                    last_verified_at=(
                        max(verified_checked) if verified_checked else None
                    ),
                )
            )

        blocking = self._blocking_reasons(programs, per_tool_readiness)
        drafting_allowed = bool(programs) and not blocking
        if plan_metadata is not None and plan_metadata.article_type is None:
            warnings.append(
                "article_type_undetermined: ArticlePlan の記事タイプが未確定"
            )

        pricing_checked_all = [
            t.pricing_checked_at for t in tool_facts if t.pricing_checked_at is not None
        ]

        return FactPackDTO(
            article=ArticleService._to_read(article),
            keyword_id=article.keyword_id,
            keyword=keyword.keyword if keyword is not None else None,
            plan_metadata=plan_metadata,
            affiliate_candidates=self._candidates(article_id, programs),
            tool_facts=tool_facts,
            source_coverage=self._coverage(
                article_id, len(programs), tools_with_official_pricing
            ),
            missing_facts=missing_facts,
            freshness=FreshnessReport(
                within_policy=not any(
                    FactKey(s.fact_key) in REQUIRED_FACT_KEYS for s in stale_facts
                ),
                stale_facts=stale_facts,
                stalest_pricing_checked_at=(
                    min(pricing_checked_all) if pricing_checked_all else None
                ),
            ),
            readiness=FactPackReadiness(
                drafting_allowed=drafting_allowed,
                per_tool=per_tool_readiness,
                blocking_reasons=blocking,
            ),
            warnings=warnings,
        )

    # -- helpers --------------------------------------------------
    def _subject_programs(self, article_id: int) -> list[AffiliateProgram]:
        links = self._links.list_by_article(article_id)
        programs: list[AffiliateProgram] = []
        for link in sorted(links, key=lambda x: x.affiliate_program_id):
            program = self._programs.get_by_id(link.affiliate_program_id)
            if program is not None:
                programs.append(program)
        return programs

    def _plan_metadata(self, keyword_id: int | None) -> FactPackPlanMetadata | None:
        if keyword_id is None:
            return None
        plan = ArticlePlanService(self._session).plan_for_keyword(keyword_id)
        return FactPackPlanMetadata(
            article_type=plan.article_type.value if plan.article_type else None,
            target_reader=plan.target_reader,
            search_intent_summary=plan.search_intent_summary,
            outline_headings=[s.heading for s in plan.outline],
            comparison_axes=[a.axis for a in plan.comparison_axes],
            cta_strategy=plan.cta_strategy,
            cannibalization_guidance=plan.cannibalization.guidance,
        )

    def _candidates(
        self, article_id: int, programs: list[AffiliateProgram]
    ) -> list[FactPackAffiliateCandidate]:
        # ArticlePlan の candidate ordering / role を参照する。
        role_by_id: dict[int, str] = {}
        commission_by_id: dict[int, tuple[str | None, float | None]] = {}
        article = self._articles.get_by_id(article_id)
        if article is not None and article.keyword_id is not None:
            plan = ArticlePlanService(self._session).plan_for_keyword(article.keyword_id)
            for c in plan.affiliate_candidates:
                role_by_id[c.program_id] = c.recommended_role
                commission_by_id[c.program_id] = (c.commission_type, c.commission_value)
        out: list[FactPackAffiliateCandidate] = []
        for p in programs:
            ct, cv = commission_by_id.get(p.id, (p.commission_type, p.commission_value))
            out.append(
                FactPackAffiliateCandidate(
                    program_id=p.id,
                    name=p.name,
                    provider=p.provider,
                    recommended_role=role_by_id.get(p.id, "comparison_candidate"),
                    commission_type=ct,
                    commission_value=cv,
                )
            )
        return out

    def _coverage(
        self, article_id: int, tools_total: int, tools_with_pricing: int
    ) -> SourceCoverage:
        sources: list[Source] = self._sources.list_by_article(article_id)
        by_type: dict[str, int] = {}
        for s in sources:
            by_type[s.source_type] = by_type.get(s.source_type, 0) + 1
        return SourceCoverage(
            source_count=len(sources),
            by_type=by_type,
            tools_with_official_pricing=tools_with_pricing,
            tools_total=tools_total,
        )

    def _tool_readiness(
        self, subject: str, facts_for: dict[str, ArticleFact], now: datetime
    ) -> tuple[ToolReadiness, list[StaleFact]]:
        missing_required: list[str] = []
        stale_required: list[StaleFact] = []
        for key in REQUIRED_FACT_KEYS:
            row = facts_for.get(str(key))
            if row is None:
                missing_required.append(str(key))
                continue
            status = ValueStatus(row.value_status)
            if key in {FactKey.PRICING_SUMMARY, FactKey.FREE_PLAN_AVAILABLE}:
                if status not in _PRICING_STATUS_OK:
                    missing_required.append(str(key))
                    continue
            else:
                if status is not ValueStatus.VERIFIED:
                    missing_required.append(str(key))
                    continue
                min_len = MIN_LIST_LEN.get(key)
                if min_len is not None and not (
                    isinstance(row.fact_value, list) and len(row.fact_value) >= min_len
                ):
                    missing_required.append(str(key))
                    continue
            if not is_fresh(key, row.checked_at, now=now):
                stale_required.append(
                    StaleFact(
                        subject_ref=subject,
                        fact_key=str(key),
                        checked_at=ensure_aware(row.checked_at),
                        max_age_days=max_age_for(key).days,
                    )
                )
        ok = not missing_required and not stale_required
        return (
            ToolReadiness(
                subject_ref=subject,
                ok=ok,
                missing_required=missing_required,
                stale_required=[s.fact_key for s in stale_required],
            ),
            stale_required,
        )

    def _recommended_warnings(
        self, subject: str, facts_for: dict[str, ArticleFact]
    ) -> list[str]:
        missing = [
            str(k)
            for k in RECOMMENDED_FACT_KEYS
            if facts_for.get(str(k)) is None
            or ValueStatus(facts_for[str(k)].value_status) is not ValueStatus.VERIFIED
        ]
        if not missing:
            return []
        return [
            f"recommended_fact_missing[{subject}]: {', '.join(missing)} "
            "(drafting 可・本文では『不明』と明記)"
        ]

    def _blocking_reasons(
        self,
        programs: list[AffiliateProgram],
        per_tool: list[ToolReadiness],
    ) -> list[str]:
        reasons: list[str] = []
        if not programs:
            reasons.append("no comparison subjects (Article に affiliate link がない)")
        for r in per_tool:
            if not r.ok:
                parts = []
                if r.missing_required:
                    parts.append(f"missing required: {', '.join(r.missing_required)}")
                if r.stale_required:
                    parts.append(f"stale required: {', '.join(r.stale_required)}")
                reasons.append(f"{r.subject_ref}: {'; '.join(parts)}")
        return reasons
