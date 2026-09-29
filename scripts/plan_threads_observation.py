"""管理用 CLI: 外の Threads の観察の 1 日の計画を表示する (T6.5B.2、**読むだけ**)。

    uv run python scripts/plan_threads_observation.py
    uv run python scripts/plan_threads_observation.py --stage 1
    uv run python scripts/plan_threads_observation.py --date 2026-09-30 --json
    uv run python scripts/plan_threads_observation.py --offline

**ブラウザを開かない・Threads に問い合わせない・DB に書かない・スケジュールを作らない。**
知っているアカウントの候補は、DB の観察の記録を読むだけで作る (``--offline`` なら読まない)。
既定の段階は 3 (通常の目安 90 件)。いま実行してよい段階は方針の rollout に従う (人の許可)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.social.threads.observer.planning import build_daily_plan, load_policy  # noqa: E402

EXIT_OK = 0
JST = ZoneInfo("Asia/Tokyo")


def render(plan, policy: dict) -> str:
    rollout = policy["rollout"]
    lines = [
        f"Threads の観察の計画 (読むだけ・実行しない) — {plan.day} (JST)",
        f"方針: {plan.policy_version} / 段階 {plan.stage} ({plan.stage_name}) の上限 "
        f"{plan.stage_cap} 件",
        f"重複を除いた投稿: 最低の目安 {plan.soft_min} / 通常の目安 {plan.unique_target} / "
        f"上限 {plan.hard_max} (目標であって約束ではない。足りない分は埋めない)",
        f"いまの段階: {rollout['current_stage']} (次の段階へは人の許可が要る)",
        "",
    ]
    by_source: dict[str, list] = {}
    for step in plan.steps:
        by_source.setdefault(step.source_type, []).append(step)
    for source, steps in by_source.items():
        lines.append(f"{source}: {sum(s.budget for s in steps)}")
        if source == "search":
            lines.append(f"  語の一覧: {plan.query_pool_version} (日付で順に回す)")
            lines += [f"  {s.query}: {s.budget}" for s in steps]
        elif source == "topic_for_you":
            topic = plan.topic_for_you
            lines.append(f"  トピック最大 {topic['max_topics']} 個 × 各 "
                         f"{topic['max_posts_per_topic']} 件")  # fmt: skip
            lines.append("  (一覧の順。このアカウント向けの一覧で、世の中のトレンドではない)")
        elif source == "known_account":
            lines.append(f"  投稿者 {len(steps)} 人 × 最大 {max(s.budget for s in steps)} 件 "
                         "(投稿者の基準を作るため)")  # fmt: skip
            lines += [f"  {s.query[:3]}…: {s.budget}" for s in steps]
    for item in plan.skipped:
        lines.append(f"計画に入れない: {item['source_type']} — {item['reason']}")
    lines += [f"注意: {note}" for note in plan.notes]
    lines += [
        "",
        f"計画の投稿の数 (最大): {plan.planned_posts}",
        f"開くページ (最大): {plan.estimated_max_pages} / スクロール (最大): "
        f"{plan.estimated_max_scrolls}",
        f"同じ投稿の再観測: {'有効' if plan.repeat_observation['enabled'] else '無効'} "
        f"(予算 {plan.repeat_observation['budget']}、重複を除いた数に入れない)",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, session_factory=None, now: datetime | None = None,
         policy_path: Path | None = None) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="計画の日 (JST、YYYY-MM-DD。既定は今日)")
    parser.add_argument("--stage", type=int, default=3, help="段階 (1〜4。既定は 3 = 通常)")
    parser.add_argument("--offline", action="store_true", help="DB を読まない (投稿者の候補なし)")
    parser.add_argument("--json", action="store_true", help="JSON で出す")
    args = parser.parse_args(argv)
    policy = load_policy(policy_path) if policy_path else load_policy()
    day = date.fromisoformat(args.date) if args.date else (now or datetime.now(UTC)).astimezone(
        JST).date()  # fmt: skip
    authors: list[dict] = []
    if not args.offline:
        if session_factory is None:
            from app.config.database import SessionLocal

            session_factory = SessionLocal
        from app.services.threads_author_pool import author_pool

        with session_factory() as session:
            authors = author_pool(session)
            session.rollback()
    plan = build_daily_plan(policy, day=day, stage=args.stage, authors=authors)
    if args.json:
        print(json.dumps({**plan.as_dict(), "authors_in_pool": len(authors)}, ensure_ascii=False,
                         indent=2, default=str))  # fmt: skip
    else:
        print(render(plan, policy))
        print(f"観察した投稿者 (候補の元): {len(authors)} 人")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
