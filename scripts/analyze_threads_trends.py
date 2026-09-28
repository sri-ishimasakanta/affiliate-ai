"""管理用 CLI: 外の観察と自分の投稿の傾向を表示する (T6.5A-B、**読むだけ**)。

    uv run python scripts/analyze_threads_trends.py
    uv run python scripts/analyze_threads_trends.py --json

DB を読むだけ (書かない)。Threads にも OpenAI にも問い合わせない。結果は **記述** であって、
生成・公開には何も戻さない。少ない数のまとまりには small_sample が付く。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.threads_trend_analysis_service import build_report  # noqa: E402

EXIT_OK = 0


def render(report: dict) -> str:
    lines = [f"Threads の傾向 (読むだけ・{report['as_of']})", ""]
    ext = report["external"]
    lines.append("■ 外の観察")
    if not ext.get("available", True):
        lines.append(f"  利用できない: {ext['reason']}")
    else:
        lines.append(f"  実行: {ext['runs_by_status'] or 'なし'}")
        lines.append(f"  投稿 {ext['posts']} 件 / 観測 {ext['observations']} 回 / "
                     f"複数回見た投稿 {ext['posts_with_repeated_snapshots']} 件")  # fmt: skip
        lines.append(f"  投稿者 {ext['authors']} 人 "
                     f"(基準を作れた {ext['authors_with_baseline']} 人)")  # fmt: skip
        for item in ext["candidate_breakouts"]:
            lines.append(
                f"  伸びた候補: @{item['author_handle']} いいね {item['likes']} "
                f"(投稿者の中央値 {item['author_likes_baseline']['median']}、"
                f"{item['likes_breakout_ratio']} 倍) {item['external_post_key']}"
            )
        topics = ", ".join(t["topic"] for t in ext["repeated_trending_topics"]) or "なし"
        lines.append(f"  繰り返し出たトレンドのトピック: {topics}")
    own = report["own"]
    lines += ["", "■ 自分の投稿 (公開済み)",
              f"  {own['publications']} 本 (指標あり {own['publications_with_metrics']} 本)"
              + (" — 少ない数 (small sample)" if own["small_sample"] else "")]  # fmt: skip
    for dim in ("content_kind", "topic", "char_bucket", "cta_class"):
        for value, group in own["dimensions"][dim].items():
            flag = " (少数)" if group["small_sample"] else ""
            lines.append(f"  {dim}={value}: {group['n']} 本、views 中央値 "
                         f"{group['median']['views']}、likes 中央値 {group['median']['likes']}"
                         f"{flag}")  # fmt: skip
    lines += ["", "■ 自分の投稿: 中央値が高く / 低く観測されたまとまり (原因ではない)"]
    for dim, per_metric in own["observed_extremes"].items():
        for metric, item in per_metric.items():
            if item["status"] != "compared":
                lines.append(f"  {dim} / {metric}: {item['status']} "
                             f"(比べられるまとまり {item['comparable_groups']})")  # fmt: skip
                continue
            high, low = item["observed higher median"], item["observed lower median"]
            lines.append(f"  {dim} / {metric}: observed higher median = {high['group']} "
                         f"(n={high['n']}, {high['median']}) / observed lower median = "
                         f"{low['group']} (n={low['n']}, {low['median']})")  # fmt: skip
    lines += ["", "■ 注意"] + [f"  - {text}" for text in report["limitations"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, session_factory=None, now: datetime | None = None
         ) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="JSON で出す")
    args = parser.parse_args(argv)
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        report = build_report(session, now=now or datetime.now(UTC))
        session.rollback()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(report))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
