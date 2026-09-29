"""候補ごとの分析の証拠を、共通の形で集める (C10-A)。**読むだけ・外に問い合わせない。**

``app/analysis/evidence_contract.py`` の形で、keyword と記事の成分を別々に返す。値は保存済みの
signal・確認・クリックだけから読む (手元で導く成分も、ここでは新しく導かない: 最新の signal の
値をそのまま読む)。古さは ``source_policy.json`` の境で決める (古い値を最新として使わない)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import evidence_contract as ec
from app.analysis import sources as src
from app.article.fact_freshness import ensure_aware
from app.models import Article, Keyword, KeywordSignal


class AnalysisEvidenceService:
    def __init__(self, session: Session, *, settings=None) -> None:
        self._session = session
        self._settings = settings

    def keywords(self, *, keyword_ids=None, now: datetime | None = None
                 ) -> list[ec.CandidateEvidence]:  # fmt: skip
        now = ensure_aware(now or datetime.now(UTC))
        query = select(Keyword).order_by(Keyword.id)
        if keyword_ids is not None:
            query = query.where(Keyword.id.in_(list(keyword_ids)))
        keywords = list(self._session.scalars(query))
        latest: dict[tuple[int, str], KeywordSignal] = {}
        for row in self._session.scalars(select(KeywordSignal).where(
                KeywordSignal.keyword_id.in_([k.id for k in keywords]))
                .order_by(KeywordSignal.observed_at.desc(), KeywordSignal.id.desc())):
            if ensure_aware(row.observed_at) <= now:  # 未来の観測は使わない
                latest.setdefault((row.keyword_id, row.component), row)
        ads = src.source_definition("google_ads")
        out = []
        for k in keywords:
            components = {}
            for name, (source, access) in ec.KEYWORD_COMPONENTS.items():
                row = latest.get((k.id, name))
                components[name] = self._component(name, source, access, row, now=now,
                                                   stale_days=ads.get("stale_after_days")
                                                   if source == "google_ads" else None)
            out.append(ec.CandidateEvidence("keyword", f"keyword:{k.id}", components))
        self._session.rollback()
        return out

    @staticmethod
    def _component(name, source, access, row, *, now, stale_days) -> ec.ComponentEvidence:
        if row is None:
            return ec.ComponentEvidence(name, ec.MISSING, source, access,
                                        reason="no signal stored")
        raw = dict(row.raw_data or {}) if isinstance(row.raw_data, dict) else {}
        version = (raw.get("normalizer") or {}).get("version") or raw.get("normalizer_version")
        observed = ensure_aware(row.observed_at)
        state = ec.USABLE
        reason = None
        if stale_days is not None and src.age_state(
                observed, now=now, stale_after_days=stale_days) == src.STALE:
            state, reason = ec.STALE, f"older than {stale_days} days"
        flags = tuple(raw.get("quality_flags") or ())
        if name == "search_demand":
            from app.keyword.normalizers.search_demand import is_missing_search_demand

            if is_missing_search_demand(raw):
                return ec.ComponentEvidence(
                    name, ec.MISSING, source, access, provider=row.provider,
                    observed_at=observed.isoformat(), version=version,
                    reason="Google Ads returned no search volume evidence (average 0 and no "
                           "monthly history); the stored 0.0 is not a real zero",
                    quality_flags=("stored_zero_is_missing",),
                    provenance=f"keyword_signals:{row.id}")
        if name == "commercial_intent" and raw.get("market_evidence_state") == "missing":
            flags = flags + ("market_evidence_missing_query_intent_only",)
        return ec.ComponentEvidence(
            name, state, source, access, value=row.normalized_value, provider=row.provider,
            observed_at=observed.isoformat(), version=version, reason=reason,
            quality_flags=flags, provenance=f"keyword_signals:{row.id}")

    def articles(self, *, now: datetime | None = None) -> list[ec.CandidateEvidence]:
        from app.services.attribution_readiness_service import AttributionReadinessService
        from app.services.index_state_service import IndexStateService

        now = ensure_aware(now or datetime.now(UTC))
        observations, _meta = IndexStateService(self._session).latest(now=now)
        readiness = AttributionReadinessService(self._session).report()
        levels = {p["program_id"]: p for p in readiness["programs"]}
        from app.models import AffiliateLinkTarget

        targets = list(self._session.scalars(select(AffiliateLinkTarget).where(
            AffiliateLinkTarget.status == "active")))  # fmt: skip
        out = []
        for article in self._session.scalars(select(Article).order_by(Article.id)):
            o = observations.get(article.id)
            if o is None:
                index = ec.ComponentEvidence("index_state", ec.MISSING, "url_inspection",
                                             ec.ACCESS_CACHED, reason="never checked")
            else:
                state = (ec.USABLE if o.known and o.freshness_state == src.FRESH else
                         ec.STALE if o.known else ec.INSUFFICIENT)
                index = ec.ComponentEvidence(
                    "index_state", state, "url_inspection", ec.ACCESS_CACHED,
                    value=o.normalized_status, provider=o.provider, observed_at=o.observed_at,
                    reason=o.missing_reason, provenance=o.data_source)
            mine = [t for t in targets if t.article_id == article.id]
            programs = sorted({t.affiliate_program_id for t in mine})
            monetization = ec.ComponentEvidence(
                "attribution_readiness", ec.USABLE if mine else ec.MISSING, "affiliate_clicks",
                ec.ACCESS_CACHED, value=",".join(
                    f"{p}:{levels[p]['click_attribution_level']}/"
                    f"{levels[p]['commission_attribution_level']}" for p in programs
                    if p in levels) or None,
                reason=None if mine else "no active /go/ link target for this article",
                provenance="affiliate_link_targets")
            out.append(ec.CandidateEvidence("article", f"article:{article.id}",
                                            {"index_state": index,
                                             "attribution_readiness": monetization}))
        self._session.rollback()
        return out


__all__ = ["AnalysisEvidenceService"]
