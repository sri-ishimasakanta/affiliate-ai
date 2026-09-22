"""app.operations.source_health の単体テスト (C8.5)。

C8.4 の誤検知を二度と起こさないための回帰テスト。

誤検知の内容: Search Console は 2026-09-22 まで正常に問い合わせできていたのに、
2026-09-15 以降に表示回数が無かったために「データが古い」と警告した。低トラフィック
のサイトでは活動が止まるのが正常であり、それは故障ではない。

したがってここで pin するのは 1 点に尽きる:

    **鮮度は「取り込みが動いているか」で判定し、「活動があるか」では判定しない。**
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.operations.policy import OperationsPolicy, load_policy
from app.operations.source_health import (
    NO_RECENT_ACTIVITY,
    SOURCE_REFRESH_STALE,
    SourceFreshness,
    evaluate_recent_activity,
    evaluate_source_refresh,
)

_POLICY = load_policy()
_TODAY = date(2026, 9, 23)
_NOW = datetime(2026, 9, 23, 6, 30, tzinfo=UTC)


def _policy(**over) -> OperationsPolicy:
    raw = dict(_POLICY.raw)
    for section, values in over.items():
        raw[section] = {**raw.get(section, {}), **values}
    return OperationsPolicy(policy_version="test", raw=raw)


def _freshness(**over) -> SourceFreshness:
    kwargs = dict(
        source="search_console",
        coverage_through=date(2026, 9, 22),
        latest_observed_data_date=date(2026, 9, 15),
        last_successful_import_at=_NOW,
        ever_had_data=True,
        ever_imported=True,
    )
    kwargs.update(over)
    return SourceFreshness(**kwargs)


def _refresh(**over):
    return evaluate_source_refresh(
        freshness=_freshness(**over), today=_TODAY, now=_NOW, policy=_POLICY
    )


# ==================== the C8.4 false positive =================================
def test_recent_coverage_with_old_activity_is_not_stale() -> None:
    """これが本丸: 2026-09-22 まで取り込めていて活動が 2026-09-15 でも正常。"""

    assert _refresh() is None


def test_search_console_with_no_activity_at_all_is_not_stale() -> None:
    assert _refresh(latest_observed_data_date=None, ever_had_data=False) is None


def test_activity_is_explicitly_not_part_of_the_judgement() -> None:
    """活動が何年前でも、取り込みが動いていれば故障ではない。"""

    assert _refresh(latest_observed_data_date=date(2020, 1, 1)) is None


# ==================== genuine staleness =======================================
def test_coverage_that_stopped_advancing_is_stale() -> None:
    draft = _refresh(coverage_through=date(2026, 9, 1))
    assert draft is not None
    assert draft.alert_type == SOURCE_REFRESH_STALE
    assert draft.evidence["activity_considered"] is False
    assert any("coverage only reaches" in reason for reason in draft.evidence["reasons"])


def test_missing_recent_import_is_stale() -> None:
    draft = _refresh(last_successful_import_at=datetime(2026, 9, 1, tzinfo=UTC))
    assert draft is not None
    assert any("last successful import" in reason for reason in draft.evidence["reasons"])


def test_a_source_never_imported_is_not_stale() -> None:
    """初期状態は故障ではない。"""

    assert (
        _refresh(
            ever_imported=False,
            coverage_through=None,
            last_successful_import_at=None,
            ever_had_data=False,
        )
        is None
    )


def test_coverage_within_the_provider_lag_is_not_stale() -> None:
    # gate は coverage_stale_after_days(3) + expected_lag_days(3) = 6 日。
    assert _refresh(coverage_through=date(2026, 9, 17)) is None
    assert _refresh(coverage_through=date(2026, 9, 16)) is not None


# ==================== ga4 =====================================================
def _ga4(**over):
    kwargs = dict(
        source="ga4",
        coverage_through=date(2026, 9, 22),
        latest_observed_data_date=None,
        last_successful_import_at=_NOW,
        ever_had_data=False,
        ever_imported=True,
    )
    kwargs.update(over)
    return evaluate_source_refresh(
        freshness=SourceFreshness(**kwargs), today=_TODAY, now=_NOW, policy=_POLICY
    )


def test_ga4_zero_row_success_is_healthy() -> None:
    """計測開始直後で 1 行も無いが、問い合わせは成功している = 正常。"""

    assert _ga4() is None


def test_ga4_coverage_that_stops_advancing_is_stale() -> None:
    draft = _ga4(coverage_through=date(2026, 9, 1))
    assert draft is not None
    assert draft.source == "ga4"


def test_ga4_that_had_data_then_goes_quiet_is_still_not_stale() -> None:
    """行が届いた実績があっても、活動が止まっただけなら故障ではない。"""

    assert _ga4(ever_had_data=True, latest_observed_data_date=date(2026, 9, 10)) is None


# ==================== affiliate clicks / commissions ==========================
def test_affiliate_clicks_with_no_new_clicks_are_not_stale() -> None:
    draft = evaluate_source_refresh(
        freshness=SourceFreshness(
            source="affiliate_clicks",
            coverage_through=None,
            latest_observed_data_date=date(2026, 9, 22),
            last_successful_import_at=_NOW,
            ever_had_data=True,
            ever_imported=True,
        ),
        today=_TODAY,
        now=_NOW,
        policy=_POLICY,
    )
    assert draft is None


def test_affiliate_clicks_without_a_recent_import_are_stale() -> None:
    draft = evaluate_source_refresh(
        freshness=SourceFreshness(
            source="affiliate_clicks",
            coverage_through=None,
            latest_observed_data_date=date(2026, 9, 22),
            last_successful_import_at=datetime(2026, 9, 10, tzinfo=UTC),
            ever_had_data=True,
            ever_imported=True,
        ),
        today=_TODAY,
        now=_NOW,
        policy=_POLICY,
    )
    assert draft is not None


def test_commissions_with_zero_rows_are_not_stale() -> None:
    draft = evaluate_source_refresh(
        freshness=SourceFreshness(
            source="make_commissions",
            coverage_through=date(2026, 9, 23),
            latest_observed_data_date=None,
            last_successful_import_at=_NOW,
            ever_had_data=False,
            ever_imported=True,
        ),
        today=_TODAY,
        now=_NOW,
        policy=_POLICY,
    )
    assert draft is None


# ==================== activity signal (informational, off by default) =========
def test_activity_signal_is_disabled_by_default() -> None:
    assert evaluate_recent_activity(freshness=_freshness(), today=_TODAY, policy=_POLICY) is None


def test_activity_signal_when_enabled_is_informational_only() -> None:
    policy = _policy(alerts={"activity_alerts_enabled": True})
    draft = evaluate_recent_activity(
        freshness=_freshness(latest_observed_data_date=date(2026, 7, 1)),
        today=_TODAY,
        policy=policy,
    )
    assert draft is not None
    assert draft.alert_type == NO_RECENT_ACTIVITY
    # info は通知の既定閾値 (warning) を下回るので、記録されても通知されない。
    assert draft.severity == "info"
    assert "not a source failure" in draft.summary


def test_activity_signal_stays_quiet_inside_its_gate() -> None:
    policy = _policy(alerts={"activity_alerts_enabled": True})
    assert evaluate_recent_activity(freshness=_freshness(), today=_TODAY, policy=policy) is None


def test_activity_signal_needs_prior_data() -> None:
    policy = _policy(alerts={"activity_alerts_enabled": True})
    assert (
        evaluate_recent_activity(
            freshness=_freshness(ever_had_data=False, latest_observed_data_date=None),
            today=_TODAY,
            policy=policy,
        )
        is None
    )


# ==================== separation of concepts ==================================
def test_coverage_and_activity_are_reported_separately() -> None:
    payload = _freshness().as_dict()
    assert payload["coverage_through"] == "2026-09-22"
    assert payload["latest_observed_data_date"] == "2026-09-15"
    assert payload["coverage_through"] != payload["latest_observed_data_date"]
