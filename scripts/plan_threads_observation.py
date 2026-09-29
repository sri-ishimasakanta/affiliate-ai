"""管理用 CLI: 外の Threads の観察の 1 日の計画を表示する (T6.5B.2、**読むだけ**)。

    uv run python scripts/plan_threads_observation.py
    uv run python scripts/plan_threads_observation.py --stage 1
    uv run python scripts/plan_threads_observation.py --date 2026-09-30 --json
    uv run python scripts/plan_threads_observation.py --offline
    uv run python scripts/plan_threads_observation.py --stage 1 --execute --dry-run --screenshots
    uv run python scripts/plan_threads_observation.py --stage 1 --execute --store --screenshots

既定 (``--execute`` なし) は **計画だけ**: ブラウザを開かない・Threads に問い合わせない・
DB に書かない・スケジュールを作らない。

``--execute`` (T6.5B.3) は、作った計画を **そのまま** 実行の仕組み (``observer.orchestrator``)
で動かす。``--dry-run`` (保存しない) か ``--store`` (本番の DB に保存) の **どちらかを必ず** 指定
する (既定の書き込みは無い)。ブラウザを開く前に、次を確かめて 1 つでも合わなければ止まる:
段階が正しい (保存は方針の今の段階の次まで)・計画のすべての画面が確認済み・custom_feed /
global_trending が無い・計画の数が段階の上限と上限 100 以内・ページ / スクロール / 語 /
アカウントの数が方針の上限以内・専用のブラウザのプロファイルがある・Playwright の
ブラウザの置き場所 (``PLAYWRIGHT_BROWSERS_PATH``) がある・``--store`` なら DB が期待する
revision で観察の表がある。実行の後、ログインが要った / 画面の形が違った / 勘定が合わなかった
手順があれば ``--store`` でも保存しない。
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


EXIT_GATE = 3
EXIT_NOT_STORED = 4


def gate_problems(
    plan, policy: dict, *, store: bool, profile_dir: Path, browsers_path: str | None,
    db_revision: str | None, expected_revision: str | None, tables_present: bool | None,
) -> list[str]:
    """ブラウザを開く前に確かめること (空なら通れる)。"""

    from app.social.threads.observer import selectors as sel
    from app.social.threads.observer.planning import NOT_SUBSTITUTED, PolicyError, validate_policy

    problems: list[str] = []
    try:
        validate_policy(policy)
    except PolicyError as exc:
        problems.append(f"policy: {exc}")
    current = int(policy["rollout"]["current_stage"])
    if str(plan.stage) not in policy["allocations"]:
        problems.append(f"stage {plan.stage} has no allocation")
    if store and plan.stage > current + 1:
        problems.append(f"stage {plan.stage} cannot be stored yet (current stage {current}; "
                        "one stage at a time)")  # fmt: skip
    limits = policy["page_limits"]
    for step in plan.steps:
        if step.source_type in NOT_SUBSTITUTED:
            problems.append(f"{step.source_type} must never be collected")
        surfaces = [step.source_type]
        if step.source_type == "topic_for_you":
            surfaces.append("topic_for_you_list")
        for surface in surfaces:
            if not sel.surface_verification(surface)["verified"]:
                problems.append(f"surface not verified: {surface}")
        if step.max_scrolls > int(limits["max_scrolls_per_page"]) * step.max_pages:
            problems.append(f"{step.source_type}: scroll limit exceeded")
        if step.budget > int(limits["max_accepted_per_page"]) * step.max_pages:
            problems.append(f"{step.source_type}: page budget exceeded")
    if plan.planned_posts > plan.stage_cap or plan.stage_cap > plan.hard_max:
        problems.append(f"plan {plan.planned_posts} exceeds cap {plan.stage_cap}/{plan.hard_max}")
    if plan.estimated_max_pages > int(limits["max_pages_per_run"]):
        problems.append("too many pages")
    if sum(1 for s in plan.steps if s.source_type == "search") > int(
            limits["max_search_queries_per_day"]):  # fmt: skip
        problems.append("too many search queries")
    if sum(1 for s in plan.steps if s.source_type == "known_account") > int(
            limits["max_known_accounts_per_day"]):  # fmt: skip
        problems.append("too many known accounts")
    if not profile_dir.is_dir() or not any(profile_dir.iterdir()):
        problems.append(f"browser profile missing: {profile_dir.as_posix()}")
    if not browsers_path or not Path(browsers_path).is_dir():
        problems.append("PLAYWRIGHT_BROWSERS_PATH is not configured")
    if store:
        if expected_revision is None or db_revision != expected_revision:
            problems.append(f"database revision {db_revision} != expected {expected_revision}")
        if not tables_present:
            problems.append("observer tables are missing")
    return problems


def _alembic_head() -> str | None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def _db_facts(session_factory) -> tuple[str | None, bool]:
    from sqlalchemy import text

    from app.services.threads_trend_analysis_service import observer_tables_present

    with session_factory() as session:
        try:
            revision = session.execute(text("select version_num from alembic_version")).scalar()
        except Exception:  # noqa: BLE001 - 表が無い DB
            revision = None
        present = observer_tables_present(session)
        session.rollback()
    return revision, present


def _post_rows(result) -> list[dict]:
    from app.social.threads.observer.normalize import normalize_body

    rows = []
    for item in result.posts:
        record = item.record
        rows.append({
            "source_type": item.source_type, "source_query": item.source_query,
            "external_post_key": record.external_post_key, "author_handle": record.author_handle,
            "post_timestamp": record.post_timestamp.isoformat() if record.post_timestamp else None,
            "topic": record.topic, "likes": record.likes, "replies": record.replies,
            "media_type": record.media_type, "body_length": record.features.get("body_length"),
            "body_lines": record.body_text.count("\n") + 1,
            "text_quality": normalize_body(record.body_text).text_quality,
            "normalization_flags": list(normalize_body(record.body_text).normalization_flags),
            "media_diagnostics": record.media_diagnostics,
            "numeric_facts_count": record.features.get("numeric_facts_count"),
        })  # fmt: skip
    return rows


def main(argv: list[str] | None = None, *, session_factory=None, now: datetime | None = None,
         policy_path: Path | None = None, page_factory=None, profile_dir: Path | None = None,
         browsers_path: str | None = None, expected_revision: str | None = "head",
         artifact_root: Path | None = None) -> int:  # fmt: skip
    import os

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="計画の日 (JST、YYYY-MM-DD。既定は今日)")
    parser.add_argument("--stage", type=int, default=3, help="段階 (1〜4。既定は 3 = 通常)")
    parser.add_argument("--offline", action="store_true", help="DB を読まない (投稿者の候補なし)")
    parser.add_argument("--json", action="store_true", help="JSON で出す")
    parser.add_argument("--execute", action="store_true",
                        help="計画を実行する (--dry-run か --store が必要)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="実行するが保存しない")
    mode.add_argument("--store", action="store_true", help="実行して本番の DB に保存する")
    parser.add_argument("--headed", action="store_true", help="画面を表示して実行する")
    parser.add_argument("--screenshots", action="store_true", help="ページごとに画面を保存する")
    parser.add_argument("--show-posts", action="store_true", help="投稿ごとの値を出す (本文なし)")
    args = parser.parse_args(argv)
    if (args.dry_run or args.store) and not args.execute:
        parser.error("--dry-run / --store are only valid with --execute")
    if args.execute and not (args.dry_run or args.store):
        parser.error("--execute needs exactly one of --dry-run or --store")
    policy = load_policy(policy_path) if policy_path else load_policy()
    moment = now or datetime.now(UTC)
    day = date.fromisoformat(args.date) if args.date else moment.astimezone(JST).date()
    if session_factory is None and (not args.offline or args.store):
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    authors: list[dict] = []
    if not args.offline:
        from app.services.threads_author_pool import author_pool

        with session_factory() as session:
            authors = author_pool(session)
            session.rollback()
    plan = build_daily_plan(policy, day=day, stage=args.stage, authors=authors)
    if not args.execute:
        if args.json:
            print(json.dumps({**plan.as_dict(), "authors_in_pool": len(authors)},
                             ensure_ascii=False, indent=2, default=str))  # fmt: skip
        else:
            print(render(plan, policy))
            print(f"観察した投稿者 (候補の元): {len(authors)} 人")
        return EXIT_OK

    from app.social.threads.observer.driver import DEFAULT_PROFILE_DIR

    revision, present = (None, None)
    if args.store:
        revision, present = _db_facts(session_factory)
    expected = _alembic_head() if expected_revision == "head" else expected_revision
    problems = gate_problems(
        plan, policy, store=args.store, profile_dir=profile_dir or DEFAULT_PROFILE_DIR,
        browsers_path=browsers_path if browsers_path is not None
        else os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        db_revision=revision, expected_revision=expected, tables_present=present,
    )  # fmt: skip
    record = {"plan": plan.as_dict(), "mode": "store" if args.store else "dry_run"}
    if problems:
        print(json.dumps({**record, "gate": "refused", "problems": problems},
                         ensure_ascii=False, indent=2, default=str))  # fmt: skip
        return EXIT_GATE

    from app.social.threads.observer.orchestrator import execute_plan

    if page_factory is None:
        from app.social.threads.observer.driver import PlaywrightPage

        page_factory = PlaywrightPage
    local = moment.astimezone(JST)
    shot_root = (artifact_root or Path("artifacts/threads-observer")) / local.strftime(
        "%Y-%m-%d") / f"stage{plan.stage}-{local:%H%M%S}"  # fmt: skip
    page = page_factory(profile_dir=profile_dir or DEFAULT_PROFILE_DIR, headless=not args.headed)
    try:
        out = execute_plan(page, plan, screenshot_dir=shot_root if args.screenshots else None,
                           screenshots=args.screenshots)  # fmt: skip
    finally:
        close = getattr(page, "close", None)
        if close:
            close()
    summary = out.summary()
    stop = ("login_required", "dom_unrecognized", "accounting_mismatch")
    blocking = [s for s in summary["steps"] if s["status"] in stop]
    record.update({"gate": "passed", "orchestration": summary,
                   "accounting": [{k: v for k, v in src.items() if k != "sequence"}
                                  for src in out.result.accounting],
                   "topic_list": ({k: v for k, v in out.result.topic_accounting.items()
                                   if k != "sequence"} if out.result.topic_accounting else None),
                   "provenance_multi": {k: v for k, v in out.provenance.items() if len(v) > 1},
                   "screenshots": out.result.screenshots})  # fmt: skip
    if args.show_posts:
        record["posts"] = _post_rows(out.result)
    stored = False
    if args.store and not blocking and out.result.status != "login_required":
        from app.services.threads_observer_service import record_run

        with session_factory() as session:
            run = record_run(session, out.result, extra_artifacts={
                "observation_plan": plan.as_dict(), "orchestration": summary,
                "provenance": out.provenance})  # fmt: skip
            record["run_id"] = run.id
            stored = True
    record["stored"] = stored
    if args.store and not stored:
        record["not_stored_reason"] = [f"{s['source_type']}:{s['query'] or '-'}={s['status']}"
                                       for s in blocking] or [out.result.status]  # fmt: skip
    print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    return EXIT_OK if (stored or not args.store) else EXIT_NOT_STORED


if __name__ == "__main__":
    raise SystemExit(main())
