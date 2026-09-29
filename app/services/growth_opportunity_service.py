"""GrowthOpportunityService -- 成長の証拠から、次の行動の候補を作る (C9)。**行動はしない。**

- 読むだけ (``GrowthEvidenceService`` と同じ)。DB・WordPress・Threads・メール・外の API に
  書かない・問い合わせない。
- worker に載せるための入口 (``evaluate_growth_opportunities``) と、次に評価する時刻
  (``next_evaluation_at``)・決定的な指紋を持つ。**このバッチでは worker に登録しない。**
  取り込みは 1 日 1 回なので、heartbeat ごとには評価しない (既定 24 時間ごと)。
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.growth import analysis as ga
from app.services.growth_evidence_service import DEFAULT_WINDOW_DAYS, GrowthEvidenceService

#: 次に評価するまでの間隔 (取り込みは 1 日 1 回)。
EVALUATION_INTERVAL = timedelta(hours=24)


class GrowthOpportunityService:
    def __init__(self, session: Session, *, settings=None, timezone=None,
                 evidence_service: GrowthEvidenceService | None = None) -> None:  # fmt: skip
        self._evidence = evidence_service or GrowthEvidenceService(
            session, settings=settings, timezone=timezone)

    def evaluate_growth_opportunities(self, now: datetime | None = None, *,
                                      days: int = DEFAULT_WINDOW_DAYS) -> dict:  # fmt: skip
        """worker から呼べる形の評価 (読むだけ)。"""

        now = ensure_aware(now or datetime.now(UTC))
        bundle = self._evidence.collect(now=now, days=days)
        return build_report(bundle, now=now)


def _angles() -> tuple[str, ...]:
    from app.social.threads.policy import get_policy as get_style_policy

    return tuple(get_style_policy().angles)


def build_report(bundle: dict, *, now: datetime, angles: tuple[str, ...] | None = None) -> dict:
    angles = _angles() if angles is None else angles
    evidence: list[ga.GrowthEvidence] = bundle["evidence"]
    candidates: list[ga.GrowthActionCandidate] = []
    for item in evidence:
        context = bundle["keyword_context"].get(item.keyword_id, {}) if (
            item.subject_type == "keyword") else {}  # fmt: skip
        candidates += ga.build_candidates(
            item, ga.classify(item, angles=angles,
                              recent_angles=bundle.get("recent_regular_angles") or ()),
            context=context)  # fmt: skip
    candidates += ga.site_candidates(data_quality=bundle["data_quality"],
                                     growth_plan=bundle["growth_plan"],
                                     freshness=bundle["freshness"])  # fmt: skip
    ordered = ga.sort_candidates(candidates)
    articles = [e for e in evidence if e.subject_type == "article"]

    def count(source: str, state: str = ga.USABLE) -> int:
        return sum(1 for e in articles
                   if e.sources.get(source) and e.sources[source].state == state)  # fmt: skip

    summary = {
        "article_count": len(articles),
        "published_article_count": sum(1 for e in articles
                                       if e.article.get("status") == "published"),
        "keyword_without_article_count": sum(1 for e in evidence if e.subject_type == "keyword"),
        "usable": {s: count(s) for s in ("seo", "ga4", "affiliate", "threads", "index")},
        "affiliate_trusted_click_observed_articles": sum(
            1 for e in articles
            if (e.sources["affiliate"].metrics.get("clean_clicks") or 0) > 0),
        "threads_linked_articles": sum(
            1 for e in articles if (e.sources["threads"].metrics.get("publications") or 0) > 0),
        "evidence_state_counts": dict(sorted(Counter(e.evidence_state for e in articles).items())),
        "missing_or_stale_sources": {k: v for k, v in sorted(bundle["freshness"].items())
                                     if v in (ga.UNAVAILABLE, ga.STALE)},
        "candidate_counts_by_action": dict(sorted(Counter(c.action_type for c in ordered).items())),
        "candidate_counts_by_pattern": dict(sorted(Counter(
            p for c in ordered for p in c.patterns).items())),
        "candidate_counts_by_evidence_state": dict(sorted(Counter(
            c.evidence_state for c in ordered).items())),
    }  # fmt: skip
    warnings = []
    for source, state in sorted(bundle["freshness"].items()):
        if state in (ga.UNAVAILABLE, ga.STALE):
            warnings.append(f"{source}: {state}")
    clicks = bundle["clicks"]
    if clicks.get("excluded_instrumentation"):
        warnings.append(f"{clicks['excluded_instrumentation']} affiliate click(s) before the "
                        "trusted measurement start are excluded from reader behaviour")
    if not bundle["program_commissions"]:
        warnings.append("no commission data; article-level revenue is not available")
    warnings.append("commissions are program-level; no article-level revenue is derived")
    if summary["evidence_state_counts"].get(ga.EVIDENCE_INSUFFICIENT):
        warnings.append(f"{summary['evidence_state_counts'][ga.EVIDENCE_INSUFFICIENT]} "
                        "article(s) have no usable reader-behaviour source yet")
    fp = ga.fingerprint(ordered)
    return {
        "schema_version": ga.SCHEMA_VERSION,
        "as_of": now.isoformat(),
        "read_only": True,
        "window": bundle["window"],
        "freshness": bundle["freshness"],
        "freshness_detail": bundle["freshness_detail"],
        "ga4_configured": bundle["ga4_configured"],
        "trusted_click_measurement_start_at": bundle["trusted_click_measurement_start_at"],
        "clicks": clicks,
        "program_commissions": bundle["program_commissions"],
        "unattributed_commissions": bundle["unattributed_commissions"],
        "index_observed_at": bundle["index_observed_at"],
        "growth_plan": bundle["growth_plan"],
        "recent_regular_angles": bundle.get("recent_regular_angles") or [],
        "summary": summary,
        "evidence": [e.as_dict() for e in evidence],
        "candidates": [c.as_dict() for c in ordered],
        "warnings": warnings,
        "limitations": [
            "candidates only; nothing is executed, published or written",
            "descriptive: observed situations and actions worth trying, not causes or "
            "guaranteed effects",
            "no single score: priority components are kept separate and shown",
            "the keyword opportunity score is used only relative to other keywords",
        ],
        "fingerprint": fp,
        "next_evaluation_at": (now + EVALUATION_INTERVAL).isoformat(),
        "side_effects": {"db_writes": 0, "external_calls": 0, "wordpress_writes": 0,
                         "threads_writes": 0, "emails": 0},
    }


def filter_candidates(report: dict, *, article_id: int | None = None,
                      keyword_id: int | None = None, action_type: str | None = None,
                      evidence_state: str | None = None, min_age_days: int | None = None,
                      max_age_days: int | None = None, monetized_only: bool = False) -> list[dict]:
    """表示の絞り込み (分析そのものは全体で作る)。"""

    ages = {e["article_id"]: e["article"].get("age_days") for e in report["evidence"]
            if e["subject_type"] == "article"}  # fmt: skip
    monetized = {e["article_id"]: bool(e["article"].get("monetized")) for e in report["evidence"]
                 if e["subject_type"] == "article"}  # fmt: skip
    out = []
    for c in report["candidates"]:
        if article_id is not None and c["article_id"] != article_id:
            continue
        if keyword_id is not None and c["keyword_id"] != keyword_id:
            continue
        if action_type and c["action_type"] != action_type:
            continue
        if evidence_state and c["evidence_state"] != evidence_state:
            continue
        age = ages.get(c["article_id"])
        if min_age_days is not None and (age is None or age < min_age_days):
            continue
        if max_age_days is not None and (age is None or age > max_age_days):
            continue
        if monetized_only and not monetized.get(c["article_id"]):
            continue
        out.append(c)
    return out


__all__ = ["EVALUATION_INTERVAL", "GrowthOpportunityService", "build_report",
           "filter_candidates"]
