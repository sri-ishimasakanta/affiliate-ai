"""scripts/analyze_google_ads_keywords.py の分析用 CLI を検証する。

Google Ads API への実通信は行わない (Fake provider / dummy Settings のみ)。
"""

import pytest

from app.exceptions import ExternalProviderError, ProviderNotConfiguredError
from app.keyword.providers.google_ads import (
    GoogleAdsKeywordMetrics,
    MonthlySearchVolume,
)
from scripts.analyze_google_ads_keywords import (
    EXIT_NO_DATA,
    EXIT_NOT_CONFIGURED,
    EXIT_OK,
    EXIT_PROVIDER_ERROR,
    EXIT_UNEXPECTED,
    build_rows,
    main,
    render_table,
    run_analysis,
)
from tests.support.google_ads_fakes import (
    FakeGoogleAdsProvider,
    dummy_google_ads_settings,
    unconfigured_settings,
)

_SECRETS = (
    "dummy-developer-token",
    "dummy-client-id",
    "dummy-client-secret",
    "dummy-refresh-token",
)


def _metrics(
    keyword: str,
    *,
    avg: int | None = 1300,
    competition: str | None = "MEDIUM",
    competition_index: int | None = 45,
    low_micros: int | None = 120_000,
    high_micros: int | None = 640_000,
    months: int = 2,
) -> GoogleAdsKeywordMetrics:
    volumes = tuple(
        MonthlySearchVolume(year=2025, month=m, monthly_searches=1000 + m * 100)
        for m in range(1, months + 1)
    )
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=avg,
        monthly_search_volumes=volumes,
        competition=competition,
        competition_index=competition_index,
        low_top_of_page_bid_micros=low_micros,
        high_top_of_page_bid_micros=high_micros,
    )


# --- 引数 -------------------------------------------------------------------
def test_main_requires_keyword_argument() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2  # argparse usage error


def test_main_passes_all_keywords_through(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run(keywords, **kwargs: object) -> int:
        captured["keywords"] = list(keywords)
        captured["output"] = kwargs.get("output")
        captured["show_months"] = kwargs.get("show_months")
        return EXIT_OK

    monkeypatch.setattr("scripts.analyze_google_ads_keywords.run_analysis", _fake_run)
    code = main(
        [
            "--keyword",
            "AI 議事録 おすすめ",
            "--keyword",
            "AI 議事録 無料",
            "--output",
            "out.csv",
            "--show-months",
            "6",
        ]
    )
    assert code == EXIT_OK
    assert captured["keywords"] == ["AI 議事録 おすすめ", "AI 議事録 無料"]
    assert captured["output"] == "out.csv"
    assert captured["show_months"] == 6


def test_blank_keywords_are_rejected(capsys) -> None:
    code = run_analysis(
        ["   ", ""], settings=dummy_google_ads_settings(), provider=FakeGoogleAdsProvider()
    )
    assert code == EXIT_UNEXPECTED
    assert "no valid keyword" in capsys.readouterr().err.lower()


# --- provider 呼び出し ----------------------------------------------------
def test_all_keywords_passed_to_provider_in_one_call() -> None:
    provider = FakeGoogleAdsProvider(
        metrics=[_metrics("AI 議事録 おすすめ"), _metrics("AI 議事録 無料")]
    )

    code = run_analysis(
        ["AI 議事録 おすすめ", "AI 議事録 無料", "AI 議事録 比較"],
        settings=dummy_google_ads_settings(),
        provider=provider,
    )

    assert code == EXIT_OK
    # keyword ごとに 1 回ずつではなく、1 リクエストで全部渡す
    assert provider.calls == [
        ["AI 議事録 おすすめ", "AI 議事録 無料", "AI 議事録 比較"]
    ]


def test_duplicate_keywords_are_deduplicated() -> None:
    provider = FakeGoogleAdsProvider(metrics=[_metrics("kw")])
    run_analysis(
        ["kw", " kw ", "kw"],
        settings=dummy_google_ads_settings(),
        provider=provider,
    )
    assert provider.calls == [["kw"]]


# --- 表形式データへの変換 ------------------------------------------------
def test_build_rows_converts_metrics_and_bids() -> None:
    rows = build_rows(
        [
            _metrics("kw1", low_micros=120_000, high_micros=640_000, months=3),
            _metrics("kw2", avg=None, low_micros=None, high_micros=None, months=0),
        ]
    )

    assert rows[0] == {
        "keyword": "kw1",
        "avg_monthly_searches": 1300,
        "competition": "MEDIUM",
        "competition_index": 45,
        "low_top_of_page_bid_micros": 120_000,
        "high_top_of_page_bid_micros": 640_000,
        "low_top_of_page_bid": 0.12,
        "high_top_of_page_bid": 0.64,
        "monthly_search_volumes_count": 3,
    }
    assert rows[1]["avg_monthly_searches"] is None
    assert rows[1]["low_top_of_page_bid"] is None
    assert rows[1]["high_top_of_page_bid"] is None
    assert rows[1]["monthly_search_volumes_count"] == 0


def test_render_table_contains_header_and_all_keywords() -> None:
    table = render_table(
        build_rows([_metrics("AI 議事録 おすすめ"), _metrics("AI 議事録 比較")])
    )
    assert "avg_searches" in table
    assert "low_bid" in table
    assert "AI 議事録 おすすめ" in table
    assert "AI 議事録 比較" in table
    # ヘッダ行の下に区切り線がある
    assert "---" in table


def test_run_analysis_prints_table(capsys) -> None:
    code = run_analysis(
        ["AI 議事録 おすすめ"],
        settings=dummy_google_ads_settings(),
        provider=FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 おすすめ")]),
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "Google Ads keyword metrics analysis" in out
    assert "avg_searches" in out
    assert "0.12" in out  # low_bid = 120000 / 1_000_000
    assert "0.64" in out  # high_bid = 640000 / 1_000_000
    assert out.strip().endswith("OK")


# --- CSV 出力 -----------------------------------------------------------
def test_csv_output(tmp_path, capsys) -> None:
    out_file = tmp_path / "metrics.csv"
    code = run_analysis(
        ["kw1", "kw2"],
        settings=dummy_google_ads_settings(),
        provider=FakeGoogleAdsProvider(
            metrics=[
                _metrics("kw1"),
                _metrics("kw2", avg=None, low_micros=None, high_micros=None, months=0),
            ]
        ),
        output=str(out_file),
    )

    assert code == EXIT_OK
    text = out_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == (
        "keyword,avg_monthly_searches,competition,competition_index,"
        "low_top_of_page_bid_micros,high_top_of_page_bid_micros,"
        "low_top_of_page_bid,high_top_of_page_bid,monthly_search_volumes_count"
    )
    assert lines[1] == "kw1,1300,MEDIUM,45,120000,640000,0.12,0.64,2"
    # None は空欄で書き出す
    assert lines[2] == "kw2,,MEDIUM,45,,,,,0"
    assert "wrote 2 row(s) to" in capsys.readouterr().out


# --- monthly 表示 -----------------------------------------------------
def test_show_months_prints_recent_only(capsys) -> None:
    run_analysis(
        ["kw"],
        settings=dummy_google_ads_settings(),
        provider=FakeGoogleAdsProvider(metrics=[_metrics("kw", months=6)]),
        show_months=2,
    )
    out = capsys.readouterr().out
    assert "monthly_search_volumes (last 2):" in out
    assert "2025-06: 1600" in out
    assert "2025-05: 1500" in out
    assert "2025-04: 1400" not in out


def test_no_monthly_section_by_default(capsys) -> None:
    run_analysis(
        ["kw"],
        settings=dummy_google_ads_settings(),
        provider=FakeGoogleAdsProvider(metrics=[_metrics("kw", months=6)]),
    )
    assert "monthly_search_volumes (last" not in capsys.readouterr().out


# --- エラー系 ---------------------------------------------------------
def test_not_configured(capsys) -> None:
    code = run_analysis(["kw"], settings=unconfigured_settings())
    assert code == EXIT_NOT_CONFIGURED
    assert "not configured" in capsys.readouterr().err.lower()


def test_provider_not_configured_at_request_time() -> None:
    code = run_analysis(
        ["kw"],
        settings=dummy_google_ads_settings(),
        provider=FakeGoogleAdsProvider(error=ProviderNotConfiguredError("google_ads")),
    )
    assert code == EXIT_NOT_CONFIGURED


def test_provider_error(capsys) -> None:
    code = run_analysis(
        ["kw"],
        settings=dummy_google_ads_settings(),
        provider=FakeGoogleAdsProvider(
            error=ExternalProviderError("google_ads", "Google Ads API request failed")
        ),
    )
    assert code == EXIT_PROVIDER_ERROR
    assert "Google Ads request failed" in capsys.readouterr().err


def test_empty_result(capsys) -> None:
    code = run_analysis(
        ["kw"],
        settings=dummy_google_ads_settings(),
        provider=FakeGoogleAdsProvider(metrics=[]),
    )
    assert code == EXIT_NO_DATA
    assert "no historical metrics" in capsys.readouterr().err.lower()


def test_unexpected_exception_prints_only_type_name(capsys) -> None:
    class _Boom:
        def fetch_historical_metrics(self, keywords: list[str]) -> list:
            raise RuntimeError("developer_token=SUPER_SECRET refresh_token=RT")

    code = run_analysis(
        ["kw"], settings=dummy_google_ads_settings(), provider=_Boom()
    )
    assert code == EXIT_UNEXPECTED
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "SUPER_SECRET" not in combined
    assert "RuntimeError" in combined


# --- secret 非出力 --------------------------------------------------
@pytest.mark.parametrize(
    "provider",
    [
        FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 おすすめ")]),
        FakeGoogleAdsProvider(
            error=ExternalProviderError("google_ads", "Google Ads API request failed")
        ),
        FakeGoogleAdsProvider(metrics=[]),
    ],
)
def test_no_credentials_are_ever_printed(
    capsys, tmp_path, provider: FakeGoogleAdsProvider
) -> None:
    out_file = tmp_path / "metrics.csv"
    run_analysis(
        ["AI 議事録 おすすめ"],
        settings=dummy_google_ads_settings(),
        provider=provider,
        output=str(out_file),
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    if out_file.exists():
        combined += out_file.read_text(encoding="utf-8")
    for secret in _SECRETS:
        assert secret not in combined
    assert "1234567890" not in combined  # customer_id も出さない
