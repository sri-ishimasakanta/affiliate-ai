"""app/keyword/providers/google_ads_ideas.py — GenerateKeywordIdeas の wrapper。

fake client のみ。実 Google Ads へは一切通信しない (credential も dummy)。
"""

from __future__ import annotations

import pytest

import app.keyword.providers.google_ads_ideas as ideas_mod
from app.exceptions import ExternalProviderError, ProviderNotConfiguredError
from app.keyword.providers.google_ads_ideas import (
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS_LIMIT,
    MAX_SEEDS,
    MSG_API,
    MSG_AUTH,
    MSG_AUTHZ,
    MSG_BUDGET,
    MSG_CLIENT,
    MSG_QUOTA,
    MSG_REQUEST,
    GoogleAdsAuthError,
    GoogleAdsKeywordIdeaProvider,
    GoogleAdsQuotaError,
    KeywordIdeaBudgetError,
    classify_failure,
)
from tests.support.google_ads_fakes import (
    dummy_google_ads_settings,
    month_volume,
    unconfigured_settings,
)
from tests.support.google_ads_idea_fakes import (
    FakeGoogleAdsException,
    FakeGrpcError,
    FakeIdeasClient,
    FakeIdeasPager,
    RefreshError,
    idea_row,
    idea_row_with_metrics,
)

_SECRETS = (
    "dummy-developer-token",
    "dummy-client-id",
    "dummy-client-secret",
    "dummy-refresh-token",
    "1234567890",
)


def _provider(client: FakeIdeasClient, **kw) -> GoogleAdsKeywordIdeaProvider:
    return GoogleAdsKeywordIdeaProvider(dummy_google_ads_settings(), client=client, **kw)


def _response(*rows, extra_pages=None) -> FakeIdeasPager:
    return FakeIdeasPager(list(rows), extra_pages)


# ================================================================ request shape
def test_request_uses_project_language_location_seeds_and_page_size() -> None:
    client = FakeIdeasClient(response=_response())
    provider = GoogleAdsKeywordIdeaProvider(
        dummy_google_ads_settings(google_ads_geo_target_id=2392, google_ads_language_id=1005),
        client=client,
    )
    provider.generate_keyword_ideas(["業務効率化 ツール", "ClickUp"], max_results=50)

    (request,) = client.requests
    assert request.customer_id == "1234567890"
    assert request.language == "languageConstants/1005"
    assert request.geo_target_constants == ["geoTargetConstants/2392"]
    assert request.keyword_plan_network == "GOOGLE_SEARCH"
    assert request.keyword_seed.keywords == ["業務効率化 ツール", "ClickUp"]
    assert request.page_size == 50


def test_custom_location_and_language_settings_are_honoured() -> None:
    client = FakeIdeasClient(response=_response())
    settings = dummy_google_ads_settings(google_ads_geo_target_id=2840, google_ads_language_id=1000)
    GoogleAdsKeywordIdeaProvider(settings, client=client).generate_keyword_ideas(["crm"])
    assert client.requests[0].geo_target_constants == ["geoTargetConstants/2840"]
    assert client.requests[0].language == "languageConstants/1000"


def test_default_max_results_is_bounded() -> None:
    client = FakeIdeasClient(response=_response())
    _provider(client).generate_keyword_ideas(["crm"])
    assert client.requests[0].page_size == DEFAULT_MAX_RESULTS <= MAX_RESULTS_LIMIT


def test_exactly_one_request_and_no_further_pages_are_fetched() -> None:
    pager = _response(
        idea_row("crm おすすめ"), extra_pages=[[idea_row("crm 比較")], [idea_row("x")]]
    )
    client = FakeIdeasClient(response=pager)
    provider = _provider(client)

    ideas = provider.generate_keyword_ideas(["crm"])

    assert client.calls == 1 and provider.request_count == 1
    assert pager.extra_pages_fetched == 0  # pager を反復していない
    assert [i.keyword for i in ideas] == ["crm おすすめ"]


# ================================================================ response mapping
def test_ideas_are_normalized_deduplicated_and_carry_metrics() -> None:
    client = FakeIdeasClient(
        response=_response(
            idea_row_with_metrics(
                "ＣＲＭ　おすすめ",
                avg_monthly_searches=880,
                competition="HIGH",
                competition_index=77,
                low_top_of_page_bid_micros=120_000,
                high_top_of_page_bid_micros=900_000,
                monthly_search_volumes=[month_volume("MARCH", 2026, 700)],
            ),
            idea_row_with_metrics("crm おすすめ", avg_monthly_searches=1),  # 正規化後に重複
            idea_row("ChatGPT   法人  プラン"),
            idea_row(""),
        )
    )
    ideas = _provider(client).generate_keyword_ideas(["crm"])

    assert [i.keyword for i in ideas] == ["crm おすすめ", "chatgpt 法人 プラン"]
    first = ideas[0].metrics
    assert first is not None
    assert first.keyword == "crm おすすめ"
    assert first.avg_monthly_searches == 880
    assert first.competition == "HIGH" and first.competition_index == 77
    assert first.low_top_of_page_bid_micros == 120_000
    assert first.high_top_of_page_bid_micros == 900_000
    assert first.monthly_search_volumes[0].month == 3
    assert ideas[1].metrics is None  # API が指標を返さなければ None (捏造しない)


def test_result_count_is_bounded_by_max_results() -> None:
    rows = [idea_row(f"keyword {i}") for i in range(30)]
    client = FakeIdeasClient(response=_response(*rows))
    ideas = _provider(client).generate_keyword_ideas(["seed"], max_results=10)
    assert len(ideas) == 10


def test_empty_response_returns_empty_list() -> None:
    client = FakeIdeasClient(response=_response())
    assert _provider(client).generate_keyword_ideas(["seed"]) == []


# ================================================================ validation
@pytest.mark.parametrize(
    "seeds",
    [[], ["", "  "], [f"seed {i}" for i in range(MAX_SEEDS + 1)], [1]],
    ids=["empty", "blank", "too-many", "non-string"],
)
def test_invalid_seeds_are_rejected_before_any_request(seeds) -> None:
    client = FakeIdeasClient(response=_response())
    with pytest.raises(ValueError):
        _provider(client).generate_keyword_ideas(seeds)
    assert client.calls == 0


def test_duplicate_and_padded_seeds_are_cleaned() -> None:
    client = FakeIdeasClient(response=_response())
    _provider(client).generate_keyword_ideas([" CRM ", "crm", "ＣＲＭ", "SFA"])
    assert client.requests[0].keyword_seed.keywords == ["CRM", "SFA"]


@pytest.mark.parametrize("bad", [0, -1, MAX_RESULTS_LIMIT + 1, True, "10", 1.5])
def test_max_results_bounds_are_enforced(bad) -> None:
    client = FakeIdeasClient(response=_response())
    with pytest.raises(ValueError):
        _provider(client).generate_keyword_ideas(["crm"], max_results=bad)
    assert client.calls == 0


def test_not_configured_raises_without_touching_the_client() -> None:
    client = FakeIdeasClient(response=_response())
    provider = GoogleAdsKeywordIdeaProvider(unconfigured_settings(), client=client)
    with pytest.raises(ProviderNotConfiguredError):
        provider.generate_keyword_ideas(["crm"])
    assert client.calls == 0 and provider.request_count == 0


def test_request_budget_blocks_extra_requests_before_calling_google() -> None:
    client = FakeIdeasClient(response=_response())
    provider = _provider(client, max_requests=2)
    provider.generate_keyword_ideas(["a"])
    provider.generate_keyword_ideas(["b"])
    with pytest.raises(KeywordIdeaBudgetError) as exc:
        provider.generate_keyword_ideas(["c"])
    assert MSG_BUDGET in str(exc.value)
    assert client.calls == 2 and provider.request_count == 2


# ================================================================ safe failures
@pytest.mark.parametrize(
    ("error", "kind", "message", "error_type"),
    [
        (
            RefreshError("invalid_grant: Bad Request dummy-refresh-token"),
            "auth",
            MSG_AUTH,
            GoogleAdsAuthError,
        ),
        (
            FakeGoogleAdsException("authentication_error", "token dummy-client-secret"),
            "auth",
            MSG_AUTH,
            GoogleAdsAuthError,
        ),
        (FakeGoogleAdsException("oauth_error"), "auth", MSG_AUTH, GoogleAdsAuthError),
        (
            FakeGoogleAdsException("authorization_error", "customer 1234567890"),
            "authz",
            MSG_AUTHZ,
            GoogleAdsAuthError,
        ),
        (FakeGoogleAdsException("quota_error"), "quota", MSG_QUOTA, GoogleAdsQuotaError),
        (FakeGoogleAdsException("request_error"), "request", MSG_REQUEST, ExternalProviderError),
        (FakeGrpcError("UNAUTHENTICATED"), "auth", MSG_AUTH, GoogleAdsAuthError),
        (FakeGrpcError("PERMISSION_DENIED"), "authz", MSG_AUTHZ, GoogleAdsAuthError),
        (FakeGrpcError("RESOURCE_EXHAUSTED"), "quota", MSG_QUOTA, GoogleAdsQuotaError),
        (FakeGrpcError("INVALID_ARGUMENT"), "request", MSG_REQUEST, ExternalProviderError),
        (RuntimeError("boom dummy-developer-token"), "api", MSG_API, ExternalProviderError),
    ],
)
def test_api_failures_map_to_safe_fixed_messages(error, kind, message, error_type) -> None:
    assert classify_failure(error) == kind
    client = FakeIdeasClient(error=error)
    provider = _provider(client)
    with pytest.raises(error_type) as exc:
        provider.generate_keyword_ideas(["crm"])
    text = str(exc.value)
    assert message in text
    for secret in _SECRETS:
        assert secret not in text
    assert client.calls == 1 and provider.request_count == 1


def test_wrapped_refresh_error_in_the_cause_chain_is_classified_as_auth() -> None:
    try:
        try:
            raise RefreshError("invalid_grant")
        except RefreshError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as wrapped:
        assert classify_failure(wrapped) == "auth"


def test_client_initialisation_auth_failure_is_reported_as_auth_and_sends_no_request(
    monkeypatch,
) -> None:
    def _fail(_settings):
        try:
            raise RefreshError("invalid_grant: Bad Request")
        except RefreshError as inner:
            raise ExternalProviderError("google_ads", MSG_CLIENT) from inner

    monkeypatch.setattr(ideas_mod, "build_google_ads_client", _fail)
    provider = GoogleAdsKeywordIdeaProvider(dummy_google_ads_settings())
    with pytest.raises(GoogleAdsAuthError) as exc:
        provider.generate_keyword_ideas(["crm"])
    assert MSG_AUTH in str(exc.value)
    assert provider.request_count == 0  # client を作れなかったので request は 0


def test_generic_client_initialisation_failure_keeps_the_fixed_client_message(monkeypatch) -> None:
    def _fail(_settings):
        raise ExternalProviderError("google_ads", MSG_CLIENT) from RuntimeError(
            "secret dummy-client-id"
        )

    monkeypatch.setattr(ideas_mod, "build_google_ads_client", _fail)
    provider = GoogleAdsKeywordIdeaProvider(dummy_google_ads_settings())
    with pytest.raises(ExternalProviderError) as exc:
        provider.generate_keyword_ideas(["crm"])
    assert MSG_CLIENT in str(exc.value)
    assert "dummy-client-id" not in str(exc.value)
    assert provider.request_count == 0


def test_not_configured_at_client_build_time_is_not_masked(monkeypatch) -> None:
    def _fail(_settings):
        raise ProviderNotConfiguredError("google_ads")

    monkeypatch.setattr(ideas_mod, "build_google_ads_client", _fail)
    with pytest.raises(ProviderNotConfiguredError):
        GoogleAdsKeywordIdeaProvider(dummy_google_ads_settings()).generate_keyword_ideas(["crm"])


def test_shared_client_builder_is_used_by_the_metrics_provider_too(monkeypatch) -> None:
    """Metrics provider と同じ client 生成パターン (build_google_ads_client) を再利用している。"""

    import app.keyword.providers.google_ads as metrics_mod

    seen: list[object] = []

    def _fake_build(settings):
        seen.append(settings)
        return FakeIdeasClient(response=_response())

    monkeypatch.setattr(metrics_mod, "build_google_ads_client", _fake_build)
    settings = dummy_google_ads_settings()
    provider = metrics_mod.GoogleAdsKeywordMetricsProvider(settings)
    assert isinstance(provider._build_client(), FakeIdeasClient)
    assert seen == [settings]
