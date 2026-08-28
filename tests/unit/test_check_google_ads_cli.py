"""scripts/check_google_ads.py の Smoke Test CLI を検証する。

Google Ads API への実通信は行わない (Fake provider / dummy Settings のみ)。
"""

import pytest

from app.exceptions import ExternalProviderError, ProviderNotConfiguredError
from app.keyword.providers.google_ads import (
    GoogleAdsKeywordMetrics,
    MonthlySearchVolume,
)
from scripts.check_google_ads import (
    EXIT_NO_DATA,
    EXIT_NOT_CONFIGURED,
    EXIT_OK,
    EXIT_PROVIDER_ERROR,
    main,
    run_smoke_test,
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


def _metrics(keyword: str = "AI 議事録 おすすめ") -> GoogleAdsKeywordMetrics:
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=1300,
        monthly_search_volumes=(
            MonthlySearchVolume(year=2025, month=1, monthly_searches=1200),
            MonthlySearchVolume(year=2025, month=2, monthly_searches=1400),
        ),
        competition="MEDIUM",
        competition_index=45,
        low_top_of_page_bid_micros=120_000,
        high_top_of_page_bid_micros=640_000,
    )


def test_main_requires_keyword_argument() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2  # argparse usage error


def test_main_passes_keyword_to_provider(monkeypatch, capsys) -> None:
    provider = FakeGoogleAdsProvider(metrics=[_metrics("my keyword")])
    captured: dict[str, object] = {}

    def _fake_run(keyword: str, **kwargs: object) -> int:
        captured["keyword"] = keyword
        return EXIT_OK

    monkeypatch.setattr("scripts.check_google_ads.run_smoke_test", _fake_run)
    assert main(["--keyword", "my keyword"]) == EXIT_OK
    assert captured["keyword"] == "my keyword"
    assert provider.calls == []  # 差し替えたので呼ばれない


def test_run_smoke_test_prints_safe_fields_on_success(capsys) -> None:
    provider = FakeGoogleAdsProvider(metrics=[_metrics()])

    code = run_smoke_test(
        "AI 議事録 おすすめ",
        settings=dummy_google_ads_settings(),
        provider=provider,
    )

    assert code == EXIT_OK
    assert provider.calls == [["AI 議事録 おすすめ"]]
    out = capsys.readouterr().out
    assert "avg_monthly_searches        : 1300" in out
    assert "competition                 : MEDIUM" in out
    assert "competition_index           : 45" in out
    assert "low_top_of_page_bid_micros  : 120000" in out
    assert "high_top_of_page_bid_micros : 640000" in out
    assert "monthly_search_volumes      : 2 months" in out
    assert out.strip().endswith("OK")


def test_run_smoke_test_show_months(capsys) -> None:
    run_smoke_test(
        "kw",
        settings=dummy_google_ads_settings(),
        provider=FakeGoogleAdsProvider(metrics=[_metrics()]),
        show_months=1,
    )
    out = capsys.readouterr().out
    assert "- 2025-02: 1400" in out
    assert "- 2025-01: 1200" not in out  # 直近 1 か月のみ


def test_run_smoke_test_not_configured(capsys) -> None:
    code = run_smoke_test("kw", settings=unconfigured_settings())

    assert code == EXIT_NOT_CONFIGURED
    err = capsys.readouterr().err
    assert "not configured" in err.lower()


def test_run_smoke_test_provider_not_configured_at_request_time(capsys) -> None:
    provider = FakeGoogleAdsProvider(error=ProviderNotConfiguredError("google_ads"))

    code = run_smoke_test(
        "kw", settings=dummy_google_ads_settings(), provider=provider
    )

    assert code == EXIT_NOT_CONFIGURED


def test_run_smoke_test_provider_error(capsys) -> None:
    provider = FakeGoogleAdsProvider(
        error=ExternalProviderError("google_ads", "Google Ads API request failed")
    )

    code = run_smoke_test(
        "kw", settings=dummy_google_ads_settings(), provider=provider
    )

    assert code == EXIT_PROVIDER_ERROR
    err = capsys.readouterr().err
    assert "Google Ads request failed" in err


def test_run_smoke_test_no_metrics(capsys) -> None:
    code = run_smoke_test(
        "kw",
        settings=dummy_google_ads_settings(),
        provider=FakeGoogleAdsProvider(metrics=[]),
    )

    assert code == EXIT_NO_DATA
    err = capsys.readouterr().err
    assert "no historical metrics" in err.lower()


def test_run_smoke_test_metrics_without_avg(capsys) -> None:
    empty = GoogleAdsKeywordMetrics(keyword="kw", avg_monthly_searches=None)
    code = run_smoke_test(
        "kw",
        settings=dummy_google_ads_settings(),
        provider=FakeGoogleAdsProvider(metrics=[empty]),
    )

    assert code == EXIT_NO_DATA


def test_unexpected_exception_prints_only_type_name(capsys) -> None:
    class _Boom:
        def fetch_historical_metrics(self, keywords: list[str]) -> list:
            raise RuntimeError("developer_token=SUPER_SECRET refresh_token=RT")

    code = run_smoke_test(
        "kw", settings=dummy_google_ads_settings(), provider=_Boom()
    )

    assert code != EXIT_OK
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "SUPER_SECRET" not in combined
    assert "RuntimeError" in combined


@pytest.mark.parametrize(
    "provider",
    [
        FakeGoogleAdsProvider(metrics=[_metrics()]),
        FakeGoogleAdsProvider(
            error=ExternalProviderError("google_ads", "Google Ads API request failed")
        ),
        FakeGoogleAdsProvider(metrics=[]),
    ],
)
def test_no_credentials_are_ever_printed(capsys, provider: FakeGoogleAdsProvider) -> None:
    run_smoke_test(
        "AI 議事録 おすすめ",
        settings=dummy_google_ads_settings(),
        provider=provider,
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for secret in _SECRETS:
        assert secret not in combined
    # customer_id もスモークテスト結果には出さない
    assert "1234567890" not in combined
