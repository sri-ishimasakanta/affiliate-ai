"""管理用 CLI: C11 の tracking URL の登録と、program 単位の host の許可 (2026-10-02)。

既定はすべて PLAN (書かない)。``--execute`` のときだけ書く。外には何も送らない。

    # 1) tracking URL をローカル DB に登録 (URL は非表示の入力。引数には取らない)
    uv run python scripts/affiliate_tracking_intake.py onboard --program-id 14 --expect-name Krisp --expect-provider Impact
    uv run python scripts/affiliate_tracking_intake.py onboard --program-id 14 --expect-name Krisp --expect-provider Impact --execute

    #    クリップボードから渡す (shell の履歴に残らない。PowerShell の例)
    Get-Clipboard | uv run python scripts/affiliate_tracking_intake.py onboard ... --url-stdin

    # 2) 登録した URL の host を、この program にだけ許す
    uv run python scripts/affiliate_tracking_intake.py approve-host --program-id 14 --expect-name Krisp --expect-provider Impact --host <host> --source "Impact dashboard" --by human --observed-at 2026-10-02T10:00:00+09:00 [--execute]
    uv run python scripts/affiliate_tracking_intake.py revoke-host  ... [--execute]

    # 3) 状態 (読むだけ)
    uv run python scripts/affiliate_tracking_intake.py status [--program-id 14]

- URL の全体・query の値・token は出さない (scheme・host・query の名前・長さ・SHA-256 の先頭だけ)。
- 登録しても link target は作らない・記事は変えない・WordPress には反映しない。
- host の許可は ``app/config/affiliate_program_host_approvals.json`` に追記する (git の差分で
  確かめて commit する)。catalog の landing page の host は許可にならない。
"""  # noqa: E501

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXIT_OK, EXIT_REFUSED, EXIT_FAILED = 0, 2, 1


def _safe_error(exc: Exception) -> str:
    from app.affiliate.destination_safety import AffiliateDestinationError
    from app.affiliate.program_host_approvals import HostApprovalError
    from app.services.affiliate_tracking_intake_service import IntakeError

    if isinstance(exc, IntakeError | HostApprovalError | AffiliateDestinationError):
        return str(exc)
    # SQLAlchemy などの例外は bound parameter (= URL) を含みうるので文言を出さない
    return f"{type(exc).__name__} (message withheld: may reference sensitive input)"


def _read_url(from_stdin: bool, stdin=None, prompt=getpass.getpass) -> str:
    if from_stdin:
        return (stdin or sys.stdin).readline()
    return prompt("tracking URL (hidden input, not echoed, never logged): ")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    onboard = sub.add_parser("onboard")
    for cmd in (onboard,):
        cmd.add_argument("--program-id", type=int, required=True)
        cmd.add_argument("--expect-name", required=True)
        cmd.add_argument("--expect-provider", required=True)
        cmd.add_argument("--url-stdin", action="store_true",
                         help="read the URL from the first line of stdin instead of a prompt")
        cmd.add_argument("--execute", action="store_true")
    for name in ("approve-host", "revoke-host"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--program-id", type=int, required=True)
        cmd.add_argument("--expect-name", required=True)
        cmd.add_argument("--expect-provider", required=True)
        cmd.add_argument("--host", required=True)
        cmd.add_argument("--source", required=True)
        cmd.add_argument("--by", required=True)
        cmd.add_argument("--observed-at", required=True)
        cmd.add_argument("--execute", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--program-id", type=int)
    return parser


def main(argv=None, *, session_factory=None, approvals_path: Path | None = None, stdin=None,
         prompt=getpass.getpass, now=None) -> int:  # fmt: skip
    from app.services.affiliate_tracking_intake_service import AffiliateTrackingIntakeService

    args = _parser().parse_args(argv)
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    try:
        with session_factory() as session:
            service = AffiliateTrackingIntakeService(session, approvals_path=approvals_path)
            if args.command == "onboard":
                # identity を先に確かめてから URL を求める (Make onboarding と同じ順番)
                service.program(args.program_id, expect_name=args.expect_name,
                                expect_provider=args.expect_provider)
                session.rollback()
                raw = _read_url(args.url_stdin, stdin, prompt)
                kw = {"expect_name": args.expect_name, "expect_provider": args.expect_provider,
                      "raw_url": raw}
                result = (service.execute_onboard(args.program_id, **kw) if args.execute
                          else {**service.plan_onboard(args.program_id, **kw), "executed": False})
                del raw, kw
            elif args.command in ("approve-host", "revoke-host"):
                action = "approve" if args.command == "approve-host" else "revoke"
                kw = {"expect_name": args.expect_name, "expect_provider": args.expect_provider,
                      "host": args.host, "source": args.source, "decided_by": args.by,
                      "observed_at": args.observed_at, "now": now}
                result = (service.execute_host(action, args.program_id, **kw) if args.execute
                          else {**service.plan_host(action, args.program_id, **kw),
                                "executed": False})  # fmt: skip
            else:
                result = service.status(args.program_id)
    except Exception as exc:  # noqa: BLE001 - CLI の境界は安全側 (URL を出さない)
        from app.affiliate.program_host_approvals import HostApprovalError
        from app.services.affiliate_tracking_intake_service import IntakeError

        refused = isinstance(exc, IntakeError | HostApprovalError)
        print(("refused: " if refused else "FAILED: ") + _safe_error(exc))
        return EXIT_REFUSED if refused else EXIT_FAILED
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if isinstance(result, dict) and not result.get("executed", True):
        print("PLAN only: nothing was written. Re-run with --execute to write.")
    print("local only: nothing was sent to any provider or to WordPress; no link target, "
          "article or projection was changed")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
