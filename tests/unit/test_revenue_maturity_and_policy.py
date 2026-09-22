"""app.revenue.policy / app.revenue.maturity の単体テスト (C7)。

pin する要点:

- 閾値はコードではなくバージョン管理された JSON にある。
- 信頼できる計測開始時刻が設定され、その理由が記録されている。
- **データが無いことを「成果が出ていない」と言わない**。
- 収益化を意図していない記事に、収益化の不足を問わない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.revenue.maturity import (
    ATTRIBUTION_PROGRAM_ONLY,
    ATTRIBUTION_UNAVAILABLE,
    INSUFFICIENT_CLICK_SAMPLE,
    INSUFFICIENT_TRAFFIC_SAMPLE,
    MEASURABLE,
    MONETIZATION_SETUP_INCOMPLETE,
    MONETIZED_AWAITING_TRAFFIC,
    NOT_MONETIZABLE,
    assess_revenue_maturity,
)
from app.revenue.policy import PRIORITIES, load_policy

_POLICY = load_policy()
_TODAY = date(2026, 12, 1)


def _assess(**over):
    kwargs = dict(
        article_id=10,
        monetization_mode="affiliate",
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        today=_TODAY,
        active_target_count=1,
        active_mapping_count=1,
        traffic_data_available=True,
        sessions=1000,
        organic_sessions=500,
        clean_clicks=50,
        excluded_clicks=0,
        raw_clicks=50,
        commission_attribution=ATTRIBUTION_PROGRAM_ONLY,
        policy=_POLICY,
    )
    kwargs.update(over)
    return assess_revenue_maturity(**kwargs)


# ==================== policy ==================================================
def test_policy_declares_a_version_and_a_trusted_cutoff() -> None:
    assert _POLICY.policy_version
    assert _POLICY.trusted_measurement_start_at is not None
    assert _POLICY.trusted_measurement_start_at.tzinfo is not None


def test_cutoff_rationale_is_documented() -> None:
    rationale = _POLICY.trusted_measurement_rationale
    assert rationale
    assert any("instrumentation" in line for line in rationale)
    # 履歴を消さないことを明示している。
    assert any("NEVER deleted" in line or "retained" in line.lower() for line in rationale)


def test_thresholds_live_in_config_not_code() -> None:
    assert _POLICY.gate("traffic", "minimum_organic_sessions_for_click_review") is not None
    assert _POLICY.gate("clicks", "minimum_clean_clicks_for_pattern_review") is not None
    assert _POLICY.gate("traffic", "minimum_sessions_for_zero_click_review") is not None


def test_every_candidate_type_has_an_explicit_priority() -> None:
    from app.revenue.candidates import CANDIDATE_TYPES

    for candidate_type in CANDIDATE_TYPES:
        assert _POLICY.base_priority(candidate_type) in PRIORITIES


# ==================== maturity ================================================
def test_supporting_article_is_not_monetizable() -> None:
    result = _assess(monetization_mode="supporting", active_target_count=0, active_mapping_count=0)
    assert result.state == NOT_MONETIZABLE
    assert result.behavioral_evaluation_allowed is False


def test_affiliate_article_without_a_target_is_setup_incomplete() -> None:
    result = _assess(active_target_count=0, active_mapping_count=0)
    assert result.state == MONETIZATION_SETUP_INCOMPLETE


def test_target_without_mapping_is_setup_incomplete() -> None:
    assert _assess(active_mapping_count=0).state == MONETIZATION_SETUP_INCOMPLETE


def test_freshly_published_monetized_article_awaits_traffic() -> None:
    result = _assess(published_at=datetime(2026, 11, 30, tzinfo=UTC))
    assert result.state == MONETIZED_AWAITING_TRAFFIC
    assert result.behavioral_evaluation_allowed is False


def test_missing_ga4_rows_are_awaiting_traffic_not_zero_performance() -> None:
    result = _assess(traffic_data_available=False, sessions=None, organic_sessions=None)
    assert result.state == MONETIZED_AWAITING_TRAFFIC
    assert "ga4 has no daily rows" in result.reason
    assert result.behavioral_evaluation_allowed is False


def test_small_traffic_sample_is_insufficient_not_poor() -> None:
    result = _assess(organic_sessions=5)
    assert result.state == INSUFFICIENT_TRAFFIC_SAMPLE
    assert result.behavioral_evaluation_allowed is False


def test_small_click_sample_is_insufficient() -> None:
    result = _assess(clean_clicks=1)
    assert result.state == INSUFFICIENT_CLICK_SAMPLE


def test_sufficient_traffic_and_clicks_is_measurable() -> None:
    result = _assess()
    assert result.state == MEASURABLE
    assert result.behavioral_evaluation_allowed is True


def test_excluded_clicks_do_not_count_towards_the_sample() -> None:
    """自分たちの計測クリックで成熟したことにしない。"""

    result = _assess(clean_clicks=0, excluded_clicks=8, raw_clicks=8)
    assert result.state == INSUFFICIENT_CLICK_SAMPLE
    assert "8 click(s) excluded as instrumentation" in result.reason


# ==================== attribution =============================================
def test_program_level_attribution_is_explained() -> None:
    result = _assess(commission_attribution=ATTRIBUTION_PROGRAM_ONLY)
    assert result.attribution == ATTRIBUTION_PROGRAM_ONLY
    assert "no click/order/subid" in result.attribution_reason


def test_unavailable_attribution_is_explained() -> None:
    result = _assess(commission_attribution=ATTRIBUTION_UNAVAILABLE)
    assert result.attribution == ATTRIBUTION_UNAVAILABLE
    assert "no commission data" in result.attribution_reason


def test_assessment_is_deterministic() -> None:
    assert _assess() == _assess()
