"""Google Ads 実接続 Smoke Test。

実 credential をローカル ``.env`` に設定した状態で

    GoogleAdsKeywordMetricsProvider
        -> GenerateKeywordHistoricalMetrics
        -> GoogleAdsKeywordMetrics DTO

まで **実通信** できることだけを確認する。DB (Session / Keyword / KeywordSignal /
commit) には一切触れない。

    uv run python scripts/check_google_ads.py --keyword "AI 議事録 おすすめ"

secret (developer token / client id / client secret / refresh token / OAuth
credential 全体) は成功時・例外時いずれも標準出力しない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import Settings, get_settings  # noqa: E402
from app.exceptions import (  # noqa: E402
    ExternalProviderDataError,
    ExternalProviderError,
    ProviderNotConfiguredError,
)
from app.keyword.providers.google_ads import (  # noqa: E402
    GoogleAdsKeywordMetrics,
    GoogleAdsKeywordMetricsProvider,
)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_NOT_CONFIGURED = 2
EXIT_PROVIDER_ERROR = 3
EXIT_NO_DATA = 4


class _Provider:
    """`fetch_historical_metrics` を持つ最小プロトコル (型注釈用)。"""

    def fetch_historical_metrics(
        self, keywords: list[str]
    ) -> list[GoogleAdsKeywordMetrics]:  # pragma: no cover - protocol
        ...


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_google_ads",
        description="Google Ads Keyword Historical Metrics の実接続 Smoke Test (DB 保存なし)",
    )
    parser.add_argument(
        "--keyword",
        required=True,
        help="Historical metrics を取得するキーワード (必須)",
    )
    parser.add_argument(
        "--show-months",
        type=int,
        default=0,
        metavar="N",
        help="直近 N か月の monthly_search_volumes も表示する (既定 0 = 表示しない)",
    )
    return parser.parse_args(argv)


def _print_metrics(metrics: GoogleAdsKeywordMetrics, index: int, show_months: int) -> None:
    print(f"  [{index}] keyword                     : {metrics.keyword}")
    print(f"      avg_monthly_searches        : {metrics.avg_monthly_searches}")
    print(f"      competition                 : {metrics.competition}")
    print(f"      competition_index           : {metrics.competition_index}")
    print(f"      low_top_of_page_bid_micros  : {metrics.low_top_of_page_bid_micros}")
    print(f"      high_top_of_page_bid_micros : {metrics.high_top_of_page_bid_micros}")
    print(
        f"      monthly_search_volumes      : {len(metrics.monthly_search_volumes)} months"
    )
    if show_months > 0 and metrics.monthly_search_volumes:
        recent = metrics.monthly_search_volumes[-show_months:]
        for volume in recent:
            print(
                f"        - {volume.year}-{volume.month:02d}: {volume.monthly_searches}"
            )


def run_smoke_test(
    keyword: str,
    *,
    settings: Settings | None = None,
    provider: _Provider | None = None,
    show_months: int = 0,
) -> int:
    config = settings if settings is not None else get_settings()

    if not config.google_ads_configured:
        print(
            "Google Ads is not configured. "
            "Set GOOGLE_ADS_DEVELOPER_TOKEN / GOOGLE_ADS_CLIENT_ID / "
            "GOOGLE_ADS_CLIENT_SECRET / GOOGLE_ADS_REFRESH_TOKEN / "
            "GOOGLE_ADS_CUSTOMER_ID in .env (values are never printed).",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED

    active_provider: _Provider = (
        provider
        if provider is not None
        else GoogleAdsKeywordMetricsProvider(config)
    )

    print("Google Ads smoke test")
    print("  configured : yes")
    print(f"  keyword    : {keyword}")

    try:
        results = active_provider.fetch_historical_metrics([keyword])
    except ProviderNotConfiguredError:
        print(
            "Google Ads is not configured (checked at request time). "
            "Set the GOOGLE_ADS_* variables in .env.",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED
    except ExternalProviderError as exc:
        # exc のメッセージは provider が固定した安全な文言のみ (SDK 詳細・credential なし)
        print(f"Google Ads request failed: {exc}", file=sys.stderr)
        return EXIT_PROVIDER_ERROR
    except ExternalProviderDataError as exc:
        print(f"Google Ads returned no usable metrics: {exc}", file=sys.stderr)
        return EXIT_NO_DATA
    except Exception as exc:
        # 内部詳細・credential を出さないため、メッセージではなく型名のみ表示する。
        print(
            f"Unexpected error during smoke test ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return EXIT_UNEXPECTED

    if not results:
        print(
            f"  no historical metrics returned for keyword {keyword!r}",
            file=sys.stderr,
        )
        return EXIT_NO_DATA

    print(f"  --- results ({len(results)}) ---")
    for position, metrics in enumerate(results, start=1):
        _print_metrics(metrics, position, show_months)

    if all(m.avg_monthly_searches is None for m in results):
        print(
            "  warning: no avg_monthly_searches in any result "
            "(keyword may have no Google Ads historical metrics).",
            file=sys.stderr,
        )
        return EXIT_NO_DATA

    print("OK")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return run_smoke_test(args.keyword, show_months=args.show_months)


if __name__ == "__main__":
    raise SystemExit(main())
