"""Google Search Console 実接続 read-only Search Analytics Access Probe (C1-B)。

目的は **権限 / 認証 / リクエスト contract / レスポンス parsing の実証** であって
データ収集ではない。

    uv run python scripts/probe_search_console_analytics.py

- Search Console API リクエストは **ちょうど 2 回** だけ:
    1. page dataset   dimensions=["date","page"]
    2. query dataset  dimensions=["date","page","query"]
  いずれも ``rowLimit=1`` / ``startRow=0`` で **pagination しない**。
- type="web" / aggregationType="auto" / dataState="final"。
- 期間は America/Los_Angeles 暦での直近 7 日 (末尾は前日以前)。
- DB (Session) には一切触れない。metric も import run も作らない。
- Search Console を変更しない。sitemap / indexing を触らない。
- secret (private_key / private_key_id / access token / Authorization / JSON 全体) は
  成功時・例外時いずれも標準出力しない。実際の検索クエリ文字列も出力しない。
- どちらかが 403 の場合は STOP。権限を自動変更しない。どちらが失敗したか報告する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402
from app.exceptions import (  # noqa: E402
    ExternalProviderDataError,
    ExternalProviderError,
    SearchConsoleCredentialError,
)
from app.search_console.date_window import recent_window  # noqa: E402
from app.search_console.google_provider import (  # noqa: E402
    GoogleSearchConsoleProvider,
)
from app.search_console.import_identity import (  # noqa: E402
    PAGE_DIMENSIONS,
    QUERY_DIMENSIONS,
)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_NOT_CONFIGURED = 2
EXIT_PROVIDER_ERROR = 3
EXIT_PERMISSION_DENIED = 4


def _run_one(provider: GoogleSearchConsoleProvider, *, label: str, dimensions, window):
    start_date, end_date = window
    try:
        result = provider.probe_query(
            property_uri=get_settings().search_console_property_uri,
            start_date=start_date,
            end_date=end_date,
            dimensions=dimensions,
        )
    except ExternalProviderError as exc:
        msg = str(exc)
        denied = "(403)" in msg
        print(f"{label}_probe = FAIL ({'403 permission denied' if denied else msg})")
        return None, (EXIT_PERMISSION_DENIED if denied else EXIT_PROVIDER_ERROR)
    except ExternalProviderDataError as exc:
        print(f"{label}_probe = FAIL (data error: {exc})")
        return None, EXIT_PROVIDER_ERROR
    except Exception as exc:  # noqa: BLE001 - probe は安全側に倒す
        print(f"{label}_probe = UNEXPECTED ({type(exc).__name__})")
        return None, EXIT_UNEXPECTED

    print(f"{label}_probe = OK")
    print(f"  {label}_http_ok = {result.http_ok}")
    print(f"  {label}_rows_returned = {result.row_count}")
    print(f"  {label}_rows_parsed_ok = {result.rows_parsed_ok}")
    print(f"  {label}_responseAggregationType = {result.response_aggregation_type!r}")
    return result, EXIT_OK


def main() -> int:
    settings = get_settings()

    if not settings.search_console_property_uri:
        print("NOT CONFIGURED: SEARCH_CONSOLE_PROPERTY_URI is not set")
        return EXIT_NOT_CONFIGURED
    if not settings.search_console_credentials_file:
        print("NOT CONFIGURED: SEARCH_CONSOLE_CREDENTIALS_FILE is not set")
        return EXIT_NOT_CONFIGURED

    window = recent_window(days=7, end_lag_days=1)
    print(f"configured_property_uri = {settings.search_console_property_uri}")
    print(f"date_window = {window[0].isoformat()} .. {window[1].isoformat()} (PT)")
    print("query_contract = type=web aggregationType=auto dataState=final rowLimit=1")

    try:
        provider = GoogleSearchConsoleProvider(
            credentials_file=settings.search_console_credentials_file,
        )
    except SearchConsoleCredentialError as exc:
        print(f"CREDENTIAL ERROR: {exc}")
        return EXIT_NOT_CONFIGURED
    except ExternalProviderError as exc:
        print(f"PROVIDER ERROR: {exc}")
        return EXIT_PROVIDER_ERROR

    print(f"service_account_email = {provider.service_account_email}")

    page_res, page_exit = _run_one(
        provider, label="page", dimensions=PAGE_DIMENSIONS, window=window
    )
    query_res, query_exit = _run_one(
        provider, label="query", dimensions=QUERY_DIMENSIONS, window=window
    )

    print(f"search_console_request_count = {provider.search_console_request_count}")
    print(f"token_exchange_count = {provider.token_refresh_count}")

    if page_exit == EXIT_PERMISSION_DENIED or query_exit == EXIT_PERMISSION_DENIED:
        failed = []
        if page_exit == EXIT_PERMISSION_DENIED:
            failed.append("page")
        if query_exit == EXIT_PERMISSION_DENIED:
            failed.append("query")
        print(f"STOP: 403 permission denied for: {', '.join(failed)}")
        print(
            "Search Analytics read permission is NOT proven. "
            "Do not change Search Console permissions automatically; a human decides "
            "whether the service account must be promoted from Restricted to Full user."
        )
        return EXIT_PERMISSION_DENIED

    if page_exit != EXIT_OK:
        return page_exit
    if query_exit != EXIT_OK:
        return query_exit

    print(
        "OK: both Search Analytics requests succeeded -> "
        "Search Analytics read permission is empirically proven"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
