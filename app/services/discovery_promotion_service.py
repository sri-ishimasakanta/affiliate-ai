"""発見の候補を Keyword にする (C10-3 / C10-E)。**人が 1 件ずつ決める。**

発見の候補 ≠ Keyword。夜の分析は候補を Keyword にしない。ここは人が選んだ 1 件だけを Keyword に
する (まとめて全部を承認する入口は無い)。

- ``plan(limit)``: 意味のある候補を 3〜5 件だけ示す (C9-A と同じく少数・理由つき・1 つの点数を
  作らない)。順: gap を埋める → 検索の量の観測がある → 検索の表示がある → その他。重なり
  (cannibalization) で止まる候補・既に Keyword の候補は出さない。各候補の
  ``promotion_fingerprint`` (語・証拠・重なりの状態から) を出す。
- ``promote(candidate_id, expected_fingerprint)``: 指紋が今と同じで、同じ語の Keyword が無く、
  重なりが無いときだけ、既存の ``KeywordService.create_keyword`` で Keyword を作る。候補は
  ``promoted`` にし、そのときの証拠を ``evidence_json["promotion"]`` に固定する。保存済みの
  Google Ads の値があれば、それから search_demand / commercial_intent の signal を作る (元の
  観測の時刻のまま。Google Ads を呼び直さない)。記事・計画の依頼・Growth の承認は作らない。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.content.text import phrase_key, stable_hash
from app.models import Keyword, KeywordSignal
from app.models.content_discovery import CDC_PROMOTED, CDC_TRACKED, ContentDiscoveryCandidate

DEFAULT_LIMIT = 5


class DiscoveryPromotionError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DiscoveryPromotionService:
    def __init__(self, session: Session, *, settings=None, intelligence=None) -> None:
        self._session = session
        self._settings = settings
        self._intelligence = intelligence

    def _candidates(self, now: datetime) -> list[dict]:
        from app.services.content_intelligence_service import ContentIntelligenceService

        service = self._intelligence or ContentIntelligenceService(self._session,
                                                                   settings=self._settings)
        ci = service.build(now=now)
        rows = {r.phrase_key: r for r in self._session.scalars(select(ContentDiscoveryCandidate))}
        out = []
        for item in ci.next_articles:
            if item.keyword_id is not None or not item.discovery_key:
                continue
            row = rows.get(item.discovery_key)
            if row is None or row.status != CDC_TRACKED:
                continue
            ads = (row.evidence_json or {}).get("google_ads") or {}
            view = {"candidate_id": row.id, "phrase": row.phrase, "phrase_key": row.phrase_key,
                    "cluster_id": item.cluster_id, "gaps": list(item.gaps_filled),
                    "content_type": item.content_type.get("role"),
                    "monetization_role": item.monetization_role,
                    "cannibalization": list(item.cannibalization),
                    "google_ads": {k: ads.get(k) for k in (
                        "observed_at", "avg_monthly_searches", "search_volume_evidence",
                        "search_demand", "commercial_intent", "market_evidence_state")},
                    "gsc": {k: (row.evidence_json or {}).get(k) for k in (
                        "gsc_impressions", "gsc_clicks")},
                    "sources": sorted({s.get("source") for s in row.sources_json or []}),
                    "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at
                    else None, "seen_count": row.seen_count}  # fmt: skip
            view["promotion_fingerprint"] = stable_hash({
                "phrase_key": row.phrase_key, "cluster": view["cluster_id"],
                "gaps": view["gaps"], "google_ads": view["google_ads"],
                "cannibalization": [(h.get("kind"), h.get("ref")) for h in
                                    view["cannibalization"]]})
            out.append(view)
        return out

    @staticmethod
    def _tier(view: dict) -> tuple[int, str]:
        ads = view["google_ads"]
        if view["gaps"]:
            return 0, "fills " + ", ".join(view["gaps"][:2])
        if ads.get("search_volume_evidence") == "observed":
            return 1, f"Google Ads average {ads.get('avg_monthly_searches')} searches / month"
        if (view["gsc"].get("gsc_impressions") or 0) > 0:
            return 2, f"{view['gsc']['gsc_impressions']} Search Console impression(s)"
        return 3, "discovered term (config / seed)"

    def plan(self, *, now: datetime | None = None, limit: int = DEFAULT_LIMIT) -> dict:
        now = ensure_aware(now or datetime.now(UTC))
        views = self._candidates(now)
        eligible = [v for v in views if not v["cannibalization"]]
        blocked = [v for v in views if v["cannibalization"]]
        ranked = sorted(eligible, key=lambda v: (self._tier(v)[0],
                                                 -(v["google_ads"].get("avg_monthly_searches")
                                                   or 0), v["phrase_key"]))  # fmt: skip
        # 弱い多様さ: まずクラスタごとに一番よい候補、残りは全体の順 (C9-A と同じ考え)。
        limit = max(1, min(limit, DEFAULT_LIMIT))
        firsts, seen = [], set()
        for v in ranked:
            if v["cluster_id"] not in seen:
                firsts.append(v)
                seen.add(v["cluster_id"])
        picked = (firsts + [v for v in ranked if v not in firsts])[:limit]
        shown = []
        for v in picked:
            shown.append({**v, "why": self._tier(v)[1],
                          "command": f"manage_discovery_candidates.py promote {v['candidate_id']}"
                                     f" --fingerprint {v['promotion_fingerprint']} --execute"})
        self._session.rollback()
        return {"as_of": now.isoformat(), "shown": shown, "tracked": len(views),
                "eligible": len(eligible), "blocked_by_cannibalization": len(blocked),
                "not_shown": max(0, len(eligible) - len(shown)),
                "notes": ["individual review only: no approve-all",
                          "promotion creates a Keyword only; no article, planning request or "
                          "Growth approval", "composite score: none"]}

    def promote(self, candidate_id: int, *, expected_fingerprint: str,
                executed_by: str = "human", now: datetime | None = None) -> dict:
        from app.keyword.schemas import KeywordCreate
        from app.services.keyword_service import KeywordService

        now = ensure_aware(now or datetime.now(UTC))
        row = self._session.get(ContentDiscoveryCandidate, candidate_id)
        if row is None:
            raise DiscoveryPromotionError(f"discovery candidate {candidate_id} does not exist")
        if row.status != CDC_TRACKED:
            raise DiscoveryPromotionError(f"candidate {candidate_id} is {row.status}")
        view = next((v for v in self._candidates(now) if v["candidate_id"] == candidate_id),
                    None)
        if view is None:
            raise DiscoveryPromotionError("the candidate is no longer a new term in the current "
                                          "analysis (duplicate or overlap)")
        if view["promotion_fingerprint"] != expected_fingerprint:
            raise DiscoveryPromotionError("fingerprint mismatch: the evidence changed since the "
                                          "plan; re-run the plan")
        if view["cannibalization"]:
            raise DiscoveryPromotionError("cannibalization: " + "; ".join(
                f"{h['rule']} with {h['ref']}" for h in view["cannibalization"][:3]))
        existing = [k for k in self._session.scalars(select(Keyword))
                    if phrase_key(k.keyword) == row.phrase_key]
        if existing:
            raise DiscoveryPromotionError(f"keyword {existing[0].id} already has this phrase")
        created = KeywordService(self._session).create_keyword(KeywordCreate(keyword=row.phrase))
        signals = self._signals_from_cache(created.id, row)
        row = self._session.get(ContentDiscoveryCandidate, candidate_id)
        row.status, row.keyword_id, row.updated_at = CDC_PROMOTED, created.id, now
        row.evidence_json = {**(row.evidence_json or {}), "promotion": {
            "promoted_at": now.isoformat(), "by": executed_by,
            "fingerprint": expected_fingerprint, "cluster_id": view["cluster_id"],
            "gaps": view["gaps"], "content_type": view["content_type"],
            "keyword_id": created.id, "signals_from_cache": signals}}
        self._session.commit()
        return {"promoted": True, "keyword_id": created.id, "phrase": row.phrase,
                "signals_from_cache": signals, "articles_created": 0,
                "external_calls": 0}

    def _signals_from_cache(self, keyword_id: int, row: ContentDiscoveryCandidate) -> list[str]:
        """保存済みの Google Ads の値から signal を作る (呼び直さない)。値が無ければ作らない。"""

        ads = (row.evidence_json or {}).get("google_ads") or {}
        if not ads.get("returned") or not ads.get("observed_at"):
            return []
        observed = datetime.fromisoformat(ads["observed_at"])
        raw = {k: ads.get(k) for k in (
            "avg_monthly_searches", "monthly_search_volumes", "competition",
            "competition_index", "low_top_of_page_bid_micros", "high_top_of_page_bid_micros",
            "search_volume_evidence", "market_evidence_state", "quality_flags")}
        made = []
        for component, value in (("search_demand", ads.get("search_demand")),
                                 ("commercial_intent", ads.get("commercial_intent"))):
            if value is None:
                continue  # 欠測は signal にしない
            self._session.add(KeywordSignal(
                keyword_id=keyword_id, component=component, normalized_value=float(value),
                provider="google_ads", observed_at=observed, source_reference=ads.get(
                    "source_reference"),
                raw_data={**raw, "normalizer": {"name": component, "version": (
                    ads.get("normalizers") or {}).get(component)},
                    "derived_from": f"content_discovery_candidates:{row.id}",
                    "rederived_without_external_call": True}))
            made.append(component)
        return made


__all__ = ["DiscoveryPromotionError", "DiscoveryPromotionService"]
