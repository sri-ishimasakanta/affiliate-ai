"""Content Intelligence の組み立て (C10-2 / C10-B)。**読むだけ・外に問い合わせない。**

手元のデータと保存済みの外のデータだけから、クラスタ・gap・発見の候補・再利用できる事実・
記事の種類の勧め・次の記事の候補を作る (``app/content/*`` の pure の規則)。成分は別々に持ち、
合成の点数は作らない。何も書かない (``read_only_session``)。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.content import clusters as cl
from app.content import content_types as ct
from app.content import discovery as dc
from app.content import facts as fx
from app.content import gaps as gp
from app.content import next_article as na
from app.content.text import phrase_key, phrase_text
from app.models import (
    AffiliateLinkTarget,
    Article,
    ArticleFact,
    Keyword,
    KeywordScore,
    SearchConsoleQueryDaily,
    SeoImprovementCandidate,
)

ROOT = Path(__file__).resolve().parents[2]
_LIVE_STATUSES = ("planned", "drafting", "review", "approved", "published", "rewrite")
_PLANNED_STATUSES = ("planned", "drafting", "review", "approved")


@dataclass
class ContentIntelligence:
    """1 回の組み立ての結果 (``as_dict`` で JSON にできる)。"""

    as_of: str
    clusters: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    discovery: list = field(default_factory=list)
    facts: list = field(default_factory=list)
    candidates: list = field(default_factory=list)  # 候補ごとの証拠 (keyword / 発見)
    next_articles: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    refresh: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "clusters": [c.as_dict() for c in self.clusters],
            "gaps": [g.as_dict() for g in self.gaps],
            "discovery": [d.as_dict() for d in self.discovery],
            "facts": [f.as_dict() for f in self.facts],
            "fact_research_plan": fx.research_plan(self.facts),
            "candidates": list(self.candidates),
            "next_articles": [n.as_dict() for n in self.next_articles],
            "sources": dict(self.sources), "refresh": dict(self.refresh),
            "composite_score": None, "notes": list(self.notes),
        }


class ContentIntelligenceService:
    def __init__(self, session: Session, *, settings=None, registry: cl.ClusterRegistry | None =
                 None, seeds_path: Path | str | None = None) -> None:  # fmt: skip
        self._session = session
        if settings is None:
            from app.config.settings import get_settings

            settings = get_settings()
        self._settings = settings
        self._registry = registry or cl.load_registry()
        self._seeds_path = (Path(seeds_path) if seeds_path
                            else ROOT / "app/config/discovery_seeds.json")

    # -- 読む ---------------------------------------------------------------------------------
    def build(self, *, now: datetime | None = None) -> ContentIntelligence:
        from app.services.content_queue_service import read_only_session

        now = ensure_aware(now or datetime.now(UTC))
        with read_only_session(self._session):
            try:
                return self._build(now)
            finally:
                self._session.rollback()

    def _build(self, now: datetime) -> ContentIntelligence:
        from app.analysis import evidence_contract as ec
        from app.services.analysis_evidence_service import AnalysisEvidenceService
        from app.services.index_state_service import IndexStateService
        from app.services.source_health_service import SourceHealthService

        registry = self._registry
        keywords = list(self._session.scalars(select(Keyword).order_by(Keyword.id)))
        articles = list(self._session.scalars(select(Article).order_by(Article.id)))
        kw_text = {k.id: k.keyword for k in keywords}
        article_facts = [cl.ArticleFact(a.id, a.keyword_id, kw_text.get(a.keyword_id),
                                        str(a.status), a.article_type, a.monetization_mode)
                         for a in articles]
        clusters = cl.build_clusters(registry, keywords=[
            cl.KeywordFact(k.id, k.keyword, str(k.status)) for k in keywords],
            articles=article_facts, observed_at=now.isoformat())
        live_by_keyword = {a.keyword_id: a for a in articles
                           if a.keyword_id is not None and str(a.status) != "archived"}
        # 成分 (C10-A の共通の形) と score
        evidence = {int(c.subject_id.split(":")[1]): c for c in
                    AnalysisEvidenceService(self._session, settings=self._settings)
                    .keywords(now=now)}
        scored = {s.keyword_id for s in self._session.scalars(select(KeywordScore))}
        catalog = self._catalog()
        # 事実・索引・内部リンク・収益
        facts = fx.build_subject_facts(self._stored_facts(), now=now)
        stale_by_article = self._stale_fact_articles(facts)
        observations, _meta = IndexStateService(self._session).latest(now=now)
        index_states = {aid: {"normalized_status": o.normalized_status,
                              "freshness": o.freshness_state, "observed_at": o.observed_at}
                        for aid, o in observations.items()}  # fmt: skip
        links = self._open_link_candidates()
        targets = list(self._session.scalars(select(AffiliateLinkTarget).where(
            AffiliateLinkTarget.status == "active")))  # fmt: skip
        monetization = {a.id: self._monetization(kw_text.get(a.keyword_id) or a.title,
                                                 catalog, a.id, targets) for a in articles}
        # 発見
        observations_d = self._discovery_observations()
        discovery = dc.merge_candidates(
            observations_d, keywords=[(k.id, k.keyword) for k in keywords],
            article_keywords=[(a.id, kw_text[a.keyword_id]) for a in articles
                              if a.keyword_id in kw_text and str(a.status) != "archived"],
            registry=registry)
        # 役割の根拠 → gap
        cluster_by_key = {c.cluster_key: c for c in clusters}
        role_evidence: dict[str, list[gp.RoleEvidence]] = {}
        open_keywords = [k for k in keywords if k.id not in live_by_keyword
                         and str(k.status) != "rejected"]
        for k in open_keywords:
            a = cl.assign_cluster(k.keyword, registry)
            if a.cluster_key:
                rec = self._recommend(k.keyword, cluster_by_key[a.cluster_key],
                                      evidence.get(k.id), catalog)
                role_evidence.setdefault(a.cluster_key, []).append(
                    gp.RoleEvidence(k.keyword, rec.role, "keyword", f"keyword:{k.id}"))
        for d in discovery:
            if d.is_new and d.cluster_key:
                role, _m = ct.role_from_marker(d.phrase)
                if role:
                    role_evidence.setdefault(d.cluster_key, []).append(gp.RoleEvidence(
                        d.phrase, role, "discovery", f"discovery:{d.phrase_key}"))
        for c in registry.clusters:
            for m in c.members:
                if m.source == "portfolio_plan" and m.planned_type:
                    role = ct.role_of_article(m.text, m.planned_type)
                    if role:
                        role_evidence.setdefault(c.cluster_key, []).append(
                            gp.RoleEvidence(m.text, role, "portfolio_plan"))
        sources = {k: v.as_dict() for k, v in SourceHealthService(
            self._session, settings=self._settings).collect(now=now).items()}
        source_states = {k: v["freshness_state"] for k, v in sources.items()}
        gaps = []
        for c in clusters:
            gaps += gp.find_gaps(c, role_evidence=role_evidence.get(c.cluster_key, []),
                                 link_candidates=links, stale_fact_articles=stale_by_article,
                                 index_states=index_states, monetization=monetization,
                                 source_freshness={k: source_states.get(k) for k in (
                                     "search_console", "url_inspection", "google_ads")})
        # 重なりの相手
        existing = self._existing_content(articles, kw_text, registry)
        growth = self._growth_candidates()
        candidates, next_articles = [], []
        for k in open_keywords:
            comp = evidence.get(k.id)
            components = {name: v.as_dict() for name, v in (comp.components.items() if comp
                                                           else {}.items())}
            a = cl.assign_cluster(k.keyword, registry)
            cluster = cluster_by_key.get(a.cluster_key) if a.cluster_key else None
            rec = self._recommend(k.keyword, cluster, comp, catalog)
            affiliate = self._affiliate(k.keyword, catalog)
            filled = [g.gap_key for g in gaps if cluster and g.cluster_id == cluster.cluster_id
                      and (g.expected_role == rec.role or g.gap_type == gp.GAP_NO_ARTICLE
                           or (g.gap_type == gp.GAP_MISSING_PILLAR and phrase_key(
                               cluster.pillar.get("keyword") or "") == phrase_key(k.keyword)))]
            role = (cl.ROLE_PILLAR if cluster and phrase_key(cluster.pillar.get("keyword") or "")
                    == phrase_key(k.keyword) else cl.ROLE_SUPPORTING)
            subject_readiness = self._facts_for(affiliate, facts)
            item = na.build_candidate(
                topic=phrase_text(k.keyword), keyword_id=k.id, discovery_key=None,
                cluster_id=cluster.cluster_id if cluster else None,
                cluster_key=cluster.cluster_key if cluster else None, cluster_role=role,
                recommendation=rec, gaps=filled, components=components, affiliate=affiliate,
                facts_readiness=subject_readiness, existing=existing, scored=k.id in scored,
                growth=growth.get(k.id), vocabulary=registry.vocabulary)
            next_articles.append(item)
            candidates.append(self._candidate_row(item, "keyword", a, components, affiliate,
                                                  subject_readiness))
        for d in discovery:
            if not d.is_new:
                continue
            cluster = cluster_by_key.get(d.cluster_key) if d.cluster_key else None
            rec = self._recommend(d.phrase, cluster, None, catalog)
            affiliate = self._affiliate(d.phrase, catalog)
            filled = [g.gap_key for g in gaps if cluster and g.cluster_id == cluster.cluster_id
                      and (g.expected_role == rec.role or g.gap_type == gp.GAP_NO_ARTICLE)]
            components = {name: ec.ComponentEvidence(name, ec.MISSING, source, access,
                                                     reason="not a Keyword yet").as_dict()
                          for name, (source, access) in ec.KEYWORD_COMPONENTS.items()}
            subject_readiness = self._facts_for(affiliate, facts)
            item = na.build_candidate(
                topic=d.phrase, keyword_id=None, discovery_key=d.phrase_key,
                cluster_id=cluster.cluster_id if cluster else None,
                cluster_key=d.cluster_key, cluster_role=cl.ROLE_SUPPORTING, recommendation=rec,
                gaps=filled, components=components, affiliate=affiliate,
                facts_readiness=subject_readiness, existing=existing, scored=False, growth=None,
                vocabulary=registry.vocabulary)
            next_articles.append(item)
            candidates.append(self._candidate_row(item, "discovery", cl.Assignment(
                d.cluster_key, d.cluster_basis), components, affiliate, subject_readiness,
                discovery=d))
        missing_ads = sorted(k.id for k in keywords
                             if (evidence.get(k.id) and evidence[k.id].components[
                                 "commercial_intent"].state == ec.MISSING))
        refresh = {"google_ads": {"keywords_without_stored_metrics": missing_ads,
                                  "discovery_candidates": sorted(
                                      d.phrase_key for d in discovery if d.is_new)}}
        return ContentIntelligence(
            as_of=now.isoformat(), clusters=clusters, gaps=gaps, discovery=discovery,
            facts=facts, candidates=candidates, next_articles=next_articles, sources=sources,
            refresh=refresh,
            notes=["read-only: no Keyword, Article or planning request is created",
                   "components are kept separate; no composite score",
                   "external data is read from storage only; refreshes are planned, not run"])

    # -- 部品 ---------------------------------------------------------------------------------
    def _catalog(self):
        from app.services.content_queue_service import ContentQueueService

        return ContentQueueService(self._session).load_catalog()

    @staticmethod
    def _affiliate(text: str, catalog) -> dict:
        from app.article.cluster_plan import affiliate_coverage, affiliate_matches_from_tiered
        from app.keyword.affiliate_tiers import match_catalog

        coverage = affiliate_coverage(affiliate_matches_from_tiered(match_catalog(text, catalog)))
        return {"eligible_programs": list(coverage.eligible_program_names),
                "strong_programs": list(coverage.strong_program_names),
                "level": coverage.level}

    def _monetization(self, text: str, catalog, article_id: int, targets) -> dict:
        affiliate = self._affiliate(text, catalog)
        blockers = []
        if affiliate["eligible_programs"] and not any(t.article_id == article_id
                                                      for t in targets):
            blockers.append("no active /go/ link target for this article")
        return {**affiliate, "blockers": blockers}

    @staticmethod
    def _recommend(text, cluster, comp, catalog) -> ct.ContentTypeRecommendation:
        from app.article.cluster_plan import Intent, intent_profile

        ci = None
        if comp is not None:
            c = comp.components.get("commercial_intent")
            if c is not None and c.state == "usable" and isinstance(c.value, (int, float)):
                ci = float(c.value)
        affiliate = ContentIntelligenceService._affiliate(text, catalog)
        covered = frozenset(cluster.roles_present) if cluster else frozenset()
        return ct.recommend_content_type(
            text, head_term=intent_profile(text).intent == Intent.HEAD,
            cluster_has_landing=ct.CATEGORY_LANDING in covered, commercial_intent=ci,
            affiliate_eligible=bool(affiliate["eligible_programs"]), covered_roles=covered)

    def _stored_facts(self) -> list[fx.StoredFact]:
        return [fx.StoredFact(f.article_id, f.subject_ref, f.fact_key, f.fact_value,
                              str(f.value_status), f.checked_at, f.source_id,
                              f.affiliate_program_id)
                for f in self._session.scalars(select(ArticleFact))]

    @staticmethod
    def _stale_fact_articles(facts: list[fx.SubjectFacts]) -> dict[int, list[str]]:
        out: dict[int, set[str]] = {}
        for s in facts:
            for v in s.facts:
                if not v.fresh:
                    out.setdefault(v.source_article_id, set()).add(v.fact_key)
        return {k: sorted(v) for k, v in out.items()}

    @staticmethod
    def _facts_for(affiliate: dict, facts: list[fx.SubjectFacts]) -> str | None:
        names = [phrase_key(n) for n in affiliate.get("eligible_programs") or []]
        if not names:
            return None
        found = [s for s in facts if s.subject_key in names]
        if not found:
            return fx.MISSING
        order = [fx.MISSING, fx.PARTIAL, fx.STALE, fx.READY]
        return min((s.readiness for s in found), key=order.index)

    def _open_link_candidates(self) -> dict[int, int]:
        from app.models import SeoImprovementRun

        run = self._session.scalars(select(SeoImprovementRun)
                                    .order_by(SeoImprovementRun.id.desc()).limit(1)).first()
        if run is None:
            return {}
        rows = self._session.scalars(select(SeoImprovementCandidate).where(
            SeoImprovementCandidate.seo_improvement_run_id == run.id,
            SeoImprovementCandidate.candidate_type == "INTERNAL_LINK_OPPORTUNITY"))
        return dict(Counter(r.article_id for r in rows))

    def _discovery_observations(self) -> list[dc.DiscoveryObservation]:
        out: list[dc.DiscoveryObservation] = []
        grouped: dict[str, dict] = {}
        for row in self._session.scalars(select(SearchConsoleQueryDaily)):
            g = grouped.setdefault(phrase_key(row.query), {
                "phrase": row.query, "impressions": 0, "clicks": 0, "pages": set(),
                "first": row.metric_date, "last": row.metric_date})
            g["impressions"] += row.impressions or 0
            g["clicks"] += row.clicks or 0
            g["pages"].add(row.page)
            g["first"] = min(g["first"], row.metric_date)
            g["last"] = max(g["last"], row.metric_date)
        for g in grouped.values():
            out.append(dc.DiscoveryObservation(
                dc.SRC_GSC_QUERY, g["phrase"], g["last"].isoformat(),
                {"impressions": g["impressions"], "clicks": g["clicks"],
                 "first_date": g["first"].isoformat(), "pages": sorted(g["pages"])}))
        for c in self._registry.clusters:
            for m in c.members:
                source = {"portfolio_plan": dc.SRC_PORTFOLIO_PLAN,
                          "portfolio_reserve": dc.SRC_PORTFOLIO_RESERVE,
                          "cluster_config": dc.SRC_CLUSTER_CONFIG}.get(m.source)
                if source:
                    out.append(dc.DiscoveryObservation(source, m.text, None,
                                                       {"cluster": c.cluster_key}))
        if self._seeds_path.exists():
            seeds = json.loads(self._seeds_path.read_text(encoding="utf-8")).get("seeds") or []
            for s in seeds:
                if isinstance(s, dict) and s.get("phrase"):
                    out.append(dc.DiscoveryObservation(dc.SRC_MANUAL_SEED, s["phrase"], None,
                                                       {"note": s.get("note")}))
        return out

    def _existing_content(self, articles, kw_text, registry) -> list[na.ExistingContent]:
        from app.models.growth_handoff import GH_ARTICLE_PLANNING, GH_OPEN_STATUSES
        from app.services.growth_handoff_service import GrowthHandoffService, handoff_ready

        out = []
        for a in articles:
            text = kw_text.get(a.keyword_id) or a.title
            if str(a.status) == "archived":
                continue
            kind = "planned_article" if str(a.status) in _PLANNED_STATUSES else "article"
            out.append(na.ExistingContent(kind, f"article:{a.id}", text, str(a.status),
                                          cl.assign_cluster(text, registry).cluster_key,
                                          ct.role_of_article(text, a.article_type)))
        if handoff_ready(self._session):
            for r in GrowthHandoffService(self._session).list(workflow=GH_ARTICLE_PLANNING):
                if r.status in GH_OPEN_STATUSES and r.keyword_id in kw_text:
                    out.append(na.ExistingContent("planning_request", f"handoff:{r.id}",
                                                  kw_text[r.keyword_id], r.status))
        return out

    def _growth_candidates(self) -> dict[int, dict]:
        from app.models.growth_action import GrowthActionCandidate

        out = {}
        for r in self._session.scalars(select(GrowthActionCandidate).where(
                GrowthActionCandidate.action_type == "create_new_article",
                GrowthActionCandidate.status.in_(("active", "pending_review", "approved")))
                .order_by(GrowthActionCandidate.id)):
            if r.keyword_id is not None:
                out[r.keyword_id] = {"id": r.id, "status": r.status,
                                     "opportunity_key": r.opportunity_key}
        return out

    @staticmethod
    def _candidate_row(item: na.NextArticleCandidate, kind: str, assignment, components,
                       affiliate, readiness, discovery: dc.DiscoveryCandidate | None = None):
        """ContentIntelligenceEvidence: 候補ごとの成分 (別々に。合成の点数は無い)。"""

        return {"schema": "content-intelligence/1", "subject": item.identity, "kind": kind,
                "topic": item.topic, "keyword_id": item.keyword_id,
                "cluster": {"cluster_id": item.cluster_id, "basis": assignment.basis},
                "gaps": list(item.gaps_filled), "content_type": dict(item.content_type),
                "components": components, "affiliate": affiliate,
                "facts_readiness": readiness,
                "cannibalization": list(item.cannibalization),
                "refresh_requirements": list(item.external_refresh) or (
                    list(discovery.refresh_needs) if discovery else []),
                "discovery": discovery.as_dict() if discovery else None,
                "composite_score": None}


__all__ = ["ContentIntelligence", "ContentIntelligenceService"]
