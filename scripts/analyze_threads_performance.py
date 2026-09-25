"""管理用 CLI: Threads の投稿の成績を、同じ経過時間で比べる診断 (**読むだけ**)。

    uv run python scripts/analyze_threads_performance.py
    uv run python scripts/analyze_threads_performance.py --as-of 2026-09-25T21:00:00+09:00
    uv run python scripts/analyze_threads_performance.py --output-dir D:/tmp/reports

保存済みの観測だけを使う。DB にも Threads にも書かず、Threads には問い合わせもしない
(観測を取り直さない)。出力は ``reports/threads_performance_diagnostic_latest.{json,md}``
(``reports/`` は git 管理外の実行時の成果物)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.threads_performance_service import ThreadsPerformanceService  # noqa: E402
from app.social.threads.performance import CHECKPOINT_HOURS  # noqa: E402

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "reports"
BASENAME = "threads_performance_diagnostic_latest"


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", dest="as_of", help="分析の時刻 (ISO 8601。既定は現在)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true", help="JSON を標準出力にも出す")
    args = parser.parse_args(argv)

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    with session_factory() as session:
        report = ThreadsPerformanceService(session, settings=settings).report(as_of=as_of)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{BASENAME}.json"
    md_path = args.output_dir / f"{BASENAME}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    md_path.write_text(render_markdown(report), "utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"status = {report['diagnostic']['status']}")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print("read-only: database writes = 0, Threads calls = 0")
    return 0


def _fmt(value) -> str:
    return "—" if value is None else str(value)


def render_markdown(report: dict) -> str:
    pubs = report["publications"]
    summary = report["checkpoint_summary"]
    diag = report["diagnostic"]
    lines = [
        "# Threads performance diagnostic (age-normalized, read-only)",
        "",
        f"- as of: {report['as_of_local']} ({report['local_timezone']})",
        f"- data cutoff (latest stored observation): {_fmt(report['data_cutoff_local'])}",
        f"- publications: {report['publication_count']}",
        f"- schema: `{report['schema_version']}`",
        f"- **diagnostic status: `{diag['status']}`**",
        "",
        "Missing or non-comparable values are shown as `—`, never as 0.",
        "",
        "## 1. Raw views (NOT comparable: different ages)",
        "",
        "| publication | published (JST) | age now (h) | latest obs age (h) | latest views |",
        "|---|---|---|---|---|",
    ]
    for p in pubs:
        latest = p["latest"] or {}
        latest_views = (latest.get("metrics") or {}).get("views")
        lines.append(
            f"| #{p['publication_id']} | {p['published_at_local']} | {p['age_at_as_of_hours']} "
            f"| {_fmt(latest.get('age_hours'))} | {_fmt(latest_views)} |"
        )
    header = " | ".join(f"views@{h}h" for h in CHECKPOINT_HOURS)
    lines += [
        "",
        "## 2. Views at equal age since publication",
        "",
        f"| publication | published (JST) | angle | link | chars | {header} |",
        "|---|---|---|---|---|" + "---|" * len(CHECKPOINT_HOURS),
    ]
    for p in pubs:
        cells = []
        for h in CHECKPOINT_HOURS:
            c = p["checkpoints"][f"{h}h"]
            views = (c["metrics"] or {}).get("views") if c["comparable"] else None
            cells.append(f"{views} ({c['delta_minutes']:+.0f}m)" if views is not None else "—")
        lines.append(
            f"| #{p['publication_id']} | {p['published_at_local']} | {_fmt(p['angle'])} | "
            f"{_fmt(p['link_mode'])} | {_fmt(p['character_count'])} | " + " | ".join(cells) + " |"
        )
    lines += [
        "",
        "(`+Nm` = the matched real observation was N minutes after the checkpoint.)",
        "",
        "## 3. Checkpoints with enough comparable data",
        "",
        "| checkpoint | tolerance | comparable | old enough | status | confidence | evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, s in summary.items():
        c = s["classification"]
        seq = ", ".join(f"#{x['publication_id']}={x['views']}" for x in c["sequence"]) or "—"
        lines.append(
            f"| {key} | ±{s['tolerance_minutes']:.0f} min | {s['comparable_count']} | "
            f"{s['eligible_count']} | `{c['status']}` | {c['confidence']} | {seq}; {c['reason']} |"
        )
    lines += [
        "",
        "Relative values at each checkpoint (vs the previous comparable post / vs the median "
        "of earlier comparable posts):",
        "",
    ]
    for h in CHECKPOINT_HOURS:
        cells = [
            f"#{p['publication_id']}: {_fmt(p['checkpoints'][f'{h}h']['vs_previous_pct'])}% / "
            f"{_fmt(p['checkpoints'][f'{h}h']['vs_earlier_median'])}x"
            for p in pubs
            if p["checkpoints"][f"{h}h"]["comparable"]
        ]
        if cells:
            lines.append(f"- {h}h: " + "; ".join(cells))
    lines += ["", "## 4. Early distribution vs later flattening (views gained)", ""]
    for label, v in report["velocity_summary"].items():
        vals = (
            ", ".join(
                f"#{x['publication_id']}: +{x['views_gained']} ({x['views_per_hour']}/h)"
                for x in v["values"]
            )
            or "unavailable"
        )
        lines.append(f"- {label}: {vals}")
    dims = report["dimension_summary"]
    lines += ["", "## 5. Link vs no-link (descriptive, no causal claim)", ""]
    lines += _dimension_lines(dims["link_mode"])
    lines += ["", "## 6. Angle / topic / time / length (small samples)", ""]
    for name in ("angle", "topic", "time_bucket", "length_band"):
        lines.append(f"**{name}**")
        lines += _dimension_lines(dims[name])
        lines.append("")
    lines += [
        "## 7. Engagement (interaction metrics, separate from reach)",
        "",
        "Ratios are shown only when views reach "
        f"{report['checkpoint_policy']['ratio_minimum_views']}; otherwise raw counts only.",
        "",
        "| publication | latest views | likes | replies | reposts | quotes | shares | "
        "engagements/view |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for p in pubs:
        m = (p["latest"] or {}).get("metrics") or {}
        lines.append(
            f"| #{p['publication_id']} | {_fmt(m.get('views'))} | {_fmt(m.get('likes'))} | "
            f"{_fmt(m.get('replies'))} | {_fmt(m.get('reposts'))} | {_fmt(m.get('quotes'))} | "
            f"{_fmt(m.get('shares'))} | {_fmt((p['latest'] or {}).get('engagements_per_view'))} |"
        )
    sim = report["similarity"]
    lines += [
        "",
        "## 8. Content repetition / sequence",
        "",
        f"- angle sequence: {' → '.join(_fmt(a) for a in sim['angle_sequence'])}",
    ]
    for pair in sim["consecutive"]:
        lines.append(
            f"- #{pair['publication_ids'][0]}→#{pair['publication_ids'][1]}: "
            f"bigram Jaccard {_fmt(pair['bigram_jaccard'])}, same angle {pair['same_angle']}, "
            f"same topic {pair['same_topic']}"
        )
    phrases = ", ".join(f"「{x['phrase']}」{x['publication_ids']}" for x in sim["shared_phrases"])
    lines.append(f"- shared 10-character phrases: {phrases or 'none'}")
    untracked = report["untracked_remote_posts"]
    lines += [
        "",
        "## 9. Manual / untracked posts",
        "",
        f"- detection: **{untracked.get('status')}** — {untracked.get('reason', '')}",
        f"- internal publications last read as not found (404): "
        f"{untracked.get('internal_not_resolvable') or 'none'}",
        "",
        "## 10. What cannot be concluded yet",
        "",
    ]
    lines += [f"- {note}" for note in diag["limitations"]]
    lines += ["", "## Next decision", "", _next_decision(diag["status"]), ""]
    lines.append(
        "This report changes nothing: no generation weights, learning, posting times or "
        "frequency are adjusted from it."
    )
    return "\n".join(lines) + "\n"


def _dimension_lines(groups: dict) -> list[str]:
    out = []
    for value, g in groups.items():
        cps = "; ".join(
            f"{k}: n={c['comparable']} median={_fmt(c['median_views'])}"
            for k, c in g["checkpoints"].items()
        )
        small = " (small sample)" if g["small_sample"] else ""
        out.append(f"- `{value}` posts {g['publications']}{small}: {cps}")
    return out


def _next_decision(status: str) -> str:
    return {
        "insufficient_data": "Insufficient sample — keep collecting observations at the normal "
        "cadence before drawing any conclusion.",
        "no_clear_decline": "No clear age-normalized decline — no action; re-run after more posts.",
        "mixed": "Mixed signals across checkpoints — collect more comparable posts; inspect the "
        "content mix (angle/topic/link) before changing anything in generation.",
        "directional_decline_signal": "Directional decline signal — inspect the content mix and "
        "posting conditions before changing generation; do not change frequency or timing from "
        "this alone.",
    }.get(status, "Review manually.")


if __name__ == "__main__":
    raise SystemExit(main())
