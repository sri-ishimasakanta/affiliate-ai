"""管理用 CLI: 常駐 Threads worker (T4.1 / T4.2)。

    # 既定: 1 回だけ評価して状態を出す (ロックも取らない。DB に何も書かない。外に出ない)
    uv run python scripts/run_threads_worker.py

    # 常駐 PLAN ループ (ロックを取る。2 つ目の worker は何もせずに終わる)
    uv run python scripts/run_threads_worker.py --resident

    # 読むだけの Threads 指標取得を有効にする (T4.2)
    uv run python scripts/run_threads_worker.py --resident --collect-insights

    # 承認依頼のまとめ送りを有効にする (T4.2、**本番では人の確認を経てから**)
    uv run python scripts/run_threads_worker.py --resident --send-approval-digests

**投稿はしない。** 自動公開の経路はコードで無効にしてあり、この CLI に
``--execute`` は無い。Threads への書き込み・WordPress への書き込み・
タスクスケジューラの変更は、どのモードでも 0 件。

外に作用するフラグ (``--collect-insights`` / ``--send-approval-digests``) を付けた
ときは、``--once`` でも worker のロックを取る。2 つのプロセスが同時に digest を
送ることはない。

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


def main(
    argv: list[str] | None = None, *, session_factory=None, settings=None, overrides=None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="1 回だけ評価する (既定)")
    mode.add_argument("--resident", action="store_true", help="常駐 PLAN ループ")
    parser.add_argument(
        "--max-cycles", type=int, default=None, help="--resident のサイクル上限 (検証用)"
    )
    parser.add_argument(
        "--collect-insights",
        action="store_true",
        help="期限が来た投稿の指標を読むだけで取得する (Threads への書き込みはしない)",
    )
    parser.add_argument(
        "--send-approval-digests",
        action="store_true",
        help="期限が来たら承認依頼のまとめ送りを 1 通送る (人が明示したときだけ)",
    )
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    service = ThreadsWorkerService(
        session_factory or SessionLocal,
        settings=settings or get_settings(),
        collect_insights=args.collect_insights,
        send_approval_digests=args.send_approval_digests,
        **(overrides or {}),
    )
    now = datetime.now(UTC)
    external = args.collect_insights or args.send_approval_digests

    if not args.resident:
        lock = service.build_lock() if external else None
        worker = service.build_worker(now=now, clock=lambda: now, lock=lock)
        if lock is not None:
            acquired = lock.acquire(now)
            if not acquired.get("acquired"):
                print("another Threads worker is already running; exiting without doing any work")
                return EXIT_ALREADY_RUNNING
        try:
            cycle = worker.run_cycle()
        finally:
            if lock is not None:
                lock.release(datetime.now(UTC))
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
        f"(sending enabled: {aw['delivery_enabled']})"
    )
    caps = status["capabilities"]
    print(
        f"capabilities          = collect_insights={caps['collect_insights']} "
        f"send_approval_digests={caps['send_approval_digests']} publish={caps['publish']}"
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
        if latest["latest_attempt_outcome"] == "failed":
            # 成功した最後の観測だけを見せると、失敗が続いていても気付けない。
            print(
                f"  LAST ATTEMPT FAILED  = {latest['latest_attempt_at']} "
                f"[{latest['latest_attempt_error_category']}] {latest['latest_attempt_error']}"
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

    for sub in status["subsystems"]:
        if sub["name"] != "insights_refresh":
            continue
        for item in (sub.get("last_summary") or {}).get("refreshed") or []:
            reason = f" — {item['reason']}" if item.get("reason") else ""
            print(
                f"insights refresh      = publication {item['publication_id']}: "
                f"{item['result']}{reason}"
            )

    print("\n--- subsystems (each owns its own next run) ---")
    for sub in status["subsystems"]:
        when = sub["next_run_local"] or f"(disabled: {sub['disabled_reason']})"
        print(f"  {sub['name']:28} next={when}")

    effects = status["side_effects"]
    print("\nside effects: " + ", ".join(f"{name}={value}" for name, value in effects.items()))
    print("投稿はしていない。WordPress にもスケジューラにも触れていない。")


if __name__ == "__main__":
    raise SystemExit(main())
