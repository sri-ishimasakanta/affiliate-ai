"""複数キーワードの Google Ads Keyword Historical Metrics をまとめて取得・比較する分析用 CLI。

    GoogleAdsKeywordMetricsProvider
        -> GenerateKeywordHistoricalMetrics (1 リクエストで複数 keyword)
        -> GoogleAdsKeywordMetrics DTO
        -> 表形式 / CSV

Phase 2B-3 (commercial_intent) の前段として、実データの分布を観察するための道具。
DB (Session / Keyword / KeywordSignal / KeywordScore / commit) には一切触れない。
commercial_intent / competition_ease の算出も行わない。

    uv run python scripts/analyze_google_ads_keywords.py \
        --keyword "AI 議事録 おすすめ" \
        --keyword "AI 議事録 無料" \
        --keyword "AI 議事録 比較"

secret (developer token / client id / client secret / refresh token / OAuth
credential 全体 / customer_id) は成功時・例外時いずれも出力しない。
"""

from __future__ import annotations

import argparse
import csv
import sys
import unicodedata
from collections.abc import Sequence
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

_MICROS_PER_UNIT = 1_000_000

# CSV / 内部 row の列 (フル名)。表示用の短いヘッダは _TABLE_HEADERS 側で対応付ける。
_CSV_FIELDS: tuple[str, ...] = (
    "keyword",
    "avg_monthly_searches",
    "competition",
    "competition_index",
    "low_top_of_page_bid_micros",
    "high_top_of_page_bid_micros",
    "low_top_of_page_bid",
    "high_top_of_page_bid",
    "monthly_search_volumes_count",
)

# (row のキー, 表示ヘッダ)。表は幅を抑えるため短いヘッダを使う。
_TABLE_HEADERS: tuple[tuple[str, str], ...] = (
    ("keyword", "keyword"),
    ("avg_monthly_searches", "avg_searches"),
    ("competition", "competition"),
    ("competition_index", "comp_index"),
    ("low_top_of_page_bid_micros", "low_bid_micros"),
    ("high_top_of_page_bid_micros", "high_bid_micros"),
    ("low_top_of_page_bid", "low_bid"),
    ("high_top_of_page_bid", "high_bid"),
    ("monthly_search_volumes_count", "months"),
)


class _Provider:
    """``fetch_historical_metrics`` を持つ最小プロトコル (型注釈用)。"""

    def fetch_historical_metrics(
        self, keywords: list[str]
    ) -> list[GoogleAdsKeywordMetrics]:  # pragma: no cover - protocol
        ...


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="analyze_google_ads_keywords",
        description=(
            "複数キーワードの Google Ads Keyword Historical Metrics を "
            "1 リクエストでまとめて取得し表形式で比較する (DB 保存なし)"
        ),
    )
    parser.add_argument(
        "--keyword",
        dest="keywords",
        action="append",
        required=True,
        metavar="KEYWORD",
        help="取得するキーワード。複数回指定できる (必須)",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="指定すると結果を CSV ファイルへ書き出す",
    )
    parser.add_argument(
        "--show-months",
        type=int,
        default=0,
        metavar="N",
        help="各キーワードの直近 N か月の monthly_search_volumes も表示する (既定 0)",
    )
    return parser.parse_args(argv)


def _clean_keywords(keywords: Sequence[str]) -> list[str]:
    """空白除去・空文字排除・重複排除 (入力順は維持)。"""

    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in keywords:
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned


def _micros_to_unit(micros: int | None) -> float | None:
    if micros is None:
        return None
    return round(micros / _MICROS_PER_UNIT, 2)


def build_rows(
    metrics: Sequence[GoogleAdsKeywordMetrics],
) -> list[dict[str, object]]:
    """DTO のリストを表 / CSV 用の素の dict へ変換する (SDK 非依存)。"""

    rows: list[dict[str, object]] = []
    for item in metrics:
        rows.append(
            {
                "keyword": item.keyword,
                "avg_monthly_searches": item.avg_monthly_searches,
                "competition": item.competition,
                "competition_index": item.competition_index,
                "low_top_of_page_bid_micros": item.low_top_of_page_bid_micros,
                "high_top_of_page_bid_micros": item.high_top_of_page_bid_micros,
                "low_top_of_page_bid": _micros_to_unit(item.low_top_of_page_bid_micros),
                "high_top_of_page_bid": _micros_to_unit(item.high_top_of_page_bid_micros),
                "monthly_search_volumes_count": len(item.monthly_search_volumes),
            }
        )
    return rows


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _display_width(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def render_table(rows: Sequence[dict[str, object]]) -> str:
    """row の並びを固定幅の比較表 (文字列) に整形する。"""

    keys = [key for key, _ in _TABLE_HEADERS]
    matrix: list[list[str]] = [[label for _, label in _TABLE_HEADERS]]
    matrix.extend([_cell(row[key]) for key in keys] for row in rows)
    widths = [
        max(_display_width(matrix[r][c]) for r in range(len(matrix)))
        for c in range(len(keys))
    ]
    lines: list[str] = []
    for index, record in enumerate(matrix):
        lines.append(
            "  ".join(_pad(record[c], widths[c]) for c in range(len(keys))).rstrip()
        )
        if index == 0:
            lines.append("  ".join("-" * widths[c] for c in range(len(keys))))
    return "\n".join(lines)


def _write_csv(path: Path, rows: Sequence[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {key: ("" if row[key] is None else row[key]) for key in _CSV_FIELDS}
            )


def _print_monthly(
    metrics: Sequence[GoogleAdsKeywordMetrics], show_months: int
) -> None:
    print()
    print(f"monthly_search_volumes (last {show_months}):")
    for item in metrics:
        print(f"  {item.keyword}:")
        recent = item.monthly_search_volumes[-show_months:]
        if not recent:
            print("    (no monthly data)")
            continue
        for volume in recent:
            print(f"    {volume.year}-{volume.month:02d}: {volume.monthly_searches}")


def run_analysis(
    keywords: Sequence[str],
    *,
    settings: Settings | None = None,
    provider: _Provider | None = None,
    show_months: int = 0,
    output: str | Path | None = None,
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

    cleaned = _clean_keywords(keywords)
    if not cleaned:
        print(
            "No valid keyword given (all --keyword values were empty).",
            file=sys.stderr,
        )
        return EXIT_UNEXPECTED

    active_provider: _Provider = (
        provider
        if provider is not None
        else GoogleAdsKeywordMetricsProvider(config)
    )

    print("Google Ads keyword metrics analysis")
    print("  configured : yes")
    print(f"  keywords   : {len(cleaned)}")
    for keyword in cleaned:
        print(f"    - {keyword}")

    try:
        # 1 リクエストで全 keyword を渡す (keyword ごとに呼ばない)。
        results = active_provider.fetch_historical_metrics(list(cleaned))
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
    except Exception as exc:  # 内部詳細・credential を出さない。型名のみ表示する。
        print(
            f"Unexpected error during analysis ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return EXIT_UNEXPECTED

    if not results:
        print(
            f"  no historical metrics returned for {len(cleaned)} keyword(s)",
            file=sys.stderr,
        )
        return EXIT_NO_DATA

    rows = build_rows(results)
    print()
    print("# low_bid / high_bid = *_top_of_page_bid_micros / 1,000,000")
    print(render_table(rows))

    if show_months > 0:
        _print_monthly(results, show_months)

    if output is not None:
        path = Path(output)
        _write_csv(path, rows)
        print()
        print(f"wrote {len(rows)} row(s) to {path}")

    if all(row["avg_monthly_searches"] is None for row in rows):
        print(
            "  warning: no avg_monthly_searches in any result "
            "(keywords may have no Google Ads historical metrics).",
            file=sys.stderr,
        )

    print("OK")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return run_analysis(
        args.keywords,
        show_months=args.show_months,
        output=args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
