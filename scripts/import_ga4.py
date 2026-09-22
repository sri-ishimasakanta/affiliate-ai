"""管理用 CLI: GA4 (Google Analytics Data API) の日次取り込み。

    # プラン表示のみ (無通信・無書込)。--execute を付けなければ常に plan。
    uv run python scripts/import_ga4.py --days 30 --lag 1
    uv run python scripts/import_ga4.py --start-date 2026-09-01 --end-date 2026-09-21

    # 実インポート
    uv run python scripts/import_ga4.py --days 30 --lag 1 --execute

``scripts/import_search_console.py`` (C1.1) と同じ規約に揃える:

* 範囲指定は ``--start-date`` + ``--end-date`` か ``--days`` + ``--lag`` の排他。
* ``--execute`` を付けたときだけ Google を呼び、DB に書く。
* run の監査 idempotency とメトリクス行の idempotency は別物。メトリクス行は
  ``(property, date, page_path, channel_scope)`` の UPSERT なので、同じ / 重なる
  期間を再取り込みしても行は重複しない。
* 範囲の検証は DB にも Google にも触れる前に行う。

**日付は GA4 property のタイムゾーンの暦日** である。``--days/--lag`` は property の
タイムゾーンが判明していればそれを使い、未取得なら UTC 暦日で計算してその旨を
出力する (取り込んだ実タイムゾーンは run に記録される)。

secret (credential path / private key / access token) は plan / 成功 / 例外の
いずれでも出力しない。
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.analytics.google_provider import GoogleGa4Provider  # noqa: E402
from app.analytics.import_identity import (  # noqa: E402
    CHANNEL_SCOPES,
    IMPORT_VERSION,
    PAGE_DIMENSIONS,
    PAGE_METRICS,
)
from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.exceptions import (  # noqa: E402
    ExternalProviderDataError,
    ExternalProviderError,
    Ga4ImportStateError,
    SearchConsoleCredentialError,
)
from app.models import Ga4ImportRun, Ga4PageDaily  # noqa: E402
from app.services.ga4_import_service import Ga4ImportService  # noqa: E402

EXIT_OK = 0
EXIT_INVALID_RANGE = 2
EXIT_NOT_CONFIGURED = 3
EXIT_PROVIDER_FAILED = 4

_DEFAULT_DAYS = 30
_DEFAULT_LAG = 1


def _property_today(timezone_name: str | None) -> tuple[date, str]:
    """property のタイムゾーン暦日 (不明なら UTC 暦日) と、使った基準の説明。"""

    if timezone_name:
        try:
            return datetime.now(tz=ZoneInfo(timezone_name)).date(), timezone_name
        except Exception:  # noqa: BLE001 - 不正な tz 名は UTC にフォールバック
            pass
    return datetime.now(tz=UTC).date(), "UTC (property timezone not yet known)"


def _resolve_window(args, timezone_name: str | None) -> tuple[date, date, str, str]:
    explicit = args.start_date is not None or args.end_date is not None
    rolling = args.days is not None or args.lag is not None
    if explicit and rolling:
        raise ValueError("--start-date/--end-date and --days/--lag are mutually exclusive")
    if explicit:
        if args.start_date is None or args.end_date is None:
            raise ValueError("--start-date and --end-date must both be given")
        start = _parse_date(args.start_date)
        end = _parse_date(args.end_date)
        basis = "explicit range"
    else:
        days = args.days if args.days is not None else _DEFAULT_DAYS
        lag = args.lag if args.lag is not None else _DEFAULT_LAG
        if days < 1:
            raise ValueError("--days must be >= 1")
        if lag < 0:
            raise ValueError("--lag must be >= 0")
        today, basis = _property_today(timezone_name)
        end = today - timedelta(days=lag)
        start = end - timedelta(days=days - 1)
        return start, end, "rolling-window", basis
    if start > end:
        raise ValueError("start_date is after end_date")
    return start, end, basis, "explicit"


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"invalid date: {value!r} (expected YYYY-MM-DD)") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", help="YYYY-MM-DD (property timezone calendar day)")
    parser.add_argument("--end-date", help="YYYY-MM-DD (property timezone calendar day)")
    parser.add_argument("--days", type=int, help="末尾から遡る日数 (--lag と併用)")
    parser.add_argument("--lag", type=int, help="末尾を今日から何日前にするか")
    parser.add_argument("--execute", action="store_true", help="実際に取り込む")
    args = parser.parse_args(argv)

    settings = get_settings()
    property_id = (settings.ga4_property_id or "").strip()

    timezone_name = None
    with SessionLocal() as session:
        latest = session.scalars(
            select(Ga4ImportRun)
            .where(Ga4ImportRun.property_timezone.is_not(None))
            .order_by(Ga4ImportRun.id.desc())
            .limit(1)
        ).first()
        if latest is not None:
            timezone_name = latest.property_timezone

    try:
        start, end, mode, basis = _resolve_window(args, timezone_name)
    except ValueError as exc:
        print(f"invalid range: {exc}")
        return EXIT_INVALID_RANGE

    print("=== GA4 import (PLAN) ===" if not args.execute else "=== GA4 import ===")
    print(f"mode                   = {mode}")
    print(f"property_id            = {property_id or '(not configured)'}")
    print(f"calendar basis         = {basis}")
    print(f"start_date             = {start.isoformat()}")
    print(f"end_date               = {end.isoformat()}")
    print(f"import_version         = {IMPORT_VERSION}")
    print(f"dimensions             = {list(PAGE_DIMENSIONS)}")
    print(f"metrics                = {list(PAGE_METRICS)}")
    print(f"channel_scopes         = {list(CHANNEL_SCOPES)}")
    print("metric_rows            = upsert by (property, date, page_path, scope)")
    print(f"credentials_configured = {bool(settings.search_console_credentials_file)}")

    if not property_id:
        print(
            "\nGA4 property is not configured (GA4_PROPERTY_ID). "
            "No Google request and no DB write were made."
        )
        return EXIT_NOT_CONFIGURED

    if not args.execute:
        print("\nplan only: no Google request, no DB write. pass --execute to import.")
        return EXIT_OK

    with SessionLocal() as session:
        try:
            run = Ga4ImportService(session).prepare(
                start_date=start, end_date=end, property_id=property_id
            )
        except Ga4ImportStateError as exc:
            print(f"\nprepare refused: {exc.reason}")
            return EXIT_INVALID_RANGE
        run_id = run.id
        print(f"\n=== prepared run ===\nrun_id                = {run_id}")
        print(f"status                = {run.status}")
        print(f"import_identity_hash  = {run.import_identity_hash}")

    with SessionLocal() as session:
        before_rows = session.scalar(select(func.count()).select_from(Ga4PageDaily))
        print(f"\n=== pre-execution audit ===\npage_metric_count     = {before_rows}")

    with SessionLocal() as session:
        provider = GoogleGa4Provider(settings)
        try:
            run = Ga4ImportService(session).execute(run_id, provider=provider)
        except (
            ExternalProviderError,
            ExternalProviderDataError,
            SearchConsoleCredentialError,
        ) as exc:
            print(f"\nprovider read failed (run {run_id} marked failed): {exc}")
            return EXIT_PROVIDER_FAILED
        print("\n=== executed run ===")
        print(f"run_id                = {run.id}")
        print(f"status                = {run.status}")
        print(f"property_timezone     = {run.property_timezone}")
        print(f"page_rows_received    = {run.page_rows_received}")
        print(f"page_rows_upserted    = {run.page_rows_upserted}")
        print(f"data_through_date     = {run.data_through_date}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
