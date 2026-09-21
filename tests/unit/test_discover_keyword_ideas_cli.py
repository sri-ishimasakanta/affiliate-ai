"""scripts/discover_keyword_ideas.py + app/keyword/idea_seeds.py。

PLAN は Google / DB へ一切触れない。--execute も fake client のみ (実通信 0)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.discover_keyword_ideas as m
from app.keyword.idea_seeds import (
    MAX_RESULTS_LIMIT,
    MAX_SEEDS_PER_SET,
    TARGET_CLUSTERS,
    IdeaSeedConfigError,
    load_idea_seed_config,
    parse_idea_seed_config,
)
from app.keyword.providers.google_ads_ideas import (
    MSG_AUTH,
    MSG_QUOTA,
    GoogleAdsKeywordIdeaProvider,
)
from scripts.discover_keyword_ideas import (
    DEFAULT_SEEDS,
    EXIT_API,
    EXIT_AUTH,
    EXIT_CONFIG,
    EXIT_NOT_CONFIGURED,
    EXIT_OK,
    EXIT_OUTPUT,
    run,
)
from tests.support.google_ads_fakes import dummy_google_ads_settings, unconfigured_settings
from tests.support.google_ads_idea_fakes import (
    FakeGoogleAdsException,
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


def _pager() -> FakeIdeasPager:
    return FakeIdeasPager(
        [
            idea_row_with_metrics("crm おすすめ", avg_monthly_searches=880, competition="HIGH"),
            idea_row("crm 比較"),
        ]
    )


def _factory_for(client: FakeIdeasClient, seen: list | None = None):
    def factory(settings, max_requests):
        if seen is not None:
            seen.append(max_requests)
        return GoogleAdsKeywordIdeaProvider(settings, client=client, max_requests=max_requests)

    return factory


def _forbidden_factory(_settings, _max_requests):
    raise AssertionError("no provider may be created")


def _cfg(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "seeds.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


# ============================================================ seed config
def test_tracked_seed_config_targets_all_five_clusters_within_limits() -> None:
    config = load_idea_seed_config(DEFAULT_SEEDS)
    assert tuple(s.cluster_id for s in config.sets) == TARGET_CLUSTERS == ("B", "C", "A", "D", "E")
    assert 1 <= config.default_max_results <= MAX_RESULTS_LIMIT
    for seed_set in config.sets:
        assert 1 <= len(seed_set.seeds) <= MAX_SEEDS_PER_SET


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ([], "must be a JSON object"),
        (
            {
                "version": 2,
                "default_max_results": 10,
                "clusters": {"B": {"name": "n", "seeds": ["a"]}},
            },
            "version must be 1",
        ),
        (
            {
                "version": 1,
                "default_max_results": 0,
                "clusters": {"B": {"name": "n", "seeds": ["a"]}},
            },
            "default_max_results",
        ),
        ({"version": 1, "default_max_results": 10, "clusters": {}}, "non-empty object"),
        (
            {
                "version": 1,
                "default_max_results": 10,
                "clusters": {"Z": {"name": "n", "seeds": ["a"]}},
            },
            "unknown cluster id",
        ),
        (
            {
                "version": 1,
                "default_max_results": 10,
                "clusters": {"B": {"name": "n", "seeds": []}},
            },
            "non-empty list",
        ),
        (
            {
                "version": 1,
                "default_max_results": 10,
                "clusters": {"B": {"name": "n", "seeds": ["a", "A"]}},
            },
            "duplicate seeds",
        ),
        (
            {
                "version": 1,
                "default_max_results": 10,
                "clusters": {
                    "B": {"name": "n", "seeds": [str(i) for i in range(MAX_SEEDS_PER_SET + 1)]}
                },
            },
            "at most",
        ),
        (
            {
                "version": 1,
                "default_max_results": 10,
                "clusters": {"B": {"name": "n", "seeds": ["a"], "extra": 1}},
            },
            "unknown keys",
        ),
        ({"version": 1, "default_max_results": 10, "clusters": {"B": {"seeds": ["a"]}}}, "name"),
    ],
)
def test_seed_config_validation(raw, fragment: str) -> None:
    with pytest.raises(IdeaSeedConfigError) as exc:
        parse_idea_seed_config(raw)
    assert fragment in str(exc.value)


def test_seed_config_select_and_unknown_cluster() -> None:
    config = load_idea_seed_config(DEFAULT_SEEDS)
    assert [s.cluster_id for s in config.select(["e", "b"])] == ["B", "E"]  # 定義順
    assert config.select(None) == config.sets
    with pytest.raises(IdeaSeedConfigError, match="unknown cluster"):
        config.select(["Z"])


def test_seed_config_load_errors(tmp_path: Path) -> None:
    with pytest.raises(IdeaSeedConfigError, match="not found"):
        load_idea_seed_config(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    with pytest.raises(IdeaSeedConfigError, match="not readable JSON"):
        load_idea_seed_config(bad)


# ================================================================== PLAN
def test_plan_is_the_default_and_makes_no_google_request_and_opens_no_db(capsys) -> None:
    code = run(settings=dummy_google_ads_settings(), provider_factory=_forbidden_factory)
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "(PLAN)" in out and "planned_requests       = 5" in out
    assert "no Google request made, no DB access" in out
    for cluster in TARGET_CLUSTERS:
        assert f"[{cluster}]" in out
    assert not hasattr(m, "SessionLocal")  # この CLI は DB を扱わない
    for secret in _SECRETS:
        assert secret not in out


def test_plan_reports_project_language_and_location_and_never_credentials(capsys) -> None:
    settings = dummy_google_ads_settings(google_ads_geo_target_id=2392, google_ads_language_id=1005)
    run(settings=settings, provider_factory=_forbidden_factory)
    out = capsys.readouterr().out
    assert "geo_target_id          = 2392" in out and "language_id            = 1005" in out
    assert "google_ads_configured  = True" in out
    for secret in _SECRETS:
        assert secret not in out


def test_plan_for_unconfigured_google_ads_still_succeeds_without_requests(capsys) -> None:
    code = run(settings=unconfigured_settings(), provider_factory=_forbidden_factory)
    assert code == EXIT_OK
    assert "google_ads_configured  = False" in capsys.readouterr().out


def test_plan_with_unknown_cluster_or_bad_max_results_fails_before_settings(capsys) -> None:
    assert run(clusters=["Z"], provider_factory=_forbidden_factory) == EXIT_CONFIG
    assert "unknown cluster" in capsys.readouterr().out
    assert run(max_results=0, settings=dummy_google_ads_settings()) == EXIT_CONFIG
    assert (
        run(max_results=MAX_RESULTS_LIMIT + 1, settings=dummy_google_ads_settings()) == EXIT_CONFIG
    )


# ================================================================ EXECUTE
def test_execute_sends_at_most_one_request_per_cluster_seed_set(capsys) -> None:
    client = FakeIdeasClient(response=_pager())
    budgets: list[int] = []
    code = run(
        execute=True,
        settings=dummy_google_ads_settings(),
        provider_factory=_factory_for(client, budgets),
    )
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert client.calls == 5 and budgets == [5]  # 5 cluster = 5 request、budget も 5
    config = load_idea_seed_config(DEFAULT_SEEDS)
    assert [tuple(r.keyword_seed.keywords) for r in client.requests] == [
        s.seeds for s in config.sets
    ]
    assert all(r.page_size == config.default_max_results for r in client.requests)
    assert "keyword_idea_requests_made = 5" in out
    for secret in _SECRETS:
        assert secret not in out


def test_execute_respects_cluster_selection_and_max_results() -> None:
    client = FakeIdeasClient(response=_pager())
    code = run(
        execute=True,
        clusters=["c", "B"],
        max_results=25,
        settings=dummy_google_ads_settings(),
        provider_factory=_factory_for(client),
    )
    assert code == EXIT_OK
    assert client.calls == 2
    assert all(r.page_size == 25 for r in client.requests)
    assert client.requests[0].keyword_seed.keywords[0] == "業務効率化 ツール"  # B が先


def test_execute_never_follows_extra_result_pages() -> None:
    pager = FakeIdeasPager([idea_row("a")], extra_pages=[[idea_row("b")]])
    client = FakeIdeasClient(response=pager)
    run(
        execute=True,
        clusters=["B"],
        settings=dummy_google_ads_settings(),
        provider_factory=_factory_for(client),
    )
    assert client.calls == 1 and pager.extra_pages_fetched == 0


def test_execute_is_blocked_when_google_ads_is_not_configured(capsys) -> None:
    code = run(execute=True, settings=unconfigured_settings(), provider_factory=_forbidden_factory)
    assert code == EXIT_NOT_CONFIGURED
    assert "NOT CONFIGURED" in capsys.readouterr().out


def test_auth_failure_stops_immediately_with_a_safe_message(capsys) -> None:
    client = FakeIdeasClient(error=RefreshError("invalid_grant dummy-refresh-token 1234567890"))
    code = run(
        execute=True, settings=dummy_google_ads_settings(), provider_factory=_factory_for(client)
    )
    out = capsys.readouterr().out
    assert code == EXIT_AUTH
    assert client.calls == 1  # 残り 4 cluster の request は送らない
    assert MSG_AUTH in out and "AUTH ERROR" in out
    for secret in _SECRETS:
        assert secret not in out


def test_quota_failure_is_an_api_error_and_stops(capsys) -> None:
    client = FakeIdeasClient(error=FakeGoogleAdsException("quota_error", "1234567890"))
    code = run(
        execute=True, settings=dummy_google_ads_settings(), provider_factory=_factory_for(client)
    )
    out = capsys.readouterr().out
    assert code == EXIT_API and client.calls == 1
    assert MSG_QUOTA in out and "1234567890" not in out


def test_failure_after_the_first_cluster_stops_the_remaining_requests(capsys, tmp_path) -> None:
    class _FailsOnSecond(FakeIdeasClient):
        def generate_keyword_ideas(self, *, request):
            if self.calls == 1:
                self.calls += 1
                raise FakeGoogleAdsException("request_error")
            return super().generate_keyword_ideas(request=request)

    client = _FailsOnSecond(response=_pager())
    out_file = tmp_path / "ideas.json"
    code = run(
        execute=True,
        output=out_file,
        settings=dummy_google_ads_settings(),
        provider_factory=_factory_for(client),
    )
    assert code == EXIT_API
    assert client.calls == 2  # 2 回目で失敗 -> 3 回目以降は送らない
    assert not out_file.exists()  # 失敗時は部分的な出力ファイルも作らない
    assert "API ERROR" in capsys.readouterr().out


# ================================================================ output file
def test_output_file_contains_normalized_ideas_with_metrics_or_null(tmp_path: Path, capsys) -> None:
    client = FakeIdeasClient(response=_pager())
    out_file = tmp_path / "ideas.json"
    code = run(
        execute=True,
        clusters=["E"],
        output=out_file,
        settings=dummy_google_ads_settings(
            google_ads_geo_target_id=2392, google_ads_language_id=1005
        ),
        provider_factory=_factory_for(client),
    )
    assert code == EXIT_OK
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["geo_target_id"] == 2392 and payload["language_id"] == 1005
    by_kw = {i["keyword"]: i for i in payload["ideas"]}
    assert by_kw["crm おすすめ"]["cluster"] == "E"
    assert by_kw["crm おすすめ"]["metrics"]["avg_monthly_searches"] == 880
    assert by_kw["crm おすすめ"]["metrics"]["competition"] == "HIGH"
    assert by_kw["crm 比較"]["metrics"] is None
    text = out_file.read_text(encoding="utf-8")
    for secret in _SECRETS:
        assert secret not in text
    out = capsys.readouterr().out
    assert "not_available" in out  # 指標が無い idea は not_available と表示


def test_output_never_overwrites_and_fails_before_any_request(tmp_path: Path, capsys) -> None:
    existing = tmp_path / "ideas.json"
    existing.write_text("keep me", encoding="utf-8")
    client = FakeIdeasClient(response=_pager())
    code = run(
        execute=True,
        output=existing,
        settings=dummy_google_ads_settings(),
        provider_factory=_factory_for(client),
    )
    assert code == EXIT_OUTPUT and client.calls == 0
    assert existing.read_text(encoding="utf-8") == "keep me"

    missing_dir = tmp_path / "no-such-dir" / "ideas.json"
    code = run(
        execute=True,
        output=missing_dir,
        settings=dummy_google_ads_settings(),
        provider_factory=_factory_for(client),
    )
    assert code == EXIT_OUTPUT and client.calls == 0


def test_plan_with_output_never_writes_the_file(tmp_path: Path) -> None:
    out_file = tmp_path / "ideas.json"
    code = run(
        output=out_file, settings=dummy_google_ads_settings(), provider_factory=_forbidden_factory
    )
    assert code == EXIT_OK and not out_file.exists()


# ================================================================== main()
def test_main_rejects_bad_flags_and_bad_clusters(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        m.main(["--max-results", "abc"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        m.main(["--not-a-flag"])
    assert m.main(["--cluster", "Z"]) == EXIT_CONFIG  # settings を読む前に検証で失敗
    assert "unknown cluster" in capsys.readouterr().out
