"""DraftInputSnapshot の **read-only deterministic builder**。

preview と freeze の両方がこの 1 つの builder を使う (別ロジックを書かない, §26)。
DB write は一切しない。

責務:
    Article / Keyword / ArticlePlan / ArticleAffiliateProgram / AffiliateProgram /
    FactPack / latest ArticleFact / Source を read-only で集約し、
    - 決定論的な semantic grid (7 tool × 17 FactKey = 119 cell) を構築
    - claim partition invariant を検証
    - canonical payload と content_hash を生成
    - freeze gate 判定 (can_freeze / failed_gates) を生成
まで。

raise:
    EntityNotFoundError          -- Article が存在しない
    DraftInputNotReadyError      -- artifact をそもそも組み立てられない
                                   (keyword 無し / ArticlePlan build 失敗 /
                                    claim partition 崩れ / fact→source 参照不整合)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.article.draft_input_canonical import (
    canonical_commission,
    canonical_datetime,
    compute_content_hash,
)
from app.article.fact_freshness import (
    FEATURE_MAX_AGE,
    PRICING_MAX_AGE,
    STATIC_MAX_AGE,
    is_fresh,
)
from app.article.fact_keys import (
    RECOMMENDED_FACT_KEYS,
    REQUIRED_FACT_KEYS,
    FactKey,
)
from app.exceptions import DraftInputNotReadyError, EntityNotFoundError
from app.models import (
    BUILDER_VERSION,
    PLAN_SNAPSHOT_ORIGIN,
    SNAPSHOT_VERSION,
    AffiliateProgram,
    Article,
)
from app.models.enums import AffiliateProgramStatus, ArticleStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_affiliate_program_repository import (
    ArticleAffiliateProgramRepository,
)
from app.repositories.article_fact_repository import ArticleFactRepository
from app.repositories.article_repository import ArticleRepository
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.source_repository import SourceRepository
from app.services.article_plan_service import ArticlePlanService
from app.services.fact_pack_service import FactPackService

_FACT_KEYS: tuple[str, ...] = tuple(str(k) for k in FactKey)
_FACT_POLICY_VERSION = "v1"
_CLAIM_TAXONOMY_VERSION = "fact_keys_v1"
_PRIMARY_AUTHORITY = "human_confirmed_article_affiliate_program.is_primary"
_PLANNING_ROLE_NONE = "not_a_current_candidate"


@dataclass(frozen=True)
class BuildResult:
    article_id: int
    payload: dict
    content_hash: str
    readiness: dict
    gate_status: dict
    # payload から機械導出した親行の非正規化フィールド (§50)。
    snapshot_version: str
    builder_version: str
    plan_snapshot_origin: str
    primary_affiliate_program_id: int | None
    comparison_program_ids: list[int]
    drafting_allowed_at_freeze: bool

    @property
    def can_freeze(self) -> bool:
        return bool(self.gate_status["can_freeze"])


class DraftInputSnapshotBuilder:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._keywords = KeywordRepository(session)
        self._links = ArticleAffiliateProgramRepository(session)
        self._programs = AffiliateProgramRepository(session)
        self._facts = ArticleFactRepository(session)
        self._sources = SourceRepository(session)

    # -- public --------------------------------------------------------
    def build(self, article_id: int, *, now: datetime | None = None) -> BuildResult:
        now = now or datetime.now(UTC)

        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError("Article", article_id)

        if article.keyword_id is None:
            raise DraftInputNotReadyError(
                f"Article {article_id} has no keyword; cannot build ArticlePlan"
            )
        keyword = self._keywords.get_by_id(article.keyword_id)
        if keyword is None:
            raise DraftInputNotReadyError(
                f"keyword {article.keyword_id} not found; cannot build ArticlePlan"
            )

        try:
            plan = ArticlePlanService(self._session).plan_for_keyword(article.keyword_id)
        except Exception as exc:  # noqa: BLE001 - surface as not-ready
            raise DraftInputNotReadyError(
                f"ArticlePlan build failed: {exc}"
            ) from exc

        fact_pack = FactPackService(self._session).build(article_id, now=now)

        links = sorted(
            self._links.list_by_article(article_id),
            key=lambda link: link.affiliate_program_id,
        )
        programs_by_id: dict[int, AffiliateProgram] = {}
        for link in links:
            program = self._programs.get_by_id(link.affiliate_program_id)
            if program is not None:
                programs_by_id[program.id] = program

        latest_facts = self._facts.get_latest_facts_for_article(article_id)
        facts_by_cell: dict[tuple[str, str], object] = {
            (row.subject_ref, row.fact_key): row for row in latest_facts
        }
        all_sources_by_id = {
            s.id: s for s in self._sources.list_by_article(article_id)
        }
        readiness_by_subject = {
            r.subject_ref: r for r in fact_pack.readiness.per_tool
        }

        role_by_program: dict[int, str] = {
            c.program_id: c.recommended_role for c in plan.affiliate_candidates
        }

        # --- tools grid + referenced source union ---
        referenced_source_ids: set[int] = set()
        tools: list[dict] = []
        counts = {
            "verified": 0,
            "unknown": 0,
            "not_applicable": 0,
            "not_researched": 0,
        }
        for link in links:
            program = programs_by_id.get(link.affiliate_program_id)
            if program is None:
                raise DraftInputNotReadyError(
                    f"linked affiliate_program {link.affiliate_program_id} not found"
                )
            subject = program.name
            cells: list[dict] = []
            usable: list[str] = []
            do_not_claim: list[str] = []
            for key in FactKey:
                cell = self._build_cell(
                    subject_ref=subject,
                    fact_key=key,
                    row=facts_by_cell.get((subject, str(key))),
                    all_sources_by_id=all_sources_by_id,
                    now=now,
                )
                cells.append(cell)
                counts[cell["state"]] += 1
                if cell["claim_allowed"]:
                    usable.append(str(key))
                else:
                    do_not_claim.append(str(key))
                if cell["source"] is not None:
                    referenced_source_ids.add(cell["source"]["source_id"])

            self._assert_claim_partition(subject, usable, do_not_claim, fact_pack)

            tr = readiness_by_subject.get(subject)
            tools.append(
                {
                    "affiliate_program_id": program.id,
                    "subject_ref": subject,
                    "is_primary": bool(link.is_primary),
                    "cells": cells,
                    "usable_claims": usable,
                    "do_not_claim": do_not_claim,
                    "readiness": {
                        "ok": bool(tr.ok) if tr is not None else False,
                        "missing_required": sorted(tr.missing_required) if tr else [],
                        "stale_required": sorted(tr.stale_required) if tr else [],
                    },
                }
            )

        # --- sources: referenced-only, source_id ASC (§3-B / §26) ---
        sources_payload = [
            self._source_entry(all_sources_by_id[sid])
            for sid in sorted(referenced_source_ids)
            if sid in all_sources_by_id
        ]

        # --- comparison_set + selection ---
        comparison_set = [
            self._comparison_entry(link, programs_by_id[link.affiliate_program_id],
                                   role_by_program)
            for link in links
        ]
        primary_links = [link for link in links if link.is_primary]
        primary_link = primary_links[0] if len(primary_links) == 1 else None
        primary_program = (
            programs_by_id.get(primary_link.affiliate_program_id)
            if primary_link is not None
            else None
        )
        comparison_program_ids = sorted(
            link.affiliate_program_id for link in links
        )
        selection = {
            "primary_affiliate_program_id": (
                primary_link.affiliate_program_id if primary_link else None
            ),
            "primary_article_affiliate_program_id": (
                primary_link.id if primary_link else None
            ),
            "primary_subject_ref": (
                primary_program.name if primary_program is not None else None
            ),
            "comparison_program_ids": comparison_program_ids,
            "authority": _PRIMARY_AUTHORITY,
        }

        readiness_payload = self._readiness_payload(fact_pack)

        payload: dict = {
            "snapshot_version": SNAPSHOT_VERSION,
            "article": {
                "id": article.id,
                "keyword_id": article.keyword_id,
                "title": article.title,
                "slug": article.slug,
            },
            "keyword": {
                "id": keyword.id,
                "text": keyword.keyword,
                "category": keyword.category,
            },
            "plan": self._plan_semantic(plan),
            "comparison_set": comparison_set,
            "selection": selection,
            "tools": tools,
            "sources": sources_payload,
            "policy": self._policy_payload(),
            "readiness": readiness_payload,
            "audit": self._audit_payload(
                now=now, plan=plan, keyword=keyword, counts=counts,
                n_tools=len(tools), n_sources=len(sources_payload),
            ),
        }

        content_hash = compute_content_hash(payload)
        gate_status = self._gate_status(
            article=article,
            links=links,
            primary_links=primary_links,
            programs_by_id=programs_by_id,
            fact_pack=fact_pack,
        )

        return BuildResult(
            article_id=article.id,
            payload=payload,
            content_hash=content_hash,
            readiness=readiness_payload,
            gate_status=gate_status,
            snapshot_version=SNAPSHOT_VERSION,
            builder_version=BUILDER_VERSION,
            plan_snapshot_origin=PLAN_SNAPSHOT_ORIGIN,
            primary_affiliate_program_id=selection["primary_affiliate_program_id"],
            comparison_program_ids=comparison_program_ids,
            drafting_allowed_at_freeze=bool(readiness_payload["drafting_allowed"]),
        )

    # -- cell / grid --------------------------------------------------
    def _build_cell(
        self,
        *,
        subject_ref: str,
        fact_key: FactKey,
        row,
        all_sources_by_id: dict,
        now: datetime,
    ) -> dict:
        if row is None:
            return {
                "fact_key": str(fact_key),
                "state": "not_researched",
                "fact_id": None,
                "affiliate_program_id": None,
                "value": None,
                "unknown_reason": None,
                "checked_at": None,
                "fresh": None,
                "claim_allowed": False,
                "source": None,
            }

        state = str(row.value_status)
        if state not in {"verified", "unknown", "not_applicable"}:
            raise DraftInputNotReadyError(
                f"fact {row.id} ({subject_ref}/{fact_key}) has unexpected "
                f"value_status {state!r}"
            )
        source = None
        if row.source_id is not None:
            src = all_sources_by_id.get(row.source_id)
            if src is None:
                raise DraftInputNotReadyError(
                    f"fact {row.id} ({subject_ref}/{fact_key}) references missing "
                    f"source {row.source_id}"
                )
            source = self._cell_source(src)
        return {
            "fact_key": str(fact_key),
            "state": state,
            "fact_id": row.id,
            "affiliate_program_id": row.affiliate_program_id,
            "value": row.fact_value,
            "unknown_reason": row.unknown_reason,
            "checked_at": canonical_datetime(row.checked_at),
            "fresh": is_fresh(fact_key, row.checked_at, now=now),
            "claim_allowed": state == "verified",
            "source": source,
        }

    def _assert_claim_partition(
        self, subject: str, usable: list[str], do_not_claim: list[str], fact_pack
    ) -> None:
        u, d = set(usable), set(do_not_claim)
        if u | d != set(_FACT_KEYS) or u & d:
            raise DraftInputNotReadyError(
                f"{subject}: usable_claims/do_not_claim is not a 17-key partition"
            )
        # FactPack と独立に計算した結果が一致することを保証 (どちらも同じルール)。
        fp_tool = next(
            (t for t in fact_pack.tool_facts if t.subject_ref == subject), None
        )
        if fp_tool is not None and set(fp_tool.usable_claims) != u:
            raise DraftInputNotReadyError(
                f"{subject}: usable_claims disagree with FactPack"
            )

    # -- sub-payloads ----------------------------------------------
    @staticmethod
    def _cell_source(src) -> dict:
        return {
            "source_id": src.id,
            "source_type": src.source_type,
            "source_url": src.source_url,
            "source_title": src.title,
            "source_checked_at": canonical_datetime(src.checked_at),
        }

    @staticmethod
    def _source_entry(src) -> dict:
        return {
            "id": src.id,
            "article_id": src.article_id,
            "source_type": src.source_type,
            "source_url": src.source_url,
            "title": src.title,
            "checked_at": canonical_datetime(src.checked_at),
        }

    @staticmethod
    def _comparison_entry(link, program: AffiliateProgram, role_by_program: dict) -> dict:
        return {
            "article_affiliate_program_id": link.id,
            "affiliate_program_id": program.id,
            "program_name": program.name,
            "provider": program.provider,
            "program_status": program.status,
            "commission_type": program.commission_type,
            "commission_value": canonical_commission(program.commission_value),
            "currency": program.currency,
            "is_primary": bool(link.is_primary),
            "planning_role": role_by_program.get(program.id, _PLANNING_ROLE_NONE),
        }

    @staticmethod
    def _plan_semantic(plan) -> dict:
        return {
            "plan_snapshot_origin": PLAN_SNAPSHOT_ORIGIN,
            "article_type": plan.article_type.value if plan.article_type else None,
            "target_reader": plan.target_reader,
            "search_intent_summary": plan.search_intent_summary,
            "primary_goal": plan.primary_goal,
            "secondary_goals": list(plan.secondary_goals),
            "outline": [
                {
                    "level": s.level,
                    "heading": s.heading,
                    "purpose": s.purpose,
                    "required_elements": list(s.required_elements),
                }
                for s in plan.outline
            ],
            "comparison_axes": [
                {"axis": a.axis, "data_availability": a.data_availability}
                for a in plan.comparison_axes
            ],
            "cta_strategy": plan.cta_strategy,
            "cannibalization_guidance": plan.cannibalization.guidance,
            "cannibalization_acknowledgment_required": bool(
                plan.cannibalization.acknowledgment_required
            ),
            "compliance_checklist": list(plan.compliance_checklist),
            "quality_guardrails": list(plan.quality_guardrails),
            "source_requirements": list(plan.source_requirements),
        }

    @staticmethod
    def _policy_payload() -> dict:
        return {
            "fact_policy_version": _FACT_POLICY_VERSION,
            "claim_taxonomy_version": _CLAIM_TAXONOMY_VERSION,
            "builder_version": BUILDER_VERSION,
            "fact_key_order": list(_FACT_KEYS),
            "required_fact_keys": [str(k) for k in REQUIRED_FACT_KEYS],
            "recommended_fact_keys": [str(k) for k in RECOMMENDED_FACT_KEYS],
            "freshness_policy": {
                "pricing_days": PRICING_MAX_AGE.days,
                "feature_days": FEATURE_MAX_AGE.days,
                "static_days": STATIC_MAX_AGE.days,
                "future_checked_at_is_fresh": False,
            },
        }

    @staticmethod
    def _readiness_payload(fact_pack) -> dict:
        return {
            "drafting_allowed": bool(fact_pack.readiness.drafting_allowed),
            "blocking_reasons": sorted(fact_pack.readiness.blocking_reasons),
            "per_tool": [
                {
                    "subject_ref": r.subject_ref,
                    "ok": bool(r.ok),
                    "missing_required": sorted(r.missing_required),
                    "stale_required": sorted(r.stale_required),
                }
                for r in sorted(
                    fact_pack.readiness.per_tool,
                    key=lambda r: r.subject_ref,
                )
            ],
            "freshness": {
                "within_policy": bool(fact_pack.freshness.within_policy),
                "stale_facts": sorted(
                    (
                        {
                            "subject_ref": s.subject_ref,
                            "fact_key": s.fact_key,
                            "checked_at": canonical_datetime(s.checked_at),
                            "max_age_days": s.max_age_days,
                        }
                        for s in fact_pack.freshness.stale_facts
                    ),
                    key=lambda s: (s["subject_ref"], s["fact_key"]),
                ),
                "stalest_pricing_checked_at": canonical_datetime(
                    fact_pack.freshness.stalest_pricing_checked_at
                ),
            },
            "warnings": sorted(fact_pack.warnings),
        }

    @staticmethod
    def _audit_payload(
        *, now, plan, keyword, counts: dict, n_tools: int, n_sources: int
    ) -> dict:
        present = counts["verified"] + counts["unknown"] + counts["not_applicable"]
        return {
            "built_at": canonical_datetime(now),
            "plan": {
                "working_title": plan.working_title,
                "proposed_slug": plan.proposed_slug,
                "slug_available": bool(plan.slug_available),
                "readiness": {
                    "complete": bool(plan.readiness.complete),
                    "present_components": sorted(plan.readiness.present_components),
                    "missing_components": sorted(plan.readiness.missing_components),
                    "opportunity_score": plan.readiness.opportunity_score,
                },
                "catalog": {
                    "catalog_drift": bool(plan.catalog_drift),
                    "catalog_snapshot_available": bool(plan.catalog_snapshot_available),
                    "snapshot_program_ids": sorted(plan.snapshot_program_ids),
                    "live_program_ids": sorted(plan.live_program_ids),
                },
                "warnings": sorted(plan.warnings),
            },
            "keyword": {"opportunity_score": keyword.opportunity_score},
            "counts": {
                "comparison_tools": n_tools,
                "fact_key_taxonomy": len(_FACT_KEYS),
                "semantic_cells": n_tools * len(_FACT_KEYS),
                "present_latest_facts": present,
                "verified": counts["verified"],
                "unknown": counts["unknown"],
                "not_applicable": counts["not_applicable"],
                "not_researched": counts["not_researched"],
                "referenced_sources": n_sources,
            },
        }

    # -- freeze gate ------------------------------------------------
    @staticmethod
    def _gate_status(
        *, article: Article, links, primary_links, programs_by_id, fact_pack
    ) -> dict:
        failed: list[str] = []
        if str(article.status) != ArticleStatus.PLANNED.value:
            failed.append("article_not_planned")
        if article.body is not None:
            failed.append("article_body_present")
        if article.meta_description is not None:
            failed.append("article_meta_description_present")
        if article.published_url is not None:
            failed.append("article_published_url_present")
        if article.wordpress_post_id is not None:
            failed.append("article_wordpress_post_id_present")
        if len(links) < 1:
            failed.append("no_comparison_links")
        if len(primary_links) != 1:
            failed.append("primary_not_exactly_one")
        else:
            primary_pid = primary_links[0].affiliate_program_id
            if primary_pid not in {link.affiliate_program_id for link in links}:
                failed.append("primary_not_in_comparison_set")
        for link in links:
            program = programs_by_id.get(link.affiliate_program_id)
            if program is None or str(program.status) != AffiliateProgramStatus.ACTIVE.value:
                failed.append(
                    f"inactive_affiliate_program:{link.affiliate_program_id}"
                )
        if not fact_pack.readiness.drafting_allowed:
            failed.append("factpack_drafting_not_allowed")
        if fact_pack.readiness.blocking_reasons:
            failed.append("factpack_blocking_reasons")
        if any(r.stale_required for r in fact_pack.readiness.per_tool):
            failed.append("required_fact_stale")
        if not fact_pack.freshness.within_policy:
            failed.append("freshness_not_within_policy")
        failed = sorted(dict.fromkeys(failed))
        return {"can_freeze": not failed, "failed_gates": failed}
