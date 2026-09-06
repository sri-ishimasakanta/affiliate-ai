"""管理用 CLI: 最初の本番 Search Console インポートを実行する。

    uv run python scripts/import_search_console.py            # プラン表示のみ (無通信・無書込)
    uv run python scripts/import_search_console.py --execute  # 実インポート

public HTTP endpoint ではない。provider / import のビジネスロジックは複製せず、
:class:`GoogleSearchConsoleProvider` と :class:`SearchConsoleImportService` に委譲する。

secret (private_key / private_key_id / access token / Authorization / credential
JSON / credential path) は plan / 成功 / 例外いずれでも出力しない。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.exceptions import (  # noqa: E402
    ExternalProviderDataError,
    ExternalProviderError,
    SearchConsoleCredentialError,
    SearchConsoleImportStateError,
)
from app.models import (  # noqa: E402
    SearchConsoleImportRun,
    SearchConsolePageDaily,
    SearchConsoleQueryDaily,
)
from app.search_console.date_window import recent_window  # noqa: E402
from app.search_console.google_provider import GoogleSearchConsoleProvider  # noqa: E402
from app.search_console.import_identity import (  # noqa: E402
    DATA_STATE,
    IMPORT_VERSION,
    PAGE_DIMENSIONS,
    QUERY_DIMENSIONS,
)
from app.services.search_console_import_service import (  # noqa: E402
    SearchConsoleImportService,
)

#: 最初の本番インポート専用の idempotency key。同じコマンドを誤って再実行しても
#: 同一 identity の prepared run に解決され、Google を二重に叩かない。
FIRST_IMPORT_IDEMPOTENCY_KEY = "3c5fc1c-gsc-first-production-import-v1"

_WINDOW_DAYS = 7
_WINDOW_END_LAG_DAYS = 1

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_STATE = 3
EXIT_PROVIDER_ERROR = 4
EXIT_UNEXPECTED = 5


def _default_provider_factory(credentials_file: str | None):
    return GoogleSearchConsoleProvider(credentials_file=credentials_file)


def _print_plan(*, property_uri: str, start_date, end_date) -> None:
    print("=== Search Console first production import (PLAN) ===")
    print(f"property_uri           = {property_uri}")
    print(f"start_date (PT)        = {start_date.isoformat()}")
    print(f"end_date (PT)          = {end_date.isoformat()}")
    print(f"import_version         = {IMPORT_VERSION}")
    print(f"page_dimensions        = {list(PAGE_DIMENSIONS)}")
    print(f"query_dimensions       = {list(QUERY_DIMENSIONS)}")
    print(f"data_state             = {DATA_STATE}")
    print(f"idempotency_key        = {FIRST_IMPORT_IDEMPOTENCY_KEY}")


def run(
    *,
    execute: bool,
    settings=None,
    session_factory=SessionLocal,
    provider_factory=None,
    now: datetime | None = None,
) -> int:
    settings = settings or get_settings()
    property_uri = settings.search_console_property_uri
    credentials_file = settings.search_console_credentials_file

    if not property_uri:
        print("NOT CONFIGURED: SEARCH_CONSOLE_PROPERTY_URI is not set")
        return EXIT_NOT_CONFIGURED
    if not credentials_file:
        print("NOT CONFIGURED: SEARCH_CONSOLE_CREDENTIALS_FILE is not set")
        return EXIT_NOT_CONFIGURED

    start_date, end_date = recent_window(
        days=_WINDOW_DAYS, end_lag_days=_WINDOW_END_LAG_DAYS, now=now
    )
    _print_plan(property_uri=property_uri, start_date=start_date, end_date=end_date)
    print("credentials_configured = True")

    if not execute:
        print("\nplan only: no Google request, no DB write. pass --execute to import.")
        return EXIT_OK

    if provider_factory is None:
        def provider_factory():
            return _default_provider_factory(credentials_file)

    with session_factory() as session:
        svc = SearchConsoleImportService(session)
        try:
            run_row = svc.prepare(
                property_uri=property_uri,
                start_date=start_date,
                end_date=end_date,
                idempotency_key=FIRST_IMPORT_IDEMPOTENCY_KEY,
            )
        except SearchConsoleImportStateError as exc:
            print(f"PREPARE REJECTED: {exc}")
            return EXIT_STATE

        print("\n=== prepared run ===")
        print(f"run_id                = {run_row.id}")
        print(f"status                = {run_row.status}")
        print(f"property_uri          = {run_row.property_uri}")
        print(f"start_date            = {run_row.start_date.isoformat()}")
        print(f"end_date              = {run_row.end_date.isoformat()}")
        print(f"import_identity_hash  = {run_row.import_identity_hash}")
        print(f"idempotency_key       = {run_row.idempotency_key}")
        print(f"dimensions_json       = {run_row.dimensions_json}")

        if run_row.status == "succeeded":
            print("\nfirst-import run already completed; no Google request made.")
            return EXIT_OK
        if run_row.status != "prepared":
            print(f"\nrun {run_row.id} status={run_row.status!r} is not executable.")
            return EXIT_STATE

        print("\n=== pre-execution audit ===")
        print(
            "import_run_count      = "
            f"{session.scalar(select(func.count()).select_from(SearchConsoleImportRun))}"
        )
        print(
            "page_metric_count     = "
            f"{session.scalar(select(func.count()).select_from(SearchConsolePageDaily))}"
        )
        print(
            "query_metric_count    = "
            f"{session.scalar(select(func.count()).select_from(SearchConsoleQueryDaily))}"
        )

        try:
            provider = provider_factory()
        except SearchConsoleCredentialError as exc:
            print(f"CREDENTIAL ERROR: {exc}")
            return EXIT_NOT_CONFIGURED
        except ExternalProviderError as exc:
            print(f"PROVIDER ERROR: {exc}")
            return EXIT_PROVIDER_ERROR

        try:
            out = svc.execute(run_row.id, provider=provider)
        except (ExternalProviderError, ExternalProviderDataError) as exc:
            print(f"IMPORT FAILED: {exc}")
            return EXIT_PROVIDER_ERROR
        except SearchConsoleImportStateError as exc:
            print(f"IMPORT STATE ERROR: {exc}")
            return EXIT_STATE

        print("\n=== executed run ===")
        print(f"run_id                = {out.id}")
        print(f"status                = {out.status}")
        print(f"started_at            = {out.started_at}")
        print(f"finished_at           = {out.finished_at}")
        print(f"error_message         = {out.error_message}")
        print(f"page_rows_received    = {out.page_rows_received}")
        print(f"query_rows_received   = {out.query_rows_received}")
        print(f"page_rows_upserted    = {out.page_rows_upserted}")
        print(f"query_rows_upserted   = {out.query_rows_upserted}")
        req_count = getattr(provider, "search_console_request_count", None)
        tok_count = getattr(provider, "token_refresh_count", None)
        print(f"search_console_request_count = {req_count}")
        print(f"token_refresh_count   = {tok_count}")
        if (out.page_rows_received or 0) == 0 and (out.query_rows_received or 0) == 0:
            print("zero-row import: SUCCESS (no metric rows fabricated).")
        return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="import_search_console",
        description="最初の本番 Search Console インポート (管理用 CLI)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="実インポートを行う (指定しなければプラン表示のみ・無通信・無書込)",
    )
    args = parser.parse_args(argv)
    try:
        return run(execute=args.execute)
    except Exception as exc:  # noqa: BLE001 - admin CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
