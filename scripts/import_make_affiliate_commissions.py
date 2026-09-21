"""管理用 CLI: Phase E1 -- Make affiliate commission の read-only 取り込み。

    plan (デフォルト。無通信・無書込):
        uv run python -m scripts.import_make_affiliate_commissions \
            --affiliate-program-id 1

    execute (Make API へ GET のみ、DB へ書き込み):
        uv run python -m scripts.import_make_affiliate_commissions \
            --affiliate-program-id 1 --date-from 2026-09-01 --date-to 2026-09-30 \
            --execute

``--execute`` には ``--date-from`` と ``--date-to`` の **両方が必須** (Phase E1.4 --
live で、どちらか欠けると Make API が HTTP 400 を返すことを確認済み)。範囲を
補完/デフォルト化することは一切せず、片方でも欠ければ DB / HTTP に触れる前に
fail closed する。PLAN は日付なしでも実行できる (HTTP なし・DB 書込なし)。

Make API token を CLI 引数として受け取ることは一切できない -- 常に
``MAKE_API_TOKEN`` 環境変数 (:class:`app.config.settings.Settings`) からのみ読む。

出力は安全な要約のみ: Make API token / affiliate code / tracking URL /
organization id / commission 明細は一切出力しない (件数のみ)。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.exceptions import ApplicationError  # noqa: E402
from app.services.affiliate_commission_import_service import (  # noqa: E402
    AffiliateCommissionImportService,
    validate_date_range,
)

EXIT_OK = 0
EXIT_FAILED = 1

_SAFE_EXCEPTION_TYPES = (ApplicationError,)


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, _SAFE_EXCEPTION_TYPES):
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__} (message withheld: may reference sensitive input)"


def _print_plan(result) -> None:
    print("=== Make Commission Import Plan (READ-ONLY, no HTTP, no DB mutation) ===")
    print(f"provider                = {result.provider}")
    print(f"affiliate_program_id    = {result.affiliate_program_id}")
    print(f"make_api_configured     = {result.configured}")
    print(f"date_from               = {result.date_from}")
    print(f"date_to                 = {result.date_to}")
    print(f"dates_complete          = {result.dates_complete}")
    print(f"would_execute           = {result.would_execute}")
    print()
    if not result.dates_complete:
        print(
            "DATES REQUIRED: --execute needs BOTH --date-from and --date-to "
            "(the Make API rejects requests missing either). No range is "
            "invented or defaulted."
        )
    if not result.configured:
        print(
            "NOT CONFIGURED: MAKE_API_BASE_URL / MAKE_API_TOKEN are not both set. "
            "--execute would fail closed before any HTTP request."
        )
    print("no HTTP request made, no DB write performed. Pass --execute to import.")


def _print_execute_preflight(plan) -> None:
    """``--execute`` 直前の要約。PLAN とは別物 (この後に HTTP と DB 書込が続く)。"""

    print("=== Make Commission Import Preflight (EXECUTE MODE) ===")
    print("mode                    = EXECUTE")
    print(f"provider                = {plan.provider}")
    print(f"affiliate_program_id    = {plan.affiliate_program_id}")
    print(f"make_api_configured     = {plan.configured}")
    print(f"date_from               = {plan.date_from}")
    print(f"date_to                 = {plan.date_to}")
    print()
    if not plan.configured:
        print(
            "NOT CONFIGURED: MAKE_API_BASE_URL / MAKE_API_TOKEN are not both set. "
            "Execution will fail closed before any HTTP request."
        )
    print(
        "EXECUTING NOW: this run will send read-only GET requests to the Make API "
        "and write the import run (and any commission facts) to the DB."
    )


def _print_execute(run) -> None:
    print("=== Make Commission Import Execution ===")
    print(f"run_id                  = {run.id}")
    print(f"status                  = {run.status}")
    print(f"provider                = {run.provider}")
    print(f"affiliate_program_id    = {run.affiliate_program_id}")
    print(f"page_count              = {run.page_count}")
    print(f"response_count          = {run.response_count}")
    print(f"inserted_count          = {run.inserted_count}")
    print(f"updated_count           = {run.updated_count}")
    print(f"unchanged_count         = {run.unchanged_count}")


def cmd_import(args: argparse.Namespace) -> int:
    if args.execute:
        # Phase E1.4: DB にも HTTP にも触れる前に fail closed (補完/デフォルト化しない)。
        validate_date_range(args.date_from, args.date_to, require_both=True)

    with SessionLocal() as session:
        service = AffiliateCommissionImportService(session)

        if not args.execute:
            result = service.plan(
                affiliate_program_id=args.affiliate_program_id,
                date_from=args.date_from,
                date_to=args.date_to,
            )
            _print_plan(result)
            return EXIT_OK

        plan = service.plan(
            affiliate_program_id=args.affiliate_program_id,
            date_from=args.date_from,
            date_to=args.date_to,
        )
        _print_execute_preflight(plan)
        print()
        run = service.import_commissions(
            affiliate_program_id=args.affiliate_program_id,
            date_from=args.date_from,
            date_to=args.date_to,
            page_limit=args.page_limit,
        )
        _print_execute(run)
        return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="import_make_affiliate_commissions",
        description=(
            "Phase E1: read-only import of Make affiliate commission facts. "
            "PLAN by default (no HTTP, no DB write); pass --execute to fetch and "
            "persist. The Make API token is never a CLI argument -- it is always "
            "read from the MAKE_API_TOKEN environment variable."
        ),
    )
    parser.add_argument("--affiliate-program-id", type=int, required=True)
    parser.add_argument(
        "--date-from",
        type=date.fromisoformat,
        default=None,
        help="YYYY-MM-DD. REQUIRED together with --date-to for --execute",
    )
    parser.add_argument(
        "--date-to",
        type=date.fromisoformat,
        default=None,
        help="YYYY-MM-DD. REQUIRED together with --date-from for --execute",
    )
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required to perform the intended Make API GET(s) and DB write; "
        "PLAN (read-only) is the default",
    )
    parser.set_defaults(func=cmd_import)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI 境界は安全側に倒す
        print(f"FAILED: {_safe_error_message(exc)}")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
