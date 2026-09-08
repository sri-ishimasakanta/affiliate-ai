"""管理用 CLI: WordPress runtime から outbound click を 1 ページ取り込む。

    # PLAN のみ (無通信・無書込・run 作成なし):
    uv run python scripts/import_affiliate_outbound_clicks.py
    # 署名付き GET を 1 回実行:
    uv run python scripts/import_affiliate_outbound_clicks.py --execute

public HTTP endpoint ではない。cursor 整合・署名・検証・取り込みの business logic は
複製せず :class:`AffiliateClickImportService` に委譲する。

shared secret / HMAC signature / raw response body / full headers / destination URL /
token 値 は plan / 成功 / 例外いずれの出力にも含めない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.affiliate.click_export_client import (  # noqa: E402
    CLICK_EXPORT_ENDPOINT_PATH,
    CLICK_EXPORT_MAX_LIMIT,
)
from app.affiliate.runtime_http import require_https_origin  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.exceptions import AffiliateClickImportError  # noqa: E402
from app.services.affiliate_click_import_service import (  # noqa: E402
    AffiliateClickImportService,
)

_DEFAULT_LIMIT = 1000

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_CURSOR_MISMATCH = 3
EXIT_IMPORT_ERROR = 4
EXIT_UNEXPECTED = 5


def _endpoint_host(base_url: str | None) -> str:
    if not base_url:
        return "(not configured)"
    try:
        return require_https_origin(base_url, error_cls=AffiliateClickImportError)
    except AffiliateClickImportError:
        return "(invalid base URL)"


def run(
    *,
    execute: bool,
    limit: int,
    settings=None,
    session_factory=SessionLocal,
    transport=None,
) -> int:
    settings = settings or get_settings()
    configured = bool(settings.affiliate_runtime_push_configured)
    host = _endpoint_host(getattr(settings, "wordpress_base_url", None))

    with session_factory() as session:
        svc = AffiliateClickImportService(session)
        plan = svc.plan()

        print("=== affiliate outbound click import (PLAN) ===")
        print(f"since_id                    = {plan.since_id}")
        print(f"replica_max_source_click_id = {plan.replica_max_source_click_id}")
        print(f"cursor_consistent           = {plan.cursor_consistent}")
        print(f"limit                       = {limit}")
        print(f"endpoint_host               = {host}")
        print(f"endpoint_path               = {CLICK_EXPORT_ENDPOINT_PATH}")
        print(f"runtime_configured          = {configured}")
        print(f"execute                     = {execute}")

        if not plan.cursor_consistent:
            print(
                "\nWARNING: run cursor and replica cursor disagree; --execute would "
                "fail closed (no run row, no HTTP, no insert) until reconciled."
            )

        if not execute:
            print(
                "\nplan only: no WordPress request, no DB write, no run row. "
                "pass --execute to import one page."
            )
            return EXIT_OK

        if not configured:
            print(
                "\nNOT CONFIGURED: WORDPRESS_BASE_URL + AFFILIATE_RUNTIME_SHARED_SECRET "
                "are required for --execute."
            )
            return EXIT_NOT_CONFIGURED

        try:
            out = svc.import_next_page(
                limit=limit, settings=settings, transport=transport
            )
        except AffiliateClickImportError as exc:
            print(f"\nIMPORT FAILED: {exc.reason}")
            if exc.reason == "local click cursor integrity mismatch":
                return EXIT_CURSOR_MISMATCH
            return EXIT_IMPORT_ERROR

        print("\n=== imported page ===")
        print(f"run_id                 = {out.id}")
        print(f"status                 = {out.status}")
        print(f"http_status            = {out.http_status}")
        print(f"requested_since_id     = {out.requested_since_id}")
        print(f"response_count         = {out.response_count}")
        print(f"inserted_count         = {out.inserted_count}")
        print(f"duplicate_count        = {out.duplicate_count}")
        print(f"unresolved_token_count = {out.unresolved_token_count}")
        print(f"next_since_id          = {out.response_next_since_id}")
        print(f"has_more               = {out.has_more}")
        if out.has_more:
            print(
                "\nhas_more: received a full page; re-run --execute to fetch the next "
                "page from the new cursor."
            )
        return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="import_affiliate_outbound_clicks",
        description="WordPress runtime から outbound click を 1 ページ取り込む (管理用 CLI)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="署名付き GET を 1 回実行する (指定なしなら PLAN のみ・無通信・無書込)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"1 ページの最大件数 (1..{CLICK_EXPORT_MAX_LIMIT}, default {_DEFAULT_LIMIT})",
    )
    args = parser.parse_args(argv)
    if not (1 <= args.limit <= CLICK_EXPORT_MAX_LIMIT):
        parser.error(f"--limit must be between 1 and {CLICK_EXPORT_MAX_LIMIT}")

    try:
        return run(execute=args.execute, limit=args.limit)
    except Exception as exc:  # noqa: BLE001 - admin CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
