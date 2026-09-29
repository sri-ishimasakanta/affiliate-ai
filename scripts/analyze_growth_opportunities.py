"""成長の証拠と、次の行動の候補 (C9、**読むだけ**)。

DB に書かない・外の API に問い合わせない・WordPress / Threads に書かない・メールを送らない。
ファイルも書かない (標準出力だけ)。候補は **候補** で、何も実行しない。

    uv run python scripts/analyze_growth_opportunities.py
    uv run python scripts/analyze_growth_opportunities.py --format json
    uv run python scripts/analyze_growth_opportunities.py --action-type review_affiliate_placement
    uv run python scripts/analyze_growth_opportunities.py --article-id 10 --format json
    uv run python scripts/analyze_growth_opportunities.py --monetized-only
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.growth.analysis import ACTION_TYPES, EVIDENCE_STATES  # noqa: E402
from app.services.growth_opportunity_service import (  # noqa: E402
    GrowthOpportunityService,
    filter_candidates,
)


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", dest="as_of", help="評価の時刻 (ISO 8601。既定は現在)")
    parser.add_argument("--days", type=int, default=28, help="計測の期間 (日)")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--article-id", type=int, dest="article_id")
    parser.add_argument("--keyword-id", type=int, dest="keyword_id")
    parser.add_argument("--action-type", choices=ACTION_TYPES, dest="action_type")
    parser.add_argument("--evidence-state", choices=EVIDENCE_STATES, dest="evidence_state")
    parser.add_argument("--min-age-days", type=int, dest="min_age_days",
                        help="公開からの日数の下限")  # fmt: skip
    parser.add_argument("--max-age-days", type=int, dest="max_age_days",
                        help="公開からの日数の上限")  # fmt: skip
    parser.add_argument("--monetized-only", action="store_true", dest="monetized_only")
    parser.add_argument("--limit", type=int, default=40, help="表に出す候補の数 (table のみ)")
    args = parser.parse_args(argv)

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    with session_factory() as session:
        report = GrowthOpportunityService(session, settings=settings).evaluate_growth_opportunities(
            as_of, days=args.days)  # fmt: skip
        session.rollback()
    candidates = filter_candidates(
        report, article_id=args.article_id, keyword_id=args.keyword_id,
        action_type=args.action_type, evidence_state=args.evidence_state,
        min_age_days=args.min_age_days, max_age_days=args.max_age_days,
        monetized_only=args.monetized_only,
    )  # fmt: skip
    if args.format == "json":
        evidence = report["evidence"]
        if args.article_id is not None:
            evidence = [e for e in evidence if e["article_id"] == args.article_id]
        if args.keyword_id is not None:
            evidence = [e for e in evidence if e["keyword_id"] == args.keyword_id]
        print(json.dumps({**report, "evidence": evidence, "candidates": candidates},
                         ensure_ascii=False, indent=2, default=str))  # fmt: skip
    else:
        print(render(report, candidates, limit=args.limit))
    print("read-only: database writes = 0, external calls = 0, WordPress writes = 0, "
          "Threads writes = 0, emails = 0, files written = 0")
    return 0


def _v(value) -> str:
    return "—" if value is None else str(value)


def render(report: dict, candidates: list[dict], *, limit: int = 40) -> str:
    s = report["summary"]
    lines = [
        f"Growth opportunities (read-only, {report['schema_version']}) — as of {report['as_of']}",
        f"window: {report['window']['start']} .. {report['window']['end']} "
        f"({report['window']['days']} days); next evaluation {report['next_evaluation_at']}; "
        f"fingerprint {report['fingerprint'][:12]}",
        "",
        "## 1. Summary",
        f"articles {s['article_count']} (published {s['published_article_count']}); "
        f"keywords without an article {s['keyword_without_article_count']}",
        "usable sources (articles): " + ", ".join(f"{k}={v}" for k, v in s["usable"].items()),
        f"trusted affiliate clicks observed on {s['affiliate_trusted_click_observed_articles']} "
        f"article(s); Threads-linked articles {s['threads_linked_articles']}",
        "evidence states: " + ", ".join(f"{k}={v}" for k, v in s["evidence_state_counts"].items()),
        "source freshness: " + ", ".join(f"{k}={v}" for k, v in report["freshness"].items()),
        "candidates by action: " + ", ".join(
            f"{k}={v}" for k, v in s["candidate_counts_by_action"].items()),
        "",
        "## 2. Evidence (articles)",
        "| article | status | age d | monetized | seo | ga4 | affiliate (clean/excl) | threads "
        "(posts/best) | keyword | index | overall |",
        "|" + "---|" * 11,
    ]  # fmt: skip
    for e in report["evidence"]:
        if e["subject_type"] != "article":
            continue
        src = e["sources"]
        aff = src["affiliate"]["metrics"]
        thr = src["threads"]["metrics"]
        lines.append(
            f"| #{e['article_id']} | {e['article'].get('status')} | "
            f"{_v(e['article'].get('age_days'))} | {_v(e['article'].get('monetized'))} | "
            f"{src['seo']['state']} | {src['ga4']['state']} | "
            f"{src['affiliate']['state']} ({_v(aff.get('clean_clicks'))}/"
            f"{_v(aff.get('excluded_instrumentation_clicks'))}) | "
            f"{src['threads']['state']} ({_v(thr.get('publications'))}/"
            f"{_v(src['threads'].get('maturity'))}) | {src['keyword']['state']} | "
            f"{src['index']['state']} | {e['evidence_state']} |"
        )  # fmt: skip
    lines += ["", f"## 3. Action candidates ({len(candidates)} shown of "
                  f"{len(report['candidates'])}; nothing is executed)",
              "| # | action | subject | patterns | evidence | potential | urgency | effort | "
              "monetization | approval | external write | blockers |",
              "|" + "---|" * 12]  # fmt: skip
    for i, c in enumerate(candidates[:limit], 1):
        p = c["priority"]
        lines.append(
            f"| {i} | {c['action_type']} | {c['subject_id']} | {', '.join(c['patterns'])} | "
            f"{p['evidence_strength']['level']} | {p['potential_opportunity']['level']} | "
            f"{p['recency_urgency']['level']} | {p['effort']['level']} | "
            f"{p['monetization_relevance']['level']} | "
            f"{'yes' if c['requires_human_approval'] else 'no'} | "
            f"{_v(c['external_write_required'])} | {'; '.join(c['blockers']) or '—'} |"
        )  # fmt: skip
    lines += ["", "order: evidence strength, potential, monetization relevance, effort (lower "
                  "first), urgency — components are shown, there is no single score"]  # fmt: skip
    lines += ["", "## 4. Data quality / missing data"]
    lines += [f"- {w}" for w in report["warnings"]]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
