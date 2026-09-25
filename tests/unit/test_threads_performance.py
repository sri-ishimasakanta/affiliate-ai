"""Threads の成績を同じ経過時間で比べる診断 (pure)。

pin する契約:

- 経過時間は実際の公開時刻から測る。公開前の観測・失敗した観測は使わない。
- チェックポイントに最も近い本物の観測。同じ距離なら早い方。補間しない。
- 許容幅の外は「比較できない」(値は使わない)。欠測は 0 にしない。
- 3 本未満では傾向を言わない。3 本でも確度は very_low。
- 区間の伸びは本物の累積値の差だけ。0 で割らない。
- as_of より後のデータは見ない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.social.threads.performance import (
    STATUS_DECLINE,
    STATUS_INSUFFICIENT,
    STATUS_MIXED,
    STATUS_NO_CLEAR_DECLINE,
    Observation,
    PublicationRecord,
    build_report,
    classify_checkpoint,
    compare_media_ids,
    match_observation_at_age,
    ratio,
    tolerance_minutes,
    velocity,
)
from app.social.threads.policy import get_measurement_policy, get_operations_policy

_T0 = datetime(2026, 9, 25, 0, 0, tzinfo=UTC)  # 09:00 JST
_TZ = ZoneInfo("Asia/Tokyo")
_MP, _OP = get_measurement_policy(), get_operations_policy()


def _obs(minutes: float, views=10, outcome="observed", base=_T0, **metrics) -> Observation:
    values = {"views": views, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0, "shares": 0}
    values.update(metrics)
    return Observation(
        observed_at=base + timedelta(minutes=minutes), outcome=outcome, metrics=values
    )


def _match(observations, hours=1, tolerance=30):
    return match_observation_at_age(
        observations, published_at=_T0, checkpoint_hours=hours, tolerance=tolerance
    )


# == matching ===================================================================
def test_an_exact_checkpoint_observation_is_used() -> None:
    m = _match([_obs(60, views=20)])
    assert (m.comparable, m.value("views"), m.delta_minutes, m.age_hours) == (True, 20, 0.0, 1.0)


def test_slightly_before_and_after_are_both_comparable_with_their_delta() -> None:
    assert _match([_obs(50)]).delta_minutes == -10.0
    assert _match([_obs(75)]).delta_minutes == 15.0
    assert _match([_obs(50)]).comparable and _match([_obs(75)]).comparable


def test_outside_the_tolerance_is_not_comparable() -> None:
    m = _match([_obs(100, views=99)])
    assert m.comparable is False
    assert m.value("views") is None
    assert "40 min away" in m.reason


def test_no_observation_and_pre_publication_observations() -> None:
    assert _match([]).reason == "no observation"
    before = _match([_obs(-5, views=3)])
    assert before.comparable is False and before.observation is None


def test_the_nearest_of_several_observations_wins() -> None:
    m = _match([_obs(20, views=1), _obs(55, views=5), _obs(70, views=7), _obs(200, views=9)])
    assert m.value("views") == 5


def test_ties_choose_the_earlier_observation() -> None:
    m = _match([_obs(70, views=8), _obs(50, views=4)])
    assert m.value("views") == 4  # 同じ 10 分なら早い方 (累積値が小さい)


def test_failed_observations_and_missing_views_are_not_used() -> None:
    assert _match([_obs(60, outcome="failed", views=None)]).comparable is False
    m = _match([_obs(60, views=None)])
    assert (m.comparable, m.reason) == (False, "views missing")


def test_tolerances_come_from_the_refresh_cadence() -> None:
    got = {h: tolerance_minutes(h, _MP, _OP) for h in (1, 3, 24, 72)}
    assert got == {1: 30.0, 3: 30.0, 24: 60.0, 72: 360.0}


# == ratios / velocity ==========================================================
def test_ratio_never_divides_by_zero_or_small_samples() -> None:
    assert ratio(1, 0, 30) is None
    assert ratio(1, None, 30) is None
    assert ratio(1, 20, 30) is None
    assert ratio(3, 60, 30) == 0.05


def test_velocity_uses_only_real_cumulative_values() -> None:
    observations = [_obs(62, views=10), _obs(181, views=30), _obs(360, views=33)]
    matches = {
        h: match_observation_at_age(
            observations, published_at=_T0, checkpoint_hours=h, tolerance=30
        )
        for h in (1, 3, 6)
    }
    assert velocity(matches, 0, 1)["views_gained"] == 10
    step = velocity(matches, 1, 3)
    assert step["views_gained"] == 20
    assert step["views_per_hour"] == round(20 / ((181 - 62) / 60), 2)
    assert velocity(matches, 3, 6)["views_gained"] == 3


def test_velocity_is_unavailable_without_both_ends() -> None:
    matches = {
        h: match_observation_at_age(
            [_obs(60, views=5)], published_at=_T0, checkpoint_hours=h, tolerance=30
        )
        for h in (1, 3)
    }
    assert velocity(matches, 1, 3)["available"] is False


# == classification =============================================================
@pytest.mark.parametrize(
    ("values", "status"),
    [
        ([10, 8], STATUS_INSUFFICIENT),
        ([16, 11, 6], STATUS_DECLINE),
        ([12, 11, 4, 25], STATUS_NO_CLEAR_DECLINE),
        ([10, 12, 15], STATUS_NO_CLEAR_DECLINE),
        ([10, 10, 10], STATUS_NO_CLEAR_DECLINE),
        ([20, 5, 15], STATUS_NO_CLEAR_DECLINE),  # 最新は前の中央値を上回って回復
        ([20, 15, 13], STATUS_MIXED),  # 下がり続けるが、最新は前の中央値の 74% (70% 以下ではない)
        ([20, 18, 17], STATUS_MIXED),  # 3 本とも下がるが、最新は中央値の 89% (はっきり弱くない)
    ],
)
def test_checkpoint_classification(values, status) -> None:
    result = classify_checkpoint(list(enumerate(values, start=1)))
    assert result["status"] == status
    assert [x["views"] for x in result["sequence"]] == values  # 根拠の値を必ず添える


def test_three_posts_are_very_low_confidence() -> None:
    assert classify_checkpoint([(1, 16), (2, 11), (3, 6)])["confidence"] == "very_low"


def test_one_weak_post_is_not_a_repeated_decline() -> None:
    result = classify_checkpoint([(1, 20), (2, 22), (3, 24), (4, 5)])
    assert result["last_three_strictly_decreasing"] is False
    assert result["status"] == STATUS_MIXED


# == report =====================================================================
def _record(pid, hours_after, views_by_minute, **kw) -> PublicationRecord:
    base = _T0 + timedelta(hours=hours_after)
    return PublicationRecord(
        publication_id=pid,
        published_at=base,
        basis_source="remote_timestamp",
        media_id=f"m{pid}",
        angle=kw.get("angle", "insight"),
        link_mode=kw.get("link_mode", "none"),
        character_count=200,
        topic=kw.get("topic", "t"),
        text=kw.get("text", f"本文 {pid}"),
        observations=tuple(_obs(m, views=v, base=base) for m, v in views_by_minute),
    )


def _records():
    return [
        _record(1, 0, [(60, 30), (180, 40)], link_mode="none"),
        _record(2, 3, [(60, 20), (180, 30)], link_mode="article"),
        _record(3, 6, [(60, 10), (180, 20)], link_mode="none"),
    ]


def _report(records, as_of=_T0 + timedelta(days=2)):
    return build_report(records, as_of=as_of, measurement_policy=_MP, operations_policy=_OP, tz=_TZ)


def test_the_report_compares_equal_ages_and_attaches_relative_values() -> None:
    report = _report(_records())
    one_hour = report["checkpoint_summary"]["1h"]
    assert one_hour["comparable_count"] == 3
    assert [x["views"] for x in one_hour["classification"]["sequence"]] == [30, 20, 10]
    third = report["publications"][2]["checkpoints"]["1h"]
    assert third["vs_previous_pct"] == -50.0
    assert third["vs_earlier_median"] == round(10 / 25, 3)
    assert report["checkpoint_summary"]["24h"]["comparable_count"] == 0
    assert report["publications"][0]["checkpoints"]["24h"]["metrics"] is None  # 0 ではない


def test_link_and_time_buckets_are_grouped() -> None:
    report = _report(_records())
    links = report["dimension_summary"]["link_mode"]
    assert links["none"]["publications"] == [1, 3]
    assert links["article"]["small_sample"] is True
    assert report["publications"][0]["time_bucket"] == "morning"  # 09:00 JST
    assert set(report["dimension_summary"]["time_bucket"]) == {"morning", "midday", "afternoon"}


def test_as_of_ignores_later_data_and_is_reproducible() -> None:
    records = _records()
    early = _T0 + timedelta(hours=7, minutes=30)
    first = _report(records, as_of=early)
    later = [
        PublicationRecord(
            **{
                **r.__dict__,
                "observations": (*r.observations, _obs(24 * 60, views=999, base=r.published_at)),
            }
        )
        for r in records
    ] + [_record(4, 8, [(60, 500)])]  # as_of の後に公開
    second = _report(later, as_of=early)
    assert json.dumps(first) == json.dumps(second)
    assert first["publication_count"] == 3


def test_similarity_and_media_id_comparison() -> None:
    records = [
        _record(1, 0, [], text="業務の効率化は範囲を決めてから。 https://a.example/?x=1"),
        _record(2, 3, [], text="業務の効率化は範囲を決めてから始める。"),
    ]
    sim = _report(records)["similarity"]
    assert sim["consecutive"][0]["bigram_jaccard"] > 0.7
    assert any(p["publication_ids"] == [1, 2] for p in sim["shared_phrases"])
    assert compare_media_ids(["a", "b"], ["b", "c"]) == {
        "tracked": ["b"],
        "remote_not_tracked": ["c"],
        "internal_not_remote": ["a"],
    }


def test_overlapping_shared_fragments_collapse_to_one_phrase() -> None:
    from app.social.threads.performance import shared_phrases

    phrases = shared_phrases(
        {4: "KrispとFireflies.aiは、公式に記載。", 5: "議事録のKrispとFireflies.aiを比べる。"}
    )
    assert phrases == [{"phrase": "KrispとFireflies.ai", "publication_ids": [4, 5]}]


def test_a_phrase_contained_in_a_longer_shared_phrase_is_not_repeated() -> None:
    from app.social.threads.performance import shared_phrases

    phrases = shared_phrases(
        {
            4: "KrispとFireflies.aiは、公式に記載。",
            5: "KrispとFireflies.ai を比べる。Fireflies.aiは無料。",
        }
    )
    assert [p["phrase"] for p in phrases] == ["KrispとFireflies.aiは"]
