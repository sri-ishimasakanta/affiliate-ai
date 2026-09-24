"""管理用 CLI: 常駐 Threads worker (T4.1、**PLAN 専用**)。

    # 既定: 1 回だけ評価して状態を出す (ロックも取らない。DB に何も書かない)
    uv run python scripts/run_threads_worker.py

    # 同じく 1 回だけ (明示)
    uv run python scripts/run_threads_worker.py --once

    # 常駐 PLAN ループ (ロックを取る。2 つ目の worker は何もせずに終わる)
    uv run python scripts/run_threads_worker.py --resident

**T4.1 では投稿しない。** 自動公開の経路はコードで無効にしてあり、この CLI に
``--execute`` は無い。Threads への書き込み・承認メール・WordPress への書き込み・
タスクスケジューラの変更は、どのモードでも 0 件。

``--resident`` が書き込むのは worker 自身のロック行だけである。

**承認は公開ではない。** 承認済みの提案は queue に入るだけで、いつ・どれを出すかは
別の判定である (T4.3)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.threads_worker_service import ThreadsWorkerService  # noqa: E402
from app.social.threads.worker import EXIT_ALREADY_RUNNING, EXIT_OK  # noqa: E402


def main(argv: list[str] | None = None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="1 回だけ評価する (既定)")
    mode.add_argument("--resident", action="store_true", help="常駐 PLAN ループ")
    parser.add_argument(
        "--max-cycles", type=int, default=None, help="--resident のサイクル上限 (検証用)"
    )
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    service = ThreadsWorkerService(
        session_factory or SessionLocal, settings=settings or get_settings()
    )
    now = datetime.now(UTC)

    if not args.resident:
        worker = service.build_worker(now=now, clock=lambda: now)
        cycle = worker.run_cycle()
        status = service.status(now=now, schedule=worker.schedule)
        status["cycle"] = cycle.as_dict()
        _print_status(status, mode="once")
        _write(args.json_path, status)
        return EXIT_OK

    lock = service.build_lock()
    worker = service.build_worker(now=now, sleep=time.sleep, lock=lock)
    try:
        run = worker.run(max_cycles=args.max_cycles)
    except KeyboardInterrupt:
        # finally 節でロックは解放済み。
        print("stopped by operator")
        return EXIT_OK
    if run.exit_code == EXIT_ALREADY_RUNNING:
        print("another Threads worker is already running; exiting without doing any work")
        blocking = (run.lock or {}).get("blocking_owner_label")
        if blocking:
            print(f"  held by: {blocking}")
        return EXIT_ALREADY_RUNNING
    final = datetime.now(UTC)
    status = service.status(now=final, schedule=worker.schedule)
    status["cycles"] = [c.as_dict() for c in run.cycles]
    _print_status(status, mode="resident")
    _write(args.json_path, status)
    return run.exit_code


def _write(path: str | None, payload: dict) -> None:
    if not path:
        return
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON written to {path}")


def _print_status(status: dict, *, mode: str) -> None:
    threads = status["threads"]
    print(f"=== threads worker ({status['worker_mode'].upper()} / {mode}) ===")
    print(f"now ({status['timezone']})      = {status['now_local']}")
    print(f"policy_version        = {status['policy_version']}")
    print(f"threads state         = {threads['state']}")
    for issue in threads["config_issues"]:
        print(f"  CONFIG: {issue}")
    print(f"configuration healthy = {threads['healthy']}")
    print(f"automatic publication = {status['automatic_publication_enabled']} (T4.1: always)")
    print(f"max posts per cycle   = {status['max_publications_per_cycle']}")

    pw = status["publication_window"]
    aw = status["approval_notification_window"]
    print(f"publication window    = {pw['start']}-{pw['end']} open={pw['open_now']}")
    print(
        f"approval notify window= {aw['start']}-{aw['end']} open={aw['open_now']} "
        f"(delivery deferred to {aw['deferred_to']})"
    )

    latest = status["latest_publication"]
    if latest:
        print(
            f"latest publication    = #{latest['publication_id']} at {latest['published_local']} "
            f"({latest['minutes_since']} min ago)"
        )
        print(
            f"latest insight        = {latest['maturity']} comparable={latest['comparable']} "
            f"snapshot={latest['latest_snapshot_at'] or '(none)'}"
        )
    else:
        print("latest publication    = (none)")

    gap = status["soft_gap"]
    print(
        f"soft gap              = {gap['minutes']} min; earliest eligible {gap['earliest_local']}"
    )
    activity = status["daily_activity"]
    print(
        f"today's posts         = {activity['count']} "
        f"(advisory {activity['target_low']}-{activity['target_high']}, {activity['band']}; "
        "not a quota, not a cap)"
    )
    proposals = status["proposals"]
    print(
        f"proposals             = approved {proposals['approved']}, "
        f"awaiting approval {proposals['awaiting_approval']}"
    )

    queue = status["queue"]
    print(f"eligible candidates   = {queue['eligible_count']}")
    nxt = queue["next_candidate"]
    print(f"next candidate        = {nxt['proposal_id'] if nxt else '(none)'}")
    print(f"evidence              = {queue['evidence_state']}")
    print(f"hard blockers         = {', '.join(status['hard_blockers']) or '(none)'}")
    if status["problems"]:
        print(f"PROBLEMS              = {', '.join(status['problems'])}")

    print("\n--- subsystems (each owns its own next run) ---")
    for sub in status["subsystems"]:
        when = sub["next_run_local"] or f"(disabled: {sub['disabled_reason']})"
        print(f"  {sub['name']:28} next={when}")

    effects = status["side_effects"]
    print("\nside effects: " + ", ".join(f"{name}={value}" for name, value in effects.items()))
    print("PLAN のみ。投稿もメールも WordPress もスケジューラも触っていない。")


if __name__ == "__main__":
    raise SystemExit(main())
