"""管理用 CLI: Threads 承認依頼のまとめ送り (T4.2)。

    # 既定: いま送るなら何を送るかを見るだけ (何も書かない。メールも送らない)
    uv run python scripts/plan_threads_approval_digest.py

    # 実際に 1 通送る (人が明示したときだけ。通知窓 08:00-21:00 JST の外では送らない)
    uv run python scripts/plan_threads_approval_digest.py --execute

1 通には通常 5 件まで。提案ごとに別々のレビューページ (capability) があり、
**承認・却下は 1 件ずつ**。一括承認は無い。1 件を却下しても他には影響しない。

**承認は公開ではない。** 承認された提案は queue に入るだけで、投稿はされない。
このコマンドは Threads にも WordPress にも書き込まない。

``--execute`` は常駐 worker と同じロックを取る。worker が動いていれば何もせず終わる
(2 つのプロセスが同時に digest を送らないように)。

本番パイロット・試験のためだけに、人が明示すれば **通知窓 (08:00-21:00) だけ** を
上書きできる::

    uv run python scripts/plan_threads_approval_digest.py --execute \
        --override-notification-window \
        --override-reason "T4.2 production approval-digest pilot"

上書きが飛ばすのは通知窓だけ。stale / 期限切れ / 保留 / 重複 / cooldown / gather /
capability / セッションの期限 / CSRF / 1 件ずつの決定 は、すべてそのまま効く。
理由は必須で、digest の記録と配送記録に残る。期限は実際に送った時刻から 24 時間。
常駐 worker にはこの上書きの経路が無い。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.threads_approval_digest_service import ThreadsApprovalDigestService  # noqa: E402
from app.services.threads_worker_service import ThreadsWorkerService  # noqa: E402
from app.social.threads.digest import ManualWindowOverride, WindowOverrideError  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_NOT_SENT = 3
EXIT_ALREADY_RUNNING = 4


def main(
    argv: list[str] | None = None,
    *,
    session_factory=None,
    settings=None,
    notifier=None,
    relay_client=None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="実際に 1 通送る (既定は PLAN)")
    parser.add_argument(
        "--override-notification-window",
        action="store_true",
        help="人が明示した試験のためだけに、通知窓 (08:00-21:00) だけを上書きする",
    )
    parser.add_argument(
        "--override-reason", default=None, help="上書きの理由 (上書きするときは必須)"
    )
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    try:
        window_override = _window_override(args)
    except WindowOverrideError as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED

    factory = session_factory or SessionLocal
    settings = settings or get_settings()
    now = datetime.now(UTC)

    with factory() as session:
        service = ThreadsApprovalDigestService(
            session, settings=settings, notifier=notifier, relay_client=relay_client
        )
        plan = service.plan(now=now, window_override=window_override)
        payload = {"plan": plan.as_dict(service.timezone)}
        _print_plan(payload["plan"], executed=args.execute)
        housekeeping = service.housekeeping(now=now, execute=False)
        _print_housekeeping(housekeeping)
        payload["housekeeping"] = housekeeping

        if not args.execute:
            print("\nPLAN のみ。メールは送っていない。DB にも何も書いていない。")
            print("実際に送るには --execute を付ける (人が明示したときだけ)。")
            _write(args.json_path, payload)
            return EXIT_OK

    lock = ThreadsWorkerService(factory, settings=settings).build_lock()
    acquired = lock.acquire(now)
    if not acquired.get("acquired"):
        print("\nanother Threads worker holds the lock; not sending (no duplicate digest)")
        return EXIT_ALREADY_RUNNING
    try:
        with factory() as session:
            service = ThreadsApprovalDigestService(
                session, settings=settings, notifier=notifier, relay_client=relay_client
            )
            outcome = service.send(
                now=datetime.now(UTC), execute=True, window_override=window_override
            )
    finally:
        lock.release(datetime.now(UTC))

    payload["outcome"] = outcome.as_dict()
    print("\n=== digest send ===")
    print(f"sent               = {outcome.sent}")
    print(f"digest_id          = {outcome.digest_id}")
    print(f"proposals          = {outcome.proposal_ids}")
    print(f"review sessions    = {outcome.session_ids}")
    for skipped in outcome.skipped:
        print(f"  skipped #{skipped['proposal_id']}: {skipped['reason']}")
    if outcome.revoked_session_ids:
        print(f"revoked sessions   = {outcome.revoked_session_ids}")
    if outcome.expired_session_ids:
        print(f"expired sessions   = {outcome.expired_session_ids}")
    print(f"reason             = {outcome.reason}")
    if window_override is not None:
        print(f"window override    = {window_override.reason}")
    print(f"threads writes     = {outcome.threads_writes}")
    _write(args.json_path, payload)
    return EXIT_OK if outcome.sent else EXIT_NOT_SENT


def _window_override(args) -> ManualWindowOverride | None:
    """フラグと理由がそろったときだけ上書きを作る。片方だけなら拒否する。"""

    reason = args.override_reason
    if not args.override_notification_window:
        if reason is not None:
            raise WindowOverrideError(
                "--override-reason was given without --override-notification-window"
            )
        return None
    if not (reason or "").strip():
        raise WindowOverrideError("--override-notification-window requires --override-reason")
    return ManualWindowOverride(reason=reason)


def _write(path: str | None, payload: dict) -> None:
    if not path:
        return
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nJSON written to {path}")


def _print_plan(plan: dict, *, executed: bool) -> None:
    mode = "EXECUTE" if executed else "PLAN"
    print(f"=== threads approval digest ({mode}) ===")
    print(f"now                = {plan['now_local']}")
    print(f"policy_version     = {plan['policy_version']}")
    window_open = str(plan["notification_window_open"]).lower()
    print(f"notification_window_open  = {window_open} (08:00-21:00)")
    print(f"manual_override_requested = {str(plan['manual_override_requested']).lower()}")
    print(f"override_reason           = {plan['override_reason'] or '-'}")
    print(f"last digest sent   = {plan['last_sent_local'] or '(never)'}")
    print(f"cooldown until     = {plan['cooldown_until_local'] or '-'}")
    print(f"gather until       = {plan['gather_until_local'] or '-'}")
    print(f"due                = {plan['due_local'] or '-'}")
    print(f"waiting for        = {', '.join(plan['waiting_for']) or '(nothing)'}")
    print(
        f"would_send_email          = {str(plan['would_send']).lower()} "
        f"(emails: {plan['emails_if_sent']})"
    )
    print(f"approval TTL       = {plan['ttl_hours']}h from the send time")
    print(f"planned TTL start  = {plan['planned_ttl_start_local'] or '-'}")
    print(f"planned expiry     = {plan['planned_expires_local'] or '-'}")

    stock = plan["stock"]
    print(
        f"stock (advisory)   = approved {stock['approved_unpublished']}, "
        f"requested {stock['pending_requests']}, prepared {stock['prepared']} "
        f"(target {stock['target_low']}-{stock['target_high']})"
    )
    if stock["suggest_generating_more"]:
        print("  note: stock is below the advisory low mark; consider generating proposals")

    print(f"\n--- selected for one email ({len(plan['selected'])}) ---")
    for item in plan["selected"]:
        _print_item(item)
    print(f"\n--- deferred to a later digest ({len(plan['deferred'])}) ---")
    for item in plan["deferred"]:
        _print_item(item)
    print(f"\n--- suppressed (kept in stock) ({len(plan['suppressed'])}) ---")
    for item in plan["suppressed"]:
        _print_item(item)
    for note in plan["notes"]:
        print(f"note: {note}")


def _print_item(item: dict) -> None:
    timing = " ".join(
        part
        for part in (
            f"not_before={item['not_before_local']}" if item["not_before_local"] else "",
            f"expires={item['expires_local']}" if item["expires_local"] else "",
        )
        if part
    )
    print(
        f"  #{item['proposal_id']} [{item['reason']}] {item['angle']} / "
        f"{item['article_title']} {timing}".rstrip()
    )
    print(f"      {item['preview']}")
    for detail in item["details"]:
        print(f"      - {detail}")


def _print_housekeeping(result: dict) -> None:
    lapsed = result["expired_session_ids"]
    revoke = result["revoke"]
    if not lapsed and not revoke:
        return
    print("\n--- pending requests that would be cleaned up ---")
    for session_id in lapsed:
        print(f"  session {session_id}: expired")
    for item in revoke:
        print(
            f"  session {item['session_id']} (proposal #{item['proposal_id']}): revoke — "
            + "; ".join(item["reasons"])
        )


if __name__ == "__main__":
    raise SystemExit(main())
