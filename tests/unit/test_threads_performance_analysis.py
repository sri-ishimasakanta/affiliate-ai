"""自分の投稿の成績の分析と、生成への補助の参考 (T6.5, pure)。

pin する契約:

- 欠測は 0 にしない。views が 0 / 不明なら比率を計算しない (0 で割らない)。
- 経過時間の違う投稿を累積値で比べない。比べるのは同じ経過時間のチェックポイントだけ。
- Growth と通常の投稿を混ぜない。比べる本数が下限未満なら ``insufficient_data``。
- 比べるチェックポイントはデータで決める (届く証拠の段階がいちばん強い中で、いちばん遅いもの)。
- 少ない本数・1 本で向きが変わるまとまりは、向きを言わない。
- 参考は決定的 (入力の順番で変わらない)。証拠が無ければ中立で、prompt に何も足さない。
- 参考に投稿の本文を入れない (特徴だけ)。
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.social.threads.performance import Observation, PublicationRecord
from app.social.threads.performance_analysis import (
    DIRECTION_HIGHER,
    DIRECTION_LOWER,
    DIRECTION_NONE,
    EVIDENCE_HYPOTHESIS,
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_PRELIMINARY,
    LANE_GROWTH,
    LANE_REGULAR,
    MODE_ADVISORY,
    MODE_NEUTRAL,
    NO_TOPIC,
    SCHEMA_VERSION,
    EvidenceThresholds,
    PostInput,
    analyze_post,
    build_analysis,
    build_feedback,
    comparison_checkpoint,
    derived_metrics,
    evidence_status,
    filter_posts,
    neutral_feedback,
    percentile_rank,
    render_feedback_sections,
    supported_values_for,
)
from app.social.threads.policy import get_measurement_policy, get_operations_policy, get_policy

_T0 = datetime(2026, 9, 25, 0, 0, tzinfo=UTC)  # 09:00 JST
_TZ = ZoneInfo("Asia/Tokyo")
_MP, _OP = get_measurement_policy(), get_operations_policy()
_TH = EvidenceThresholds()  # 5 / 10 / 30
_SUPPORTED = supported_values_for(get_policy(), _MP)


def _metrics(views=10, **overrides):
    values = {"views": views, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0, "shares": 0}
    values.update(overrides)
    return values


def _post(pid, *, published=_T0, observations=(), lane=LANE_REGULAR, angle="insight",
          topic=None, recorded=False, hook=None, chars=None, article_id=25):  # fmt: skip
    record = PublicationRecord(
        publication_id=pid, published_at=published, basis_source="remote_timestamp",
        proposal_id=pid, angle=angle, character_count=chars,
        article_id=article_id if lane == LANE_REGULAR else None,
        text=f"本文そのもの {pid}",
        observations=tuple(Observation(published + timedelta(minutes=m), outcome, metrics)
                           for m, outcome, metrics in observations),
    )  # fmt: skip
    return PostInput(record=record, lane=lane, content_kind=lane, threads_topic=topic,
                     topic_recorded=recorded, conversation_hook=hook)  # fmt: skip


def _obs(minutes, views=10, outcome="observed", **overrides):
    return (minutes, outcome, _metrics(views, **overrides))


def _one(post, as_of):
    return analyze_post(post, as_of=as_of, tz=_TZ, measurement_policy=_MP, operations_policy=_OP)


def _analysis(inputs, as_of):
    return build_analysis(inputs, as_of=as_of, tz=_TZ, measurement_policy=_MP,
                          operations_policy=_OP, thresholds=_TH)  # fmt: skip


def _two_angles(n_each=6, *, high=100, low=10):
    """同じ経過時間 (1h) で、comparison は views が多く、insight は少ない通常の投稿。"""

    inputs = []
    for i in range(n_each):
        inputs.append(_post(i + 1, published=_T0 + timedelta(minutes=10 * i),
                            angle="comparison", observations=[_obs(60, views=high + i)]))
        inputs.append(_post(i + 101, published=_T0 + timedelta(minutes=10 * i + 5),
                            angle="insight", observations=[_obs(60, views=low + i)]))
    as_of = _T0 + timedelta(minutes=10 * n_each + 120)  # 全員 1h を過ぎ、3h にはまだ届かない
    return inputs, as_of


# == 1 本の値 ===================================================================================
def test_zero_views_never_divides_and_rates_are_unknown() -> None:
    d = derived_metrics(_metrics(0), minimum_views=30)
    assert d["total_engagement"] == 0
    assert (d["engagement_per_view"], d["conversation_rate"], d["amplification_rate"]) == (
        None, None, None)  # fmt: skip
    assert (d["rate_reliable"], d["rate_note"]) == (False, "zero_views")


def test_missing_metrics_are_unknown_not_zero() -> None:
    d = derived_metrics(_metrics(50, likes=None), minimum_views=30)
    assert d["total_engagement"] is None and d["engagement_per_view"] is None
    assert d["conversation_rate"] == 0.0  # replies は観測済み
    missing_views = derived_metrics(_metrics(None), minimum_views=30)
    assert missing_views["rate_note"] == "views_missing"
    assert missing_views["conversation_rate"] is None


def test_rates_below_the_view_minimum_are_computed_but_not_reliable() -> None:
    low = derived_metrics(_metrics(20, likes=2, replies=1, shares=1), minimum_views=30)
    assert low["engagement_per_view"] == 0.2
    assert (low["rate_reliable"], low["rate_note"]) == (False, "views_below_ratio_minimum")
    ok = derived_metrics(_metrics(40, likes=2, replies=1, reposts=1), minimum_views=30)
    assert (ok["engagement_per_view"], ok["conversation_rate"], ok["amplification_rate"]) == (
        0.1, 0.025, 0.025)  # fmt: skip
    assert ok["rate_reliable"] is True


def test_a_post_without_insight_is_no_insight_and_not_comparable() -> None:
    post = _one(_post(1), _T0 + timedelta(hours=2))
    assert post["completeness"]["status"] == "no_insight"
    assert post["latest_observation"] is None and post["latest_metrics"] is None
    assert post["checkpoints"]["1h"] == {**post["checkpoints"]["1h"], "reached": True,
                                         "comparable": False}  # fmt: skip
    assert post["checkpoints"]["3h"] == {"reached": False, "comparable": False}


def test_one_snapshot_has_no_growth_delta() -> None:
    post = _one(_post(1, observations=[_obs(60, 12)]), _T0 + timedelta(hours=2))
    assert post["completeness"] == {"status": "complete", "observed_snapshots": 1,
                                    "failed_snapshots": 0, "latest_missing_metrics": []}
    assert post["first_observation"] == {k: v for k, v in post["latest_observation"].items()
                                         if k != "insight_age_hours"}  # fmt: skip
    assert post["latest_observation"]["insight_age_hours"] == 1.0
    assert post["growth_delta"] is None


def test_multiple_snapshots_give_first_latest_and_growth_delta() -> None:
    post = _one(_post(1, observations=[_obs(30, 4), _obs(60, 10), _obs(180, 34, outcome="failed"),
                                       _obs(150, 25)]), _T0 + timedelta(hours=4))  # fmt: skip
    assert post["first_observation"]["metrics"]["views"] == 4
    assert post["latest_metrics"]["views"] == 25  # 失敗した観測は使わない
    assert post["growth_delta"]["hours"] == 2.0
    assert post["growth_delta"]["gained"]["views"] == 21
    assert post["growth_delta"]["views_per_hour"] == 10.5
    assert post["completeness"]["failed_snapshots"] == 1


def test_a_partial_latest_snapshot_is_reported_as_partial() -> None:
    post = _one(_post(1, observations=[_obs(60, 10, shares=None)]), _T0 + timedelta(hours=2))
    assert post["completeness"]["status"] == "partial"
    assert post["completeness"]["latest_missing_metrics"] == ["shares"]
    assert post["checkpoints"]["1h"]["components"]["engagement"] is None
    assert post["checkpoints"]["1h"]["components"]["reach"] == 10


def test_observations_after_as_of_are_not_seen() -> None:
    post = _one(_post(1, observations=[_obs(60, 10), _obs(300, 99)]), _T0 + timedelta(hours=2))
    assert post["latest_metrics"]["views"] == 10
    assert post["age_hours"] == 2.0


def test_the_sent_topic_is_distinguished_from_an_unrecorded_one() -> None:
    as_of = _T0 + timedelta(hours=1)
    assert _one(_post(1, topic="AI Threads", recorded=True), as_of)["threads_topic"] == "AI Threads"
    assert _one(_post(1, topic=None, recorded=True), as_of)["threads_topic"] == NO_TOPIC
    assert _one(_post(1), as_of)["threads_topic"] == "unknown"


# == 比べる相手 ==================================================================================
def test_percentile_rank_is_a_midrank_among_the_others() -> None:
    assert percentile_rank(5, [1, 5, 9, 9]) == 0.375
    assert percentile_rank(0, [0, 0, 0]) == 0.5
    assert percentile_rank(3, []) is None


def test_evidence_tiers_follow_the_observation_policy_thresholds() -> None:
    assert [evidence_status(n, _TH) for n in (4, 5, 9, 10, 29, 30)] == [
        "insufficient_data", "hypothesis", "hypothesis", "preliminary", "preliminary",
        "descriptive"]  # fmt: skip
    policy = {"evidence_thresholds": {"author_baseline_min_other_posts": 3,
                                      "group_candidate_pattern_min": 6,
                                      "group_stronger_descriptive_min": 12}}
    assert EvidenceThresholds.from_policy(policy) == EvidenceThresholds(3, 6, 12)


def test_the_checkpoint_is_the_latest_within_the_strongest_tier() -> None:
    counts = {"1h": 26, "3h": 25, "6h": 24, "12h": 22, "24h": 19, "72h": 5}
    assert comparison_checkpoint(counts, _TH) == "24h"  # 72h (5 本) は弱い段階
    assert comparison_checkpoint({"1h": 6, "3h": 5, "6h": 2}, _TH) == "3h"
    assert comparison_checkpoint({"1h": 4, "3h": 1}, _TH) is None


def test_posts_are_compared_at_equal_age_not_by_cumulative_latest_views() -> None:
    inputs = [_post(i, published=_T0 + timedelta(hours=i), observations=[_obs(60, 20 + i)])
              for i in range(1, 6)]  # fmt: skip
    # 古い投稿: 1h では少ないが、後で大きく伸びた (最新の累積は一番多い)。
    inputs.append(_post(99, published=_T0 - timedelta(hours=30),
                        observations=[_obs(60, 5), _obs(60 * 30, 5000)]))
    report = _analysis(inputs, _T0 + timedelta(hours=8))
    old = next(p for p in report["posts"] if p["publication_id"] == 99)
    assert report["cohorts"][LANE_REGULAR]["checkpoint"] == "1h"
    assert old["latest_metrics"]["views"] == 5000
    reach = old["evaluation"]["components"]["reach"]
    assert (reach["value"], reach["percentile_rank"], reach["position"]) == (
        5, 0.0, "lower_quartile")  # fmt: skip


def test_growth_posts_are_never_compared_with_regular_posts() -> None:
    inputs, as_of = _two_angles()
    inputs.append(_post(500, lane=LANE_GROWTH, angle="account_growth",
                        observations=[_obs(60, 1000)]))
    report = _analysis(inputs, as_of)
    assert report["cohorts"][LANE_REGULAR]["comparable"] == 12  # growth は入らない
    assert report["cohorts"][LANE_GROWTH]["checkpoint"] is None
    growth = next(p for p in report["posts"] if p["publication_id"] == 500)
    assert growth["evaluation"]["status"] == EVIDENCE_INSUFFICIENT
    assert report["summary"]["growth_posts"] == 1
    assert any(w.startswith("growth: no equal-age checkpoint") for w in report["warnings"])
    top = next(p for p in report["posts"] if p["publication_id"] == 6)
    assert top["evaluation"]["components"]["reach"]["percentile_rank"] == 1.0


def test_a_small_cohort_is_insufficient_data() -> None:
    inputs = [_post(i, published=_T0 + timedelta(minutes=i), observations=[_obs(60, i)])
              for i in range(1, 5)]  # fmt: skip
    report = _analysis(inputs, _T0 + timedelta(hours=2))
    assert report["cohorts"][LANE_REGULAR]["checkpoint"] is None
    assert {p["evaluation"]["status"] for p in report["posts"]} == {EVIDENCE_INSUFFICIENT}
    assert build_feedback(report).mode == MODE_NEUTRAL


def test_sparse_rates_do_not_weaken_the_post_status() -> None:
    inputs, as_of = _two_angles()  # views < 30 の投稿は比率を比べない
    report = _analysis(inputs, as_of)
    low = next(p for p in report["posts"] if p["publication_id"] == 101)
    assert low["evaluation"]["components"]["engagement_rate"]["value"] is None
    assert low["evaluation"]["status"] == EVIDENCE_PRELIMINARY  # 12 本の views の比較


def test_the_analysis_is_deterministic_and_order_independent() -> None:
    inputs, as_of = _two_angles()
    shuffled = list(inputs)
    random.Random(7).shuffle(shuffled)
    a, b = _analysis(inputs, as_of), _analysis(shuffled, as_of)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    assert build_feedback(a, supported_values=_SUPPORTED).fingerprint == build_feedback(
        b, supported_values=_SUPPORTED).fingerprint  # fmt: skip
    assert a["schema_version"] == SCHEMA_VERSION and a["read_only"] is True


# == まとまり ===================================================================================
def test_segments_report_n_and_direction_against_the_rest() -> None:
    inputs, as_of = _two_angles()
    report = _analysis(inputs, as_of)
    angle = report["regular_segments"]["angle"]
    assert angle["comparison"]["posts"] == 6
    assert angle["comparison"]["components"]["reach"]["n"] == 6
    assert angle["comparison"]["components"]["reach"]["evidence"] == EVIDENCE_HYPOTHESIS
    assert angle["comparison"]["components"]["reach"]["direction"] == DIRECTION_HIGHER
    assert angle["insight"]["components"]["reach"]["direction"] == DIRECTION_LOWER
    # 全員同じ値の成分 (engagement はすべて 0) は向きを言わない。
    assert angle["comparison"]["components"]["engagement"]["direction"] == DIRECTION_NONE


def test_small_or_unstable_segments_have_no_direction() -> None:
    inputs, as_of = _two_angles(n_each=4)  # 4 本ずつ (下限 5 未満)
    angle = _analysis(inputs, as_of)["regular_segments"]["angle"]
    assert angle["comparison"]["components"]["reach"]["direction"] == DIRECTION_NONE
    assert angle["comparison"]["components"]["reach"]["evidence"] == EVIDENCE_INSUFFICIENT


# == 参考 =======================================================================================
def test_feedback_is_advisory_with_supported_and_weak_patterns() -> None:
    inputs, as_of = _two_angles()
    feedback = build_feedback(_analysis(inputs, as_of), supported_values=_SUPPORTED)
    assert feedback.mode == MODE_ADVISORY
    assert (feedback.checkpoint, feedback.cohort_n) == ("1h", 12)
    assert [(p["value"], p["component"]) for p in feedback.supported] == [("comparison", "reach")]
    assert [(p["value"], p["component"]) for p in feedback.weak] == [("insight", "reach")]
    for p in (*feedback.supported, *feedback.weak):
        assert p["evidence"] == EVIDENCE_HYPOTHESIS and "仮説" in p["statement_ja"]
        assert "原因ではなく" in p["statement_ja"]
    blob = json.dumps(feedback.as_dict(), ensure_ascii=False)
    assert "本文そのもの" not in blob  # 本文は入れない (特徴だけ)
    assert feedback.provenance()["patterns"][0] == {
        "dimension": "angle", "value": "comparison", "component": "reach",
        "direction": DIRECTION_HIGHER, "evidence": EVIDENCE_HYPOTHESIS}  # fmt: skip


def test_values_the_generator_cannot_produce_are_context_only() -> None:
    inputs, as_of = _two_angles()
    feedback = build_feedback(_analysis(inputs, as_of),
                              supported_values={"angle": ("insight",)})  # fmt: skip
    assert feedback.supported == ()
    assert [p["value"] for p in feedback.context] == ["comparison"]
    assert [p["value"] for p in feedback.weak] == ["insight"]


def test_feedback_falls_back_to_neutral() -> None:
    assert build_feedback({"schema_version": "other"}).mode == MODE_NEUTRAL
    inputs = [_post(1, observations=[_obs(60, 5)])]
    neutral = build_feedback(_analysis(inputs, _T0 + timedelta(hours=2)))
    assert neutral.mode == MODE_NEUTRAL and neutral.checkpoint is None
    assert render_feedback_sections(neutral) == []
    assert render_feedback_sections(None) == []
    assert render_feedback_sections(neutral_feedback("x", "reason")) == []


def test_feedback_prompt_sections_keep_priorities_and_diversity() -> None:
    inputs, as_of = _two_angles()
    lines = render_feedback_sections(build_feedback(_analysis(inputs, as_of),
                                                    supported_values=_SUPPORTED))  # fmt: skip
    assert lines[0].startswith("## 過去の成績からの補助の参考")
    text = "\n".join(lines)
    assert "事実・記事の根拠・文体の規則が優先" in text
    assert "多様さを保つ" in text and "直近 1〜2 本" in text
    assert "「comparison」" in text and "「insight」" in text


def test_the_fingerprint_ignores_growth_latest_metrics() -> None:
    inputs, as_of = _two_angles()
    a = _analysis([*inputs, _post(500, lane=LANE_GROWTH, observations=[_obs(60, 3)])], as_of)
    b = _analysis([*inputs, _post(500, lane=LANE_GROWTH, observations=[_obs(60, 9)])], as_of)
    assert build_feedback(a).fingerprint == build_feedback(b).fingerprint
    assert build_feedback(a).growth == {"posts": 1, "evidence": EVIDENCE_INSUFFICIENT,
                                        "note": "growth posts are compared only with growth posts"}


def test_filters_narrow_the_display_only() -> None:
    inputs, as_of = _two_angles()
    inputs.append(_post(500, lane=LANE_GROWTH, observations=[_obs(60, 3)]))
    report = _analysis(inputs, as_of)
    assert {p["lane"] for p in filter_posts(report, lane=LANE_GROWTH)} == {LANE_GROWTH}
    assert {p["angle"] for p in filter_posts(report, angle="comparison")} == {"comparison"}
    assert filter_posts(report, min_age_hours=1000) == []
    assert filter_posts(report, topic="unknown") == report["posts"]
