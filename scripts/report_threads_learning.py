"""管理用 CLI: Threads の学習結果を出す (T5、**読むだけ**)。

    # 現時点の学習結果 (人が読む形)
    uv run python scripts/report_threads_learning.py

    # 機械可読な形でも書き出す (T5.5 が読む)
    uv run python scripts/report_threads_learning.py --json out/threads-learning.json

    # 比べる基準の時刻を変える (既定は 24 時間前)
    uv run python scripts/report_threads_learning.py --since 2026-09-20T00:00:00+09:00

**DB にも Threads にも書かない。** Meta に問い合わせもしない。メールも送らない。
証拠が足りなければ「足りない」と出す。それが正しい出力である。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.threads_learning_service import (  # noqa: E402
    ThreadsLearningError,
    ThreadsLearningService,
)
from app.social.threads.learning import STATUS_SUFFICIENT  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 2


def main(argv: list[str] | None = None, *, session_factory=None, now=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    parser.add_argument("--since", help="変化を比べる基準の時刻 (ISO 8601。既定は 24 時間前)")
    parser.add_argument("--as-of", dest="as_of", help="分析する時刻 (ISO 8601。既定は現在)")
    args = parser.parse_args(argv)

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else now
    since = datetime.fromisoformat(args.since) if args.since else None

    with session_factory() as session:
        try:
            report = ThreadsLearningService(session).report(now=as_of, since=since)
        except ThreadsLearningError as exc:
            print(f"refused: {exc.reason}")
            return EXIT_REFUSED
    print(format_report(report))
    if args.json_path:
        target = Path(args.json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(to_json(report), encoding="utf-8")
        print(f"\nJSON written to {args.json_path}")
    return EXIT_OK


def to_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def format_report(report: dict) -> str:
    lines: list[str] = []
    out = lines.append
    counts = report["counts"]
    thresholds = report["thresholds"]
    start, end = thresholds["comparable_snapshot_window_hours"]

    out("=== threads learning report (T5, read-only) ===")
    out(f"analysis time    = {report['generated_at_local']} ({report['local_timezone']})")
    out(f"policy_version   = {report['policy_version']}")
    out(f"schema           = {report['schema_version']}")
    out(
        f"publications     = total {counts['total']} / mature {counts['mature']} / "
        f"immature {counts['immature']} / missing comparable data "
        f"{counts['missing_comparable_data']}"
    )
    out(
        f"thresholds       = mature >= {start:g}h, comparable snapshot {start:g}-{end:g}h, "
        f">= {thresholds['minimum_mature_posts_per_value']} mature posts per value, "
        f">= {thresholds['minimum_views_for_rate']} views per post for a rate"
    )
    out(f"overall evidence = {report['evidence_status']}")
    for reason in report["evidence_reasons"]:
        out(f"  reason: {reason}")

    out("\n--- publications ---")
    if not report["publications"]:
        out("  (no published Threads post yet)")
    for post in report["publications"]:
        dims = post["dimensions"]
        out(
            f"  #{post['publication_id']} article {post['article_id']} angle={dims['angle']} "
            f"link={dims['link_mode']} length={dims['length_band']}({post['character_count']}) "
            f"{post['published_at_local']} {dims['weekday']} {dims['daypart']} "
            f"trigger={dims['trigger']}"
        )
        line = (
            f"      age {post['age_hours']}h ({post['maturity_stage']}): {post['evidence_state']}"
        )
        if post["snapshot"]:
            snap = post["snapshot"]
            line += f" / snapshot #{snap['snapshot_id']} at {snap['age_hours']}h"
        out(line)
        if post["metrics"] is not None:
            metrics = " ".join(
                f"{k}={'(missing)' if v is None else v}" for k, v in post["metrics"].items()
            )
            rate = post["interaction_rate"]
            out(f"      {metrics} rate={'(not computed)' if rate is None else rate}")
        elif post["evidence_state"] == "immature":
            out("      not used for learning yet (immature; see the T4 report for raw values)")

    for dimension, data in report["dimensions"].items():
        suffix = " (diagnostic only; never compared)" if data["diagnostic_only"] else ""
        out(f"\n--- {dimension}{suffix} ---")
        if not data["values"]:
            out("  (no value yet)")
        for value in data["values"]:
            c = value["counts"]
            label = f" [{value['label']}]" if value["label"] != value["value"] else ""
            out(
                f"  {value['value']}{label}: {value['status']} "
                f"(posts={c['publications']} mature={c['mature']} comparable={c['comparable']})"
            )
            if value["status"] == STATUS_SUFFICIENT:
                views = value["views"]
                rate = value["interaction_rate"]
                out(
                    f"      views median={views['median']} (min {views['min']}, max "
                    f"{views['max']}, total {views['total']}); interaction rate median="
                    f"{rate['median']} (min {rate['min']}, max {rate['max']}, n={rate['eligible']})"
                )
            elif c["mature"]:
                out(f"      reason: {'; '.join(value['reasons'])}")

    out("\n--- findings ---")
    if not report["findings"]:
        out(
            "  (none) no dimension has two values with sufficient evidence; "
            "nothing is compared and nothing is recommended"
        )
    for finding in report["findings"]:
        flag = " [weakening]" if finding["weakening"] else ""
        out(f"  [{finding['kind']}]{flag} {finding['statement']}")
        out(f"      since {finding['since']}; {'; '.join(finding['uncertainty'])}")

    changes = report.get("changes")
    if changes:
        out(f"\n--- changes since {changes['baseline_at_local']} ---")
        out(f"  {changes['summary']}")
        if changes["overall_status_change"]:
            change = changes["overall_status_change"]
            out(f"  overall: {change['from']} -> {change['to']}")
        if changes["new_examples"]:
            out(f"  new mature examples: {', '.join(f'#{i}' for i in changes['new_examples'])}")
        for change in changes["finding_changes"]:
            out(f"  finding {change['id']}: {change['change']}")

    out("\n--- caveats ---")
    for caveat in report["caveats"]:
        out(f"  {caveat}")
    effects = report.get("side_effects", {})
    out(
        f"\nread-only: database writes = {effects.get('database_writes', 0)}, "
        f"Threads calls = {effects.get('threads_calls', 0)}, emails = {effects.get('emails', 0)}"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
