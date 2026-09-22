"""app.seo.maturity の単体テスト (C6)。

この module の存在理由を pin する: **ゼロは「成績が悪い」ではない**。

2026-09-22 に公開した記事の表示回数がゼロなのは、レポートが追いついていない
だけであり、性能評価の対象にしてはならない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.seo.maturity import (
    ENGAGEMENT_AWAITING_DATA,
    ENGAGEMENT_INSUFFICIENT_SAMPLE,
    ENGAGEMENT_SUFFICIENT_SAMPLE,
    SEARCH_AWAITING_INDEX_DISCOVERY,
    SEARCH_INDEXED_AWAITING_DATA,
    SEARCH_INSUFFICIENT_IMPRESSIONS,
    SEARCH_NEWLY_PUBLISHED,
    SEARCH_SUFFICIENT_SAMPLE,
    assess_maturity,
)
from app.seo.policy import load_policy

_TODAY = date(2026, 10, 31)
_POLICY = load_policy()


def _assess(**over):
    kwargs = dict(
        article_id=1,
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        today=_TODAY,
        google_index_state="GSC_INDEXED",
        gsc_data_through=date(2026, 10, 28),
        ga4_data_through=date(2026, 10, 28),
        impressions=500,
        organic_sessions=500,
        ga4_configured=True,
        policy=_POLICY,
    )
    kwargs.update(over)
    return assess_maturity(**kwargs)


# ==================== search maturity =========================================
def test_mature_article_with_data_is_a_sufficient_sample() -> None:
    result = _assess()
    assert result.search_state == SEARCH_SUFFICIENT_SAMPLE
    assert result.search_sample_sufficient is True


def test_article_published_today_is_newly_published() -> None:
    result = _assess(published_at=datetime(2026, 10, 31, tzinfo=UTC), impressions=0)
    assert result.search_state == SEARCH_NEWLY_PUBLISHED
    assert result.old_enough_to_evaluate is False
    assert "0 day(s) ago" in result.search_reason


def test_zero_impressions_before_the_age_gate_is_never_a_performance_signal() -> None:
    """公開から日が浅い記事のゼロを「表示回数不足」と呼ばない。"""

    result = _assess(published_at=datetime(2026, 10, 25, tzinfo=UTC), impressions=0)
    assert result.search_state == SEARCH_NEWLY_PUBLISHED


def test_search_data_not_covering_the_publication_date_is_awaiting_data() -> None:
    """データ取得日が公開日より前なら、ゼロは何の証拠にもならない。"""

    result = _assess(
        published_at=datetime(2026, 9, 20, tzinfo=UTC),
        gsc_data_through=date(2026, 9, 15),
        impressions=0,
    )
    assert result.search_state == SEARCH_INDEXED_AWAITING_DATA
    assert result.search_data_covers_article is False
    assert "does not cover the publication date" in result.search_reason


def test_missing_gsc_data_entirely_is_awaiting_data() -> None:
    result = _assess(gsc_data_through=None, impressions=0)
    assert result.search_state == SEARCH_INDEXED_AWAITING_DATA


def test_not_indexed_after_the_age_gate_is_awaiting_index_discovery() -> None:
    result = _assess(google_index_state="GSC_NOT_KNOWN_TO_GOOGLE", impressions=0)
    assert result.search_state == SEARCH_AWAITING_INDEX_DISCOVERY


def test_unknown_index_state_is_not_treated_as_not_indexed() -> None:
    """URL Inspection が未取得なことは「未インデックス」の証拠ではない。"""

    result = _assess(google_index_state="GSC_UNKNOWN", impressions=0)
    assert result.search_state == SEARCH_INDEXED_AWAITING_DATA


def test_indexed_but_no_impressions_is_awaiting_data() -> None:
    result = _assess(impressions=0)
    assert result.search_state == SEARCH_INDEXED_AWAITING_DATA


def test_small_impression_count_is_insufficient_not_poor() -> None:
    result = _assess(impressions=5)
    assert result.search_state == SEARCH_INSUFFICIENT_IMPRESSIONS
    assert result.search_sample_sufficient is False


def test_unknown_publication_date_is_never_evaluated() -> None:
    result = _assess(published_at=None)
    assert result.search_state == SEARCH_NEWLY_PUBLISHED
    assert result.age_days is None


# ==================== engagement maturity =====================================
def test_unconfigured_ga4_is_awaiting_data() -> None:
    result = _assess(ga4_configured=False)
    assert result.engagement_state == ENGAGEMENT_AWAITING_DATA
    assert "not configured" in result.engagement_reason


def test_ga4_without_rows_is_awaiting_data_not_poor_engagement() -> None:
    result = _assess(ga4_data_through=None, organic_sessions=0)
    assert result.engagement_state == ENGAGEMENT_AWAITING_DATA
    assert result.engagement_sample_sufficient is False


def test_small_organic_sample_is_insufficient() -> None:
    result = _assess(organic_sessions=5)
    assert result.engagement_state == ENGAGEMENT_INSUFFICIENT_SAMPLE


def test_large_organic_sample_is_sufficient() -> None:
    result = _assess(organic_sessions=500)
    assert result.engagement_state == ENGAGEMENT_SUFFICIENT_SAMPLE


def test_search_and_engagement_maturity_are_independent() -> None:
    """片方が成熟しても、もう片方の判断材料にはならない。"""

    result = _assess(impressions=5000, organic_sessions=0, ga4_data_through=None)
    assert result.search_state == SEARCH_SUFFICIENT_SAMPLE
    assert result.engagement_state == ENGAGEMENT_AWAITING_DATA


def test_assessment_is_deterministic() -> None:
    assert _assess() == _assess()
