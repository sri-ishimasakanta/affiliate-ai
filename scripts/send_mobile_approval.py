"""管理用 CLI: 変更要求 1 件のモバイル承認を依頼する (C8.8)。

    # 何が送られるかだけ見る (既定。中継にもメールにも触れない)
    uv run python scripts/send_mobile_approval.py --request-id 1

    # 実際にレビューセッションを作り、依頼メールを 1 通送る
    uv run python scripts/send_mobile_approval.py --request-id 1 --execute

    # 発行済みのセッションを失効させる
    uv run python scripts/send_mobile_approval.py --revoke-session 3 --reason "作り直す"

**承認はしない。適用もしない。** ここで作られるのは ``pending`` のレビュー
セッション 1 つと、それを開く導線を持つメール 1 通だけである。

候補から自動で依頼を作ることはない。変更要求が既に存在し、承認待ちであることが
前提になる。同じ要求に有効なセッションが残っていれば、二重に送らず拒否する。

レビュー URL の capability は **表示しない**。メールにだけ載る。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.approval.capability import DEFAULT_TTL_HOURS  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.mobile_approval_service import (  # noqa: E402
    MobileApprovalError,
    MobileApprovalService,
)

EXIT_OK = 0
EXIT_REFUSED = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id", type=int, help="承認を依頼する change request の id")
    parser.add_argument("--ttl-hours", type=int, default=DEFAULT_TTL_HOURS)
    parser.add_argument("--execute", action="store_true", help="実際に送る (既定は PLAN)")
    parser.add_argument("--revoke-session", type=int, help="失効させるセッション id")
    parser.add_argument("--reason", help="失効の理由")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    with SessionLocal() as session:
        service = MobileApprovalService(session, settings=settings)
        try:
            payload = _dispatch(service, args)
        except MobileApprovalError as exc:
            print(f"refused: {exc.reason}")
            return EXIT_REFUSED

        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            print(f"\nJSON written to {args.json_path}")
    return EXIT_OK


def _dispatch(service: MobileApprovalService, args) -> dict:
    if args.revoke_session is not None:
        if not (args.reason or "").strip():
            raise MobileApprovalError("--revoke-session requires --reason")
        row = service.revoke(session_id=args.revoke_session, reason=args.reason)
        print(f"revoked session {row.id} -> {row.state}")
        return {"session_id": row.id, "state": row.state}

    if args.request_id is None:
        raise MobileApprovalError("--request-id is required")

    if not args.execute:
        prepared = service.plan(change_request_id=args.request_id, ttl_hours=args.ttl_hours)
        snapshot = prepared.snapshot
        print("=== mobile approval (PLAN) ===")
        print(f"change_request_id  = {prepared.change_request_id}")
        print(f"article            = {snapshot.get('article_title')}")
        print(f"target             = {snapshot.get('target_article_title')}")
        print(f"change_type        = {snapshot.get('change_type')}")
        print(f"priority           = {snapshot.get('priority')}")
        print(f"proposal           = v{prepared.subject_version} {prepared.subject_hash[:16]}")
        print(f"ttl_hours          = {prepared.ttl_hours}")
        print(f"eligible           = {prepared.ok}")
        for reason in prepared.blocked_reasons:
            print(f"  BLOCKED: {reason}")
        print("\nPLAN のみ。実際に送るには --execute を付ける。")
        print("承認はされない。メールはレビューページを開く導線を運ぶだけである。")
        return prepared.as_dict()

    row = service.send(change_request_id=args.request_id, ttl_hours=args.ttl_hours)
    print("=== mobile approval sent ===")
    print(f"session_id         = {row.id}")
    print(f"state              = {row.state}")
    print(f"relay_session_id   = {row.relay_session_id}")
    print(f"expires_at         = {row.expires_at}")
    print(f"notification       = {row.notification_delivery_id}")
    print("\nレビュー URL は表示しない (capability を含むため)。メールを確認すること。")
    print("承認されたら: uv run python scripts/sync_mobile_approvals.py --execute")
    return {
        "session_id": row.id,
        "state": row.state,
        "relay_session_id": row.relay_session_id,
        "subject_hash": row.subject_hash,
        "expires_at": row.expires_at,
        "notification_delivery_id": row.notification_delivery_id,
    }


if __name__ == "__main__":
    raise SystemExit(main())
