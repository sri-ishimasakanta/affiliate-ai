"""管理用 CLI: Google Ads Keyword Ideas (read-only) による keyword 候補の探索。

    # PLAN (既定): Google へ request を送らない / DB を開かない
    uv run python -m scripts.discover_keyword_ideas
    uv run python -m scripts.discover_keyword_ideas --cluster B --cluster C

    # 実行: 明示 --execute のみ。cluster (seed set) ごとに最大 1 request
    uv run python -m scripts.discover_keyword_ideas --execute --output ideas.json

- read-only: DB write なし (DB は一切開かない) / keyword 追加なし / Signal・スコア変更なし。
- request 上限: 1 cluster seed set につき ``GenerateKeywordIdeas`` 1 回 (合計 <= 選択 cluster 数)。
  認証エラー / API エラーが出た時点で残りの request は送らず終了する。
- credential / token / customer id は出力しない。エラーは固定の安全な文言のみ。
- 出力ファイル (``--output``) は新規作成のみ (既存ファイルは上書きしない)。
- 探索結果の dedupe / merge / reject 判定は ``scripts.plan_keyword_expansion`` が別途行う。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402
from app.exceptions import ExternalProviderError, ProviderNotConfiguredError  # noqa: E402
from app.keyword.idea_seeds import (  # noqa: E402
    MAX_RESULTS_LIMIT,
    IdeaSeedConfigError,
    IdeaSeedSet,
    load_idea_seed_config,
)
from app.keyword.providers.google_ads_ideas import (  # noqa: E402
    GoogleAdsAuthError,
    GoogleAdsKeywordIdea,
    GoogleAdsKeywordIdeaProvider,
)

DEFAULT_SEEDS = (
    Path(__file__).resolve().parent.parent / "app" / "config" / "keyword_idea_seeds.json"
)

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_NOT_CONFIGURED = 3
EXIT_AUTH = 4
EXIT_API = 5
EXIT_UNEXPECTED = 6
EXIT_OUTPUT = 7

_TOP_SHOWN = 10


def _metrics_dict(idea: GoogleAdsKeywordIdea) -> dict | None:
    m = idea.metrics
    if m is None:
        return None
    return {
        "avg_monthly_searches": m.avg_monthly_searches,
        "competition": m.competition,
        "competition_index": m.competition_index,
        "low_top_of_page_bid_micros": m.low_top_of_page_bid_micros,
        "high_top_of_page_bid_micros": m.high_top_of_page_bid_micros,
    }


def _print_plan(
    settings, sets: tuple[IdeaSeedSet, ...], max_results: int, *, execute: bool
) -> None:
    mode = "EXECUTE" if execute else "PLAN"
    print(f"=== Google Ads keyword-idea discovery ({mode}) ===")
    print(f"google_ads_configured  = {settings.google_ads_configured}")
    print(f"geo_target_id          = {settings.google_ads_geo_target_id}")
    print(f"language_id            = {settings.google_ads_language_id}")
    print(f"max_results_per_request = {max_results}")
    print(f"planned_requests       = {len(sets)} (at most 1 per cluster seed set)")
    for seed_set in sets:
        print(f"\n[{seed_set.cluster_id}] {seed_set.name}")
        print(f"    seeds: {', '.join(seed_set.seeds)}")
    if execute:
        print("\nread-only: no DB access, no keyword is added.")
    else:
        print("\nno Google request made, no DB access. Pass --execute to run the request(s).")


def _write_output(path: Path, ideas: list[dict], settings) -> None:
    payload = {
        "version": 1,
        "geo_target_id": settings.google_ads_geo_target_id,
        "language_id": settings.google_ads_language_id,
        "ideas": ideas,
    }
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def run(
    *,
    seeds_path: str | Path = DEFAULT_SEEDS,
    clusters: list[str] | None = None,
    max_results: int | None = None,
    execute: bool = False,
    output: str | Path | None = None,
    settings=None,
    provider_factory=None,
) -> int:
    try:
        config = load_idea_seed_config(seeds_path)
        sets = config.select(clusters)
    except IdeaSeedConfigError as exc:
        print("CONFIG ERROR:")
        for error in exc.errors:
            print(f"  - {error}")
        return EXIT_CONFIG

    limit = config.default_max_results if max_results is None else max_results
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS_LIMIT:
        print(f"CONFIG ERROR: --max-results must be an integer between 1 and {MAX_RESULTS_LIMIT}")
        return EXIT_CONFIG

    out_path = Path(output) if output is not None else None
    if execute and out_path is not None and (out_path.exists() or not out_path.parent.is_dir()):
        print(
            "OUTPUT ERROR: --output must be a new file in an existing directory (not overwritten)"
        )
        return EXIT_OUTPUT

    settings = settings or get_settings()
    _print_plan(settings, sets, limit, execute=execute)
    if not execute:
        return EXIT_OK

    if not settings.google_ads_configured:
        print(
            "NOT CONFIGURED: the GOOGLE_ADS_* settings are incomplete (values are never printed)."
        )
        return EXIT_NOT_CONFIGURED

    factory = provider_factory or (
        lambda s, max_requests: GoogleAdsKeywordIdeaProvider(s, max_requests=max_requests)
    )
    provider = factory(settings, len(sets))

    collected: list[dict] = []
    status = EXIT_OK
    for seed_set in sets:
        try:
            ideas = provider.generate_keyword_ideas(seed_set.seeds, max_results=limit)
        except ProviderNotConfiguredError:
            print("NOT CONFIGURED: Google Ads is not configured.")
            status = EXIT_NOT_CONFIGURED
            break
        except GoogleAdsAuthError as exc:
            print(f"AUTH ERROR: {exc}")
            status = EXIT_AUTH
            break
        except ExternalProviderError as exc:
            print(f"API ERROR: {exc}")
            status = EXIT_API
            break
        except ValueError:
            print("CONFIG ERROR: invalid seeds or parameters")
            status = EXIT_CONFIG
            break
        print(f"\n[{seed_set.cluster_id}] ideas returned: {len(ideas)}")
        ranked = sorted(
            ideas,
            key=lambda i: (-(i.metrics.avg_monthly_searches or 0) if i.metrics else 0, i.keyword),
        )
        for idea in ranked[:_TOP_SHOWN]:
            avg = idea.metrics.avg_monthly_searches if idea.metrics else None
            comp = idea.metrics.competition if idea.metrics else None
            print(
                f"    {idea.keyword} | avg={avg if avg is not None else 'not_available'} | "
                f"competition={comp or 'not_available'}"
            )
        for idea in ideas:
            collected.append(
                {
                    "cluster": seed_set.cluster_id,
                    "keyword": idea.keyword,
                    "metrics": _metrics_dict(idea),
                }
            )

    requests_made = getattr(provider, "request_count", None)
    print(f"\nkeyword_idea_requests_made = {requests_made}")
    if status == EXIT_OK and out_path is not None:
        try:
            _write_output(out_path, collected, settings)
        except OSError:
            print("OUTPUT ERROR: could not create the output file")
            return EXIT_OUTPUT
        print(f"wrote {len(collected)} idea(s) to {out_path}")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="discover_keyword_ideas",
        description=(
            "Read-only Google Ads keyword-idea discovery. PLAN by default (no Google request, "
            "no DB); --execute sends at most one GenerateKeywordIdeas request per cluster."
        ),
    )
    parser.add_argument("--seeds", default=str(DEFAULT_SEEDS))
    parser.add_argument("--cluster", action="append", dest="clusters", metavar="ID")
    parser.add_argument("--max-results", type=int, default=None)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", default=None, metavar="PATH")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    try:
        return run(
            seeds_path=args.seeds,
            clusters=args.clusters,
            max_results=args.max_results,
            execute=args.execute,
            output=args.output,
        )
    except Exception as exc:  # noqa: BLE001 - admin CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
