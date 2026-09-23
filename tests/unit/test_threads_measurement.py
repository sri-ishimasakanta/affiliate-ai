"""Threads 計測の読み方 (T4、pure)。

pin する契約:

- 公開直後の 0 は **成績ではない**。経過時間だけで成熟度を決める。
- 「観測できなかった」と「0 だった」を混同しない。
- 分母が足りないときに比率を出さない (0.0 も返さない)。
- 総合スコアを作らない。観察には名前が付いている。
- 閾値は policy から来る。コードに埋めない。
"""

from __future__ import annotations

import pytest

from app.social.threads.measurement import (
    MATURITY_EARLY_OBSERVATION,
    MATURITY_INITIAL_SAMPLE,
    MATURITY_JUST_PUBLISHED,
    MATURITY_MATURE,
    OBS_INSUFFICIENT_AGE,
    OBS_INSUFFICIENT_SAMPLE,
    OBS_INTERACTION_OBSERVED,
    OBS_NO_INTERACTION_YET,
    OBS_NOT_OBSERVED_YET,
    OBS_REPLY_ACTIVITY,
    OBS_VIEWS_OBSERVED,
    classify_maturity,
    interaction_total,
    interactions_per_view,
    length_bucket,
    observe,
)
from app.social.threads.policy import get_measurement_policy, load_measurement_policy

POLICY = get_measurement_policy()


def _snapshot(**values) -> dict:
    base = dict.fromkeys(("views", "likes", "replies", "reposts", "quotes", "shares"))
    base.update(values)
    return base


# -- maturity ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("hours", "stage"),
    [
        (0.1, MATURITY_JUST_PUBLISHED),
        (0.9, MATURITY_JUST_PUBLISHED),
        (1.0, MATURITY_EARLY_OBSERVATION),
        (23.9, MATURITY_EARLY_OBSERVATION),
        (24.0, MATURITY_INITIAL_SAMPLE),
        (71.9, MATURITY_INITIAL_SAMPLE),
        (72.0, MATURITY_MATURE),
        (500.0, MATURITY_MATURE),
    ],
)
def test_maturity_depends_only_on_age(hours: float, stage: str) -> None:
    assert classify_maturity(hours, POLICY).stage == stage


def test_only_mature_posts_are_comparable() -> None:
    assert classify_maturity(0.2, POLICY).comparable is False
    assert classify_maturity(100.0, POLICY).comparable is True


def test_unknown_publication_time_is_treated_as_brand_new() -> None:
    verdict = classify_maturity(None, POLICY)
    assert verdict.stage == MATURITY_JUST_PUBLISHED
    assert verdict.comparable is False
    assert verdict.age_hours is None


def test_maturity_ignores_the_metric_values_entirely() -> None:
    """値は引数に無い。**0 だから未熟、ではない。**"""

    huge = classify_maturity(0.2, POLICY)
    assert huge.stage == MATURITY_JUST_PUBLISHED


# -- observations --------------------------------------------------------------
def test_fresh_post_with_all_zeros_is_not_a_poor_performer() -> None:
    """公開 12 分後、すべて 0。これを「成績が悪い」と読まない。"""

    maturity = classify_maturity(0.2, POLICY)
    observations = observe(
        _snapshot(views=0, likes=0, replies=0, reposts=0, quotes=0, shares=0), maturity, POLICY
    )
    assert OBS_INSUFFICIENT_AGE in observations
    assert OBS_NO_INTERACTION_YET in observations
    assert OBS_VIEWS_OBSERVED not in observations
    # 「良くない」に類する語彙がそもそも存在しない。
    assert not any("poor" in o or "low" in o or "bad" in o for o in observations)


def test_missing_is_not_zero() -> None:
    maturity = classify_maturity(100.0, POLICY)
    assert OBS_NOT_OBSERVED_YET in observe(_snapshot(), maturity, POLICY)
    # 本当に 0 だったときは「未観測」ではない。
    assert OBS_NOT_OBSERVED_YET not in observe(_snapshot(views=0, likes=0), maturity, POLICY)


def test_named_observations_only() -> None:
    maturity = classify_maturity(100.0, POLICY)
    observations = observe(_snapshot(views=120, likes=3, replies=2), maturity, POLICY)
    assert OBS_VIEWS_OBSERVED in observations
    assert OBS_INTERACTION_OBSERVED in observations
    assert OBS_REPLY_ACTIVITY in observations
    assert OBS_INSUFFICIENT_AGE not in observations
    assert OBS_INSUFFICIENT_SAMPLE not in observations


def test_small_view_counts_are_flagged_as_insufficient_sample() -> None:
    maturity = classify_maturity(100.0, POLICY)
    assert OBS_INSUFFICIENT_SAMPLE in observe(_snapshot(views=5, likes=1), maturity, POLICY)


# -- ratios --------------------------------------------------------------------
_MATURE = classify_maturity(100.0, POLICY)
_FRESH = classify_maturity(0.48, POLICY)


def test_ratio_is_none_when_the_denominator_is_unknown_or_zero() -> None:
    assert interactions_per_view(_snapshot(likes=3), POLICY, _MATURE) is None
    assert interactions_per_view(_snapshot(views=0, likes=0), POLICY, _MATURE) is None


def test_ratio_is_none_below_the_minimum_sample() -> None:
    """**0.0 を返さない。** 返すと「効果ゼロ」と読まれる。"""

    assert interactions_per_view(_snapshot(views=10, likes=0), POLICY, _MATURE) is None


def test_ratio_is_none_while_the_post_is_still_young() -> None:
    """実際に起きた誤り: 公開 28 分後、views 83 / 反応 0 で ``0.0`` を出していた。

    分母は足りていても、配信が一巡していない。ここで 0.0 を出すと
    「反応ゼロ」という成績として読まれる。
    """

    assert interactions_per_view(_snapshot(views=83, likes=0, replies=0), POLICY, _FRESH) is None


def test_ratio_is_computed_only_with_enough_views() -> None:
    assert interactions_per_view(_snapshot(views=100, likes=3, replies=2), POLICY, _MATURE) == 0.05


def test_interaction_total_distinguishes_missing_from_zero() -> None:
    assert interaction_total(_snapshot()) is None
    assert interaction_total(_snapshot(likes=0)) == 0
    assert interaction_total(_snapshot(likes=2, replies=1)) == 3


# -- policy --------------------------------------------------------------------
def test_length_buckets_come_from_policy() -> None:
    assert length_bucket(80, POLICY) == "short"
    assert length_bucket(197, POLICY) == "medium"
    assert length_bucket(400, POLICY) == "long"


def test_policy_declares_the_metrics_that_do_not_exist() -> None:
    policy = load_measurement_policy()
    for absent in ("reach", "impressions", "ctr", "engagement_rate"):
        assert absent in policy.unsupported_metrics


def test_policy_version_is_recorded_so_past_judgements_stay_reproducible() -> None:
    assert load_measurement_policy().policy_version
