"""分析の出所・成分の健康診断 (C10-A)。**読むだけ・外に問い合わせない。**

    uv run python scripts/analyze_signal_health.py                 # 出所の一覧
    uv run python scripts/analyze_signal_health.py --section all
    uv run python scripts/analyze_signal_health.py --section keywords|articles|attribution|refresh
    uv run python scripts/analyze_signal_health.py --format json

出所 (Google Ads / GSC / GA4 / クリック / 成果 / Threads / URL Inspection) ごとに、最後の観測・
data-through・鮮度・欠けている理由・提供元・影響を受ける件数を出す。keyword と記事の成分は
別々に (1 つの合成の点数は作らない)。``refresh`` は、夜の分析 (C10-C) が外の取り込みを
提供元ごとにまとめて計画するための一覧 (ここでは呼ばない)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis import evidence_contract as ec  # noqa: E402

SECTIONS = ("sources", "keywords", "articles", "attribution", "refresh", "all")


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--section", choices=SECTIONS, default="sources")
    parser.add_argument("--as-of", dest="as_of")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    wanted = SECTIONS[:-1] if args.section == "all" else (args.section,)
    payload: dict = {"as_of": now.isoformat()}
    with session_factory() as session:
        from app.services.analysis_evidence_service import AnalysisEvidenceService
        from app.services.attribution_readiness_service import AttributionReadinessService
        from app.services.source_health_service import SourceHealthService

        if "sources" in wanted:
            payload["sources"] = {k: v.as_dict() for k, v in SourceHealthService(
                session, settings=settings).collect(now=now).items()}
        evidence = AnalysisEvidenceService(session, settings=settings)
        keywords = evidence.keywords(now=now) if {"keywords", "refresh"} & set(wanted) else []
        if "keywords" in wanted:
            payload["keywords"] = {"components": ec.component_summary(keywords),
                                   "candidates": [c.as_dict() for c in keywords]}
        if "articles" in wanted:
            articles = evidence.articles(now=now)
            payload["articles"] = {"components": ec.component_summary(articles),
                                   "candidates": [c.as_dict() for c in articles]}
        if "attribution" in wanted:
            payload["attribution"] = AttributionReadinessService(session).report()
        if "refresh" in wanted:
            payload["refresh"] = ec.refresh_plan(keywords)
        session.rollback()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(payload))
    print("read-only: database writes = 0, external calls = 0")
    return 0


def _v(value) -> str:
    return "—" if value in (None, "", [], {}) else str(value)


def render(payload: dict) -> str:
    lines = [f"signal health — as of {payload['as_of']}"]
    if "sources" in payload:
        lines += ["", "sources:"]
        for name, s in payload["sources"].items():
            lines.append(f"  {name:<17} {s['freshness_state']:<14} provider {s['provider']}; "
                         f"observed {_v(s['observed_at'])}; data-through "
                         f"{_v(s['data_through'])}; lag {s['expected_lag_days']}d")
            if s["missing_reason"]:
                lines.append(f"    reason: {s['missing_reason']}")
            if s["quality_flags"]:
                lines.append(f"    flags: {', '.join(s['quality_flags'])}")
            if s["affected"]:
                lines.append(f"    affected: {s['affected']}")
    for section in ("keywords", "articles"):
        if section in payload:
            lines += ["", f"{section} (components kept separate; no composite score):"]
            for name, counts in payload[section]["components"].items():
                lines.append(f"  {name:<22} " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if "attribution" in payload:
        a = payload["attribution"]
        lines += ["", "attribution readiness (nothing is allocated to articles):",
                  f"  clicks: {a['clicks']}", f"  commissions: {a['commissions']}",
                  f"  summary: {a['summary']}"]
        for p in a["programs"]:
            if p["has_tracking_url"] or p["click_attribution_level"] != "D_unattributed":
                lines.append(f"  program {p['program_id']} ({p['provider']}): clicks "
                             f"{p['click_attribution_level']}, commissions "
                             f"{p['commission_attribution_level']}; deterministic join "
                             f"{p['deterministic_join_possible']}")
        lines.append("  C11 needs (human decisions): " + " | ".join(a["c11_requirements"][:3]))
    if "refresh" in payload:
        lines += ["", "external refresh plan (not called; batched per provider):"]
        for source, plan in payload["refresh"].items():
            lines.append(f"  {source}: {len(plan['subjects'])} subject(s); batched "
                         f"{plan['batched']}; calls if run {plan['calls_if_run']}")
        if not payload["refresh"]:
            lines.append("  nothing needs an external refresh")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
