"""C10-A の分析の土台の規則 (pure)。

pin する契約:

- 出所の状態: fresh / waiting / stale / missing / insufficient / provider_error。使えるのは fresh と
  waiting だけ。古い・無い・失敗した保存済みの出所は「外の取り込みが要る」(分析は呼ばない)。
  遅れの数字は 1 か所 (運用の方針 + source_policy.json) にだけ置く。
- 索引の状態: 生の値と正規化した値を分ける。調べていない・結果が無いは ``unknown`` のまま
  (「索引されていない」ではない)。調べて失敗したら ``error``。止められていれば ``blocked``。
- 帰属: 鍵が無ければ記事に配らない。クリックは記事まで結べる (A)、Make の成果は提供元まで (B)、
  取り込みの無い提供元は結べない (D)。SubID は使っていない (勝手に有効にしない)。
- 分析の証拠: 成分は別々。合成の点数は無い。外の取り直しは提供元ごとにまとめる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analysis import evidence_contract as ec
from app.analysis import sources as src
from app.revenue import attribution_readiness as ar
from app.seo import index_state as ix
from app.seo.indexability import (
    GSC_CRAWLED_NOT_INDEXED,
    GSC_DISCOVERED_NOT_INDEXED,
    GSC_EXCLUDED,
    GSC_INDEXED,
    GSC_NOT_KNOWN_TO_GOOGLE,
    GSC_UNKNOWN,
)

_NOW = datetime(2026, 10, 1, tzinfo=UTC)


# == 出所 ==========================================================================================
def test_age_based_sources_are_fresh_stale_or_missing() -> None:
    assert src.age_state(None, now=_NOW) == src.MISSING
    assert src.age_state(_NOW - timedelta(days=10), now=_NOW, stale_after_days=45) == src.FRESH
    assert src.age_state(_NOW - timedelta(days=46), now=_NOW, stale_after_days=45) == src.STALE
    assert src.age_state(_NOW - timedelta(hours=49), now=_NOW, stale_after_hours=48) == src.STALE


def test_only_fresh_and_waiting_are_usable_and_missing_needs_an_external_refresh() -> None:
    for state in src.FRESHNESS_STATES:
        status = src.SourceStatus("x", "p", state)
        assert status.usable is (state in (src.FRESH, src.WAITING))
        assert status.needs_external_refresh is (state in (src.STALE, src.MISSING,
                                                           src.PROVIDER_ERROR))
    local = src.SourceStatus("x", "p", src.MISSING, access=src.ACCESS_LOCAL)
    assert local.needs_external_refresh is False  # 手元で導くものは外に取りに行かない


def test_the_c9_vocabulary_mapping_is_kept() -> None:
    assert src.legacy_from(src.MISSING) == "unavailable"
    assert src.legacy_from(src.STALE) == src.legacy_from(src.PROVIDER_ERROR) == "stale"
    assert src.legacy_from(src.FRESH) == src.legacy_from(src.INSUFFICIENT) == "fresh"


def test_lag_numbers_live_in_one_place() -> None:
    from app.growth.measurement import expected_lag_days
    from app.operations.policy import get_policy

    ops = get_policy()
    for name in ("search_console", "ga4"):
        assert src.expected_lag_days(name) == ops.import_config(name)["expected_lag_days"]
        assert "expected_lag_days" not in src.source_definition(name)  # 繰り返さない
        assert expected_lag_days(name) == src.expected_lag_days(name)  # C9-C も同じ値を読む
    assert src.expected_lag_days("affiliate_clicks") == 1
    policy = src.load_source_policy()
    assert set(policy["sources"]) == set(src.SOURCES)
    assert "calls" in policy["sources"]["google_ads"]["refresh"]  # 分析は Google Ads を呼ばない


# == 索引の状態 ====================================================================================
def test_raw_and_normalized_index_states_are_separate() -> None:
    mapping = {GSC_INDEXED: ix.INDEXED, GSC_CRAWLED_NOT_INDEXED: ix.CRAWLED_NOT_INDEXED,
               GSC_DISCOVERED_NOT_INDEXED: ix.DISCOVERED_NOT_INDEXED,
               GSC_NOT_KNOWN_TO_GOOGLE: ix.NOT_INDEXED, GSC_EXCLUDED: ix.EXCLUDED,
               GSC_UNKNOWN: ix.UNKNOWN}
    for raw, normalized in mapping.items():
        assert ix.normalize_index_state(raw, {}) == normalized
    assert set(ix.NORMALIZED_STATES) >= set(mapping.values())


def test_unknown_stays_unknown_and_is_not_not_indexed() -> None:
    assert ix.normalize_index_state(GSC_UNKNOWN) == ix.UNKNOWN
    assert ix.normalize_index_state(None) == ix.UNKNOWN
    assert ix.normalize_index_state(GSC_INDEXED, inspected=False) == ix.UNKNOWN
    assert ix.UNKNOWN not in ix.KNOWN_STATES and ix.ERROR not in ix.KNOWN_STATES
    assert ix.normalize_index_state(GSC_UNKNOWN, provider_error=True) == ix.ERROR


def test_blocked_comes_only_from_google_raw_values() -> None:
    assert ix.normalize_index_state(GSC_EXCLUDED, {"robots_txt_state": "DISALLOWED"}) == (
        ix.BLOCKED)
    assert ix.normalize_index_state(GSC_EXCLUDED, {"indexing_state": "BLOCKED_BY_META_TAG"}) == (
        ix.BLOCKED)
    assert ix.normalize_index_state(GSC_EXCLUDED, {"robots_txt_state": "ALLOWED"}) == ix.EXCLUDED


def test_an_observation_keeps_raw_provider_values_without_urls() -> None:
    row = {"article_id": 3, "url": "https://bizfluxlab.com/a/", "google_index_state": GSC_INDEXED,
           "verdict": "PASS", "coverage_state": "Submitted and indexed",
           "last_crawl_time": "2026-09-20T01:00:00Z", "live_state": "LIVE_HEALTHY"}
    o = ix.observation_from_row(row, observed_at="2026-09-26T00:00:00+00:00",
                                data_source="operations_step_runs:1 (inspect)", inspected=True,
                                freshness_state="fresh")
    assert o.normalized_status == ix.INDEXED and o.known
    assert o.raw_status == {"verdict": "PASS", "coverage_state": "Submitted and indexed",
                            "last_crawl_time": "2026-09-20T01:00:00Z",
                            "google_index_state": GSC_INDEXED}
    assert o.last_crawl == "2026-09-20T01:00:00Z" and o.provider == ix.PROVIDER_URL_INSPECTION
    daily = ix.observation_from_row({**row, "google_index_state": GSC_UNKNOWN},
                                    observed_at=None, data_source="d", inspected=False,
                                    freshness_state="fresh")
    assert daily.normalized_status == ix.UNKNOWN and daily.freshness_state == "missing"
    assert "did not use URL Inspection" in daily.missing_reason


# == 帰属 ==========================================================================================
def test_clicks_are_direct_and_make_commissions_are_provider_level_only() -> None:
    make = ar.program_readiness(program_id=1, name="Make", provider="make", has_tracking_url=True,
                                active_targets=3, targets_with_article=3, commission_rows=0)
    assert make.click_attribution_level == ar.LEVEL_DIRECT
    assert make.commission_attribution_level == ar.LEVEL_PROVIDER
    assert make.deterministic_join_possible is False and make.configuration_change_required
    assert make.subid_currently_used is False and make.historical_backfill_possible is False
    assert make.provider_transaction_id_available is True
    assert make.subid_supported == ar.UNKNOWN  # コードで確かめられないことは言わない


def test_a_provider_without_an_importer_stays_unattributed() -> None:
    other = ar.program_readiness(program_id=5, name="X", provider="Impact",
                                 has_tracking_url=False, active_targets=0,
                                 targets_with_article=0, commission_rows=0)
    assert other.click_attribution_level == ar.LEVEL_UNATTRIBUTED
    assert other.commission_attribution_level == ar.LEVEL_UNATTRIBUTED
    assert "no commission importer for this provider" in other.reasons


def test_a_commission_reaches_articles_only_with_a_deterministic_key() -> None:
    linked = ar.program_readiness(program_id=1, name="Make", provider="make",
                                  has_tracking_url=True, active_targets=1, targets_with_article=1,
                                  commission_rows=4, commissions_with_click_reference=2)
    assert linked.deterministic_join_possible and linked.commission_attribution_level == (
        ar.LEVEL_DIRECT)
    no_article = ar.program_readiness(program_id=1, name="Make", provider="make",
                                      has_tracking_url=True, active_targets=0,
                                      targets_with_article=0, commission_rows=4,
                                      commissions_with_click_reference=2)
    assert no_article.deterministic_join_possible is False
    assert {row["level"] for row in ar.TRUTH_TABLE} >= {ar.LEVEL_DIRECT, ar.LEVEL_PROVIDER,
                                                         ar.LEVEL_UNATTRIBUTED}


# == 分析の証拠 ====================================================================================
def test_components_stay_separate_and_refresh_is_batched_per_provider() -> None:
    def cand(i, state):
        return ec.CandidateEvidence("keyword", f"keyword:{i}", {
            "commercial_intent": ec.ComponentEvidence("commercial_intent", state, "google_ads",
                                                      ec.ACCESS_CACHED),
            "site_relevance": ec.ComponentEvidence("site_relevance", ec.MISSING,
                                                   "site_profile", ec.ACCESS_LOCAL)})

    candidates = [cand(i, ec.MISSING if i % 2 else ec.USABLE) for i in range(80)]
    plan = ec.refresh_plan(candidates)
    assert list(plan) == ["google_ads"]  # 手元で導く成分は外に取りに行かない
    assert len(plan["google_ads"]["subjects"]) == 40 and plan["google_ads"]["calls_if_run"] == 1
    d = candidates[1].as_dict()
    assert d["composite_score"] is None and d["needs_external_refresh"] == ["commercial_intent"]
    assert ec.component_summary(candidates)["commercial_intent"] == {"missing": 40, "usable": 40}
