"""Google Ads の値の取り直し (C10-2 の取り直しの計画の実行)。**既定は PLAN。**

- 対象: 保存済みの Google Ads の値が無い既存の Keyword と、値の無い発見の候補の語
  (``content_discovery_candidates``、状態 tracked)。
- 呼び出し: 既存の ``GoogleAdsKeywordMetricsProvider.fetch_historical_metrics`` (Historical
  Metrics) を、**提供元ごとに一括で** (``max_keywords_per_call`` 件まで 1 回)。新しい API の
  使い方は足さない。
- Keyword: 既存の bulk の収集と同じ signal (search_demand v2・commercial_intent v2・trend)。
  欠測は 0 にしない (値が無ければ signal を作らない)。
- 発見の候補: **Keyword を作らない。** 指標を候補の ``evidence_json["google_ads"]`` に保存するだけ
  (取った時刻・出典つき)。返ってこなかった語も「取った」と記録する (すぐに取り直さない)。
- 期待値 (語の数) が PLAN と違えば何も呼ばずに断る。取り直したものは、境 (45 日) まで次の計画に
  入らない。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.keyword.normalizers import search_demand as sd
from app.keyword.normalizers.commercial_intent import calculate_commercial_intent
from app.models import Keyword, KeywordSignal
from app.models.content_discovery import CDC_TRACKED, ContentDiscoveryCandidate


class GoogleAdsRefreshError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _stale_days() -> int:
    from app.analysis.sources import source_definition

    return int(source_definition("google_ads").get("stale_after_days") or 45)


def phrase_evidence(metrics, *, phrase: str, observed_at: datetime) -> dict:
    """1 つの語の Google Ads の証拠 (保存用)。値が無い項目は None のまま。"""

    from app.keyword.providers.google_ads import GOOGLE_ADS_SOURCE_REFERENCE

    base = {"observed_at": observed_at.isoformat(), "source_reference":
            GOOGLE_ADS_SOURCE_REFERENCE, "returned": metrics is not None}
    if metrics is None:
        return {**base, "search_volume_evidence": sd.MISSING,
                "note": "Google Ads returned no historical metrics for this phrase"}
    months = [v.monthly_searches for v in metrics.monthly_search_volumes or ()]
    ci = calculate_commercial_intent(
        keyword=phrase, low_top_of_page_bid_micros=metrics.low_top_of_page_bid_micros,
        competition_index=metrics.competition_index, competition=metrics.competition,
        high_top_of_page_bid_micros=metrics.high_top_of_page_bid_micros)
    return {**base,
            "avg_monthly_searches": metrics.avg_monthly_searches,
            "monthly_search_volumes": [{"year": v.year, "month": v.month,
                                        "monthly_searches": v.monthly_searches}
                                       for v in metrics.monthly_search_volumes or ()],
            "competition": metrics.competition, "competition_index": metrics.competition_index,
            "low_top_of_page_bid_micros": metrics.low_top_of_page_bid_micros,
            "high_top_of_page_bid_micros": metrics.high_top_of_page_bid_micros,
            "search_volume_evidence": sd.search_volume_evidence(metrics.avg_monthly_searches,
                                                                months),
            "search_demand": sd.normalize_search_demand_v2(metrics.avg_monthly_searches, months),
            "commercial_intent": ci.score, "market_evidence_state": ci.market_evidence_state,
            "quality_flags": list(ci.quality_flags),
            "normalizers": {"search_demand": sd.NORMALIZER_VERSION,
                            "commercial_intent": ci.normalizer_version}}


class GoogleAdsRefreshService:
    def __init__(self, session: Session, *, settings=None, collection=None,
                 policy: dict | None = None) -> None:  # fmt: skip
        self._session = session
        if settings is None:
            from app.config.settings import get_settings

            settings = get_settings()
        self._settings = settings
        self._collection = collection
        if policy is None:
            from app.services.nightly_analysis_service import load_policy

            policy = load_policy()
        self._per_call = int(((policy.get("refresh") or {}).get("google_ads") or {})
                             .get("max_keywords_per_call") or 1000)

    def plan(self, *, now: datetime | None = None) -> dict:
        from app.services.nightly_analysis_service import tables_ready

        now = ensure_aware(now or datetime.now(UTC))
        with_metrics = {r.keyword_id for r in self._session.scalars(select(KeywordSignal).where(
            KeywordSignal.provider == "google_ads",
            KeywordSignal.component == "commercial_intent"))}
        keywords = [(k.id, k.keyword) for k in self._session.scalars(
            select(Keyword).order_by(Keyword.id)) if k.id not in with_metrics
            and str(k.status) != "rejected"]
        phrases: list[tuple[int, str]] = []
        if tables_ready(self._session):
            limit = now - timedelta(days=_stale_days())
            for row in self._session.scalars(select(ContentDiscoveryCandidate).where(
                    ContentDiscoveryCandidate.status == CDC_TRACKED)
                    .order_by(ContentDiscoveryCandidate.id)):
                ads = (row.evidence_json or {}).get("google_ads") or {}
                seen = ads.get("observed_at")
                if seen and datetime.fromisoformat(seen) >= limit:
                    continue  # 取ったばかり (返ってこなかった語も含む)
                phrases.append((row.id, row.phrase))
        self._session.rollback()
        total = len(keywords) + len(phrases)
        return {"keywords": [{"keyword_id": i, "keyword": t} for i, t in keywords],
                "discovery_phrases": [{"candidate_id": i, "phrase": t} for i, t in phrases],
                "terms": total, "max_keywords_per_call": self._per_call,
                "calls_if_run": math.ceil(total / self._per_call) if total else 0,
                "discovery_table_ready": tables_ready(self._session),
                "creates_keywords": False}

    def execute(self, *, expect_terms: int, now: datetime | None = None) -> dict:
        from app.keyword.providers.google_ads import GoogleAdsKeywordMetricsProvider
        from app.services.keyword_metrics_collection_service import (
            GOOGLE_ADS_BUNDLE_COMPONENTS,
            KeywordMetricsCollectionService,
        )

        now = ensure_aware(now or datetime.now(UTC))
        plan = self.plan(now=now)
        if not plan["discovery_table_ready"]:
            raise GoogleAdsRefreshError("content_discovery_candidates is missing (migration "
                                        "c1d0e233e180); nothing was called")
        if plan["terms"] != expect_terms:
            raise GoogleAdsRefreshError(f"the plan has {plan['terms']} term(s); expected "
                                        f"{expect_terms}; nothing was called")
        if not plan["terms"]:
            return {"executed": False, "reason": "nothing to refresh", "provider_calls": 0}
        collection = self._collection or KeywordMetricsCollectionService(
            self._session, settings=self._settings,
            provider=GoogleAdsKeywordMetricsProvider(self._settings))
        keyword_items = [(k["keyword_id"], GOOGLE_ADS_BUNDLE_COMPONENTS)
                         for k in plan["keywords"]]
        phrase_items = plan["discovery_phrases"]
        results, phrase_metrics, calls = [], {}, 0
        items = [("k", k) for k in keyword_items] + [("p", p) for p in phrase_items]
        for start in range(0, len(items), self._per_call):
            chunk = items[start:start + self._per_call]
            r, m = collection.collect_google_ads_signals_bulk_with_phrases(
                [c for kind, c in chunk if kind == "k"],
                [c["phrase"] for kind, c in chunk if kind == "p"])
            calls += 1
            results += r
            phrase_metrics.update(m)
        states: dict[str, int] = {}
        for p in phrase_items:
            row = self._session.get(ContentDiscoveryCandidate, p["candidate_id"])
            evidence = phrase_evidence(phrase_metrics.get(p["phrase"]), phrase=p["phrase"],
                                       observed_at=now)
            row.evidence_json = {**(row.evidence_json or {}), "google_ads": evidence}
            row.refresh_needs_json = [n for n in (row.refresh_needs_json or [])
                                      if n != "google_ads"]
            row.updated_at = now
            key = evidence["search_volume_evidence"] if evidence["returned"] else "not_returned"
            states[key] = states.get(key, 0) + 1
        self._session.commit()
        keyword_created = {r.keyword_id: sorted(r.created) for r in results}
        keyword_skipped = {r.keyword_id: dict(r.skipped) for r in results if r.skipped}
        return {"executed": True, "provider_calls": calls, "requested_terms": plan["terms"],
                "keywords": {"requested": len(keyword_items),
                             "signals_created": sum(len(v) for v in keyword_created.values()),
                             "created": keyword_created, "skipped": keyword_skipped},
                "discovery_phrases": {"requested": len(phrase_items), "evidence": states},
                "keywords_created": 0, "articles_created": 0}


__all__ = ["GoogleAdsRefreshError", "GoogleAdsRefreshService", "phrase_evidence"]
