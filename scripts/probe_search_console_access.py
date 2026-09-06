"""Google Search Console 実接続 read-only Access Probe。

ローカル ``.env`` に

    SEARCH_CONSOLE_PROPERTY_URI=sc-domain:<host>
    SEARCH_CONSOLE_CREDENTIALS_FILE=<repo外のservice account JSONへのパス>

を設定した状態で、

    SearchConsoleAccessClient.list_sites()
        -> GET https://www.googleapis.com/webmasters/v3/sites (Sites.list)

まで **実通信 1 回だけ** できること、および設定した property に read-only アクセス
権限があることを確認する。

    uv run python scripts/probe_search_console_access.py

Search Analytics は呼ばない。SearchConsoleImportRun は作らない。metric は書かない。
sitemap / indexing / property / user は変更しない。DB (Session) には一切触れない。
secret (private_key / private_key_id / access token / Authorization / JSON 全体) は
成功時・例外時いずれも標準出力しない。
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
from app.search_console.google_client import SearchConsoleAccessClient  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_NOT_CONFIGURED = 2
EXIT_PROVIDER_ERROR = 3
EXIT_PROPERTY_ABSENT = 4


def main() -> int:
    settings = get_settings()

    if not settings.search_console_property_uri:
        print("NOT CONFIGURED: SEARCH_CONSOLE_PROPERTY_URI is not set")
        return EXIT_NOT_CONFIGURED
    if not settings.search_console_credentials_file:
        print("NOT CONFIGURED: SEARCH_CONSOLE_CREDENTIALS_FILE is not set")
        return EXIT_NOT_CONFIGURED

    print(f"configured_property_uri = {settings.search_console_property_uri}")

    try:
        client = SearchConsoleAccessClient(
            credentials_file=settings.search_console_credentials_file,
            property_uri=settings.search_console_property_uri,
        )
    except SearchConsoleCredentialError as exc:
        print(f"CREDENTIAL ERROR: {exc}")
        return EXIT_NOT_CONFIGURED

    # 安全な事実のみ出力 (client_email はローカル運用者にとって必要な非 secret)。
    print(f"service_account_email = {client.service_account_email}")

    try:
        result = client.list_sites()  # exactly ONE real Google request
    except (ExternalProviderError, ExternalProviderDataError) as exc:
        print(f"PROVIDER ERROR: {exc}")
        return EXIT_PROVIDER_ERROR
    except Exception as exc:  # noqa: BLE001 - probe は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}")
        return EXIT_UNEXPECTED

    print("real_google_requests = 1")
    print(f"http_ok = {result.http_ok}")
    print(f"accessible_property_count = {result.accessible_property_count}")
    print(f"configured_property_found = {result.configured_property_found}")
    print(f"configured_permission_level = {result.configured_permission_level!r}")

    if not result.configured_property_found:
        print("FAIL: configured property is not accessible to this service account")
        return EXIT_PROPERTY_ABSENT

    print("OK: read-only access to the configured Search Console property is confirmed")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
