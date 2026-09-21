"""管理用 CLI: Search Console インポート (最初の本番インポート + 任意期間の refresh)。

    # プラン表示のみ (無通信・無書込)。--execute を付けなければ常に plan。
    uv run python scripts/import_search_console.py
    uv run python scripts/import_search_console.py --start-date 2026-09-06 --end-date 2026-09-20
    uv run python scripts/import_search_console.py --days 28 --lag 3

    # 実インポート
    uv run python scripts/import_search_console.py \
        --start-date 2026-09-06 --end-date 2026-09-20 --execute

範囲の指定方法 (排他。どれか 1 つ、または無指定):

* ``--start-date`` + ``--end-date`` (YYYY-MM-DD, Pacific Time 暦日, 両方必須): 明示範囲。
* ``--days`` + ``--lag`` (両方必須): PT 暦で「今日(PT) - lag」を末尾とする直近 days 日。
* 無指定: 従来の最初の本番インポート (直近 7 日 / lag 1 / 固定 idempotency key)。

**run の監査 idempotency とメトリクス行の idempotency は別物。** 明示範囲 / 直近 N 日の
refresh は idempotency key を使わず、実行のたびに新しい ``SearchConsoleImportRun`` を
append する (監査可能)。メトリクス行は ``(property, date, page[, query])`` の UPSERT で
あり、同じ / 重なる範囲を再取り込みしても行は重複しない。無指定の最初の本番インポート
だけが固定 idempotency key を使い、同じコマンドの誤再実行で Google を二重に叩かない。

範囲の検証 (片方だけ / 不正な日付 / start > end / 未来の end / モード競合) は DB にも
Google にも触れる前に行い、失敗時は ``EXIT_INVALID_RANGE`` を返す。

public HTTP endpoint ではない。provider / import のビジネスロジックは複製せず、
:class:`GoogleSearchConsoleProvider` と :class:`SearchConsoleImportService` に委譲する。

secret (private_key / private_key_id / access token / Authorization / credential
JSON / credential path) は plan / 成功 / 例外いずれでも出力しない。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
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
from app.search_console.date_window import (  # noqa: E402
    recent_window,
    search_console_today,
)
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
EXIT_INVALID_RANGE = 6

MODE_FIRST_IMPORT = "first-import"
MODE_EXPLICIT = "explicit-range"
MODE_ROLLING = "rolling-window"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidRangeError(ValueError):
    """範囲指定が不正。DB / Google に触れる前に送出される。"""


@dataclass(frozen=True)
class ImportWindow:
    start_date: date
    end_date: date
    mode: str
    #: first-import だけが固定 key を持つ。refresh は None (実行ごとに新しい run)。
    idempotency_key: str | None


def _parse_date(value: object, *, name: str) -> date:
    if isinstance(value, datetime):
        raise InvalidRangeError(f"{name} must be a date (YYYY-MM-DD)")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _DATE_RE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise InvalidRangeError(f"{name} must be a valid date in YYYY-MM-DD form")


def resolve_window(
    *,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    days: int | None = None,
    lag: int | None = None,
    now: datetime | None = None,
) -> ImportWindow:
    """CLI の範囲指定を検証して 1 つの :class:`ImportWindow` に解決する (pure・通信なし)。"""

    has_dates = start_date is not None or end_date is not None
    has_rolling = days is not None or lag is not None

    if has_dates and has_rolling:
        raise InvalidRangeError(
            "conflicting range modes: use either --start-date/--end-date or --days/--lag, not both"
        )

    if has_dates:
        if start_date is None or end_date is None:
            raise InvalidRangeError("--start-date and --end-date must be provided together")
        start = _parse_date(start_date, name="--start-date")
        end = _parse_date(end_date, name="--end-date")
        if start > end:
            raise InvalidRangeError("start_date is after end_date")
        if end > search_console_today(now):
            raise InvalidRangeError("end_date is in the future (Pacific Time)")
        return ImportWindow(start, end, MODE_EXPLICIT, None)

    if has_rolling:
        if days is None or lag is None:
            raise InvalidRangeError("--days and --lag must be provided together")
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise InvalidRangeError("--days must be an integer >= 1")
        if isinstance(lag, bool) or not isinstance(lag, int) or lag < 0:
            raise InvalidRangeError("--lag must be an integer >= 0")
        start, end = recent_window(days=days, end_lag_days=lag, now=now)
        return ImportWindow(start, end, MODE_ROLLING, None)

    start, end = recent_window(days=_WINDOW_DAYS, end_lag_days=_WINDOW_END_LAG_DAYS, now=now)
    return ImportWindow(start, end, MODE_FIRST_IMPORT, FIRST_IMPORT_IDEMPOTENCY_KEY)


def _default_provider_factory(credentials_file: str | None):
    return GoogleSearchConsoleProvider(credentials_file=credentials_file)


def _print_plan(
    *, property_uri: str, start_date, end_date, mode: str, idempotency_key: str | None
) -> None:
    print("=== Search Console import (PLAN) ===")
    print(f"mode                   = {mode}")
    print(f"property_uri           = {property_uri}")
    print(f"start_date (PT)        = {start_date.isoformat()}")
    print(f"end_date (PT)          = {end_date.isoformat()}")
    print(f"import_version         = {IMPORT_VERSION}")
    print(f"page_dimensions        = {list(PAGE_DIMENSIONS)}")
    print(f"query_dimensions       = {list(QUERY_DIMENSIONS)}")
    print(f"data_state             = {DATA_STATE}")
    if idempotency_key is None:
        print("idempotency_key        = none (every execute appends a new audited run)")
    else:
        print(f"idempotency_key        = {idempotency_key}")
    print("metric_rows            = upsert by (property, date, page[, query]); no duplicates")


def run(
    *,
    execute: bool,
    settings=None,
    session_factory=SessionLocal,
    provider_factory=None,
    now: datetime | None = None,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    days: int | None = None,
    lag: int | None = None,
) -> int:
    # 範囲の検証は設定 / DB / Google のどれにも触れる前に行う。
    try:
        window = resolve_window(
            start_date=start_date, end_date=end_date, days=days, lag=lag, now=now
        )
    except InvalidRangeError as exc:
        print(f"INVALID RANGE: {exc}")
        return EXIT_INVALID_RANGE

    settings = settings or get_settings()
    property_uri = settings.search_console_property_uri
    credentials_file = settings.search_console_credentials_file

    if not property_uri:
        print("NOT CONFIGURED: SEARCH_CONSOLE_PROPERTY_URI is not set")
        return EXIT_NOT_CONFIGURED
    if not credentials_file:
        print("NOT CONFIGURED: SEARCH_CONSOLE_CREDENTIALS_FILE is not set")
        return EXIT_NOT_CONFIGURED

    _print_plan(
        property_uri=property_uri,
        start_date=window.start_date,
        end_date=window.end_date,
        mode=window.mode,
        idempotency_key=window.idempotency_key,
    )
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
                start_date=window.start_date,
                end_date=window.end_date,
                idempotency_key=window.idempotency_key,
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
        description=(
            "Search Console インポート (管理用 CLI)。範囲は --start-date/--end-date "
            "または --days/--lag のどちらか (無指定は従来の最初の本番インポート)。"
        ),
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="YYYY-MM-DD (Pacific Time)。--end-date と必ず一緒に指定する",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="YYYY-MM-DD (Pacific Time)。--start-date と必ず一緒に指定する",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="末尾 (今日(PT) - lag) から遡る日数。--lag と必ず一緒に指定する",
    )
    parser.add_argument(
        "--lag",
        type=int,
        default=None,
        help="今日(PT)から末尾までの日数 (>= 0)。--days と必ず一緒に指定する",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="実インポートを行う (指定しなければプラン表示のみ・無通信・無書込)",
    )
    args = parser.parse_args(argv)
    try:
        return run(
            execute=args.execute,
            start_date=args.start_date,
            end_date=args.end_date,
            days=args.days,
            lag=args.lag,
        )
    except Exception as exc:  # noqa: BLE001 - admin CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
