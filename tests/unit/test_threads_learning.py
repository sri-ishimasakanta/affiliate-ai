"""Threads の学習 (T5、pure)。

pin する契約:

- 成熟の判定・本数と views の下限・長さの帯は T4 の計測ポリシーをそのまま使う。
- 1 投稿につき代表の観測は 1 つ (72h から 24h の窓の最初の observed)。窓の後の観測は
  代表を変えない。1 時間後の値と 7 日後の値を並べない。
- 欠測は 0 ではない。views=0 は欠測ではない。
- 1〜2 本では何も言わない。3 本でも、全部の規則を満たしたときだけ。
- 1 本の大きな値で本数の不足を埋めない。若い投稿は結論を動かさない。
- 外れ値 1 本で所見を作らない (中央値と leave-one-out)。
- 一度立った所見は、少し揺れたくらいでは消えない (ヒステリシス)。
- 未知の切り口・リンクの値でも壊れない。trigger は比べない。
- 同じ入力からは同じ出力 (順序も含めて)。
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.social.threads.learning import (
    EVIDENCE_AWAITING,
    EVIDENCE_COMPARABLE,
    EVIDENCE_IMMATURE,
    EVIDENCE_MISSED,
    FINDING_NO_CLEAR_DIFFERENCE,
    FINDING_OBSERVED_DIFFERENCE,
    STATUS_INSUFFICIENT_COMPARABLE,
    STATUS_INSUFFICIENT_SAMPLE,
    STATUS_INSUFFICIENT_VIEWS,
    STATUS_SUFFICIENT,
    PublicationFact,
    SnapshotFact,
    analyze,
    compare_reports,
    daypart_of,
    dimension_values,
    interaction_rate,
    interactions_of,
    select_canonical,
)
from app.social.threads.policy import get_measurement_policy

_TZ = ZoneInfo("Asia/Tokyo")
_POLICY = get_measurement_policy()
#: 2026-09-01 (Tue) 09:00 JST
_T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
_NOW = _T0 + timedelta(days=60)
_FORBIDDEN = ("best", "worst", "winner", "loser", "optimal", "guarantee", "always")


def _snap(sid, published, age, *, outcome="observed", **metrics) -> SnapshotFact:
    values = {"views": 100, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0, "shares": 0}
    values.update(metrics)
    return SnapshotFact(
        snapshot_id=sid,
        observed_at=published + timedelta(hours=age),
        outcome=outcome,
        age_hours=age,
        metrics=values,
    )


def _pub(
    pid,
    *,
    views=100,
    likes=5,
    angle="insight",
    link_mode="none",
    chars=200,
    trigger="automatic",
    article=21,
    published=None,
    snapshots=None,
    **metrics,
) -> PublicationFact:
    published = published or _T0 + timedelta(hours=pid)
    if snapshots is None:
        snapshots = (_snap(pid * 100, published, 73, views=views, likes=likes, **metrics),)
    return PublicationFact(
        publication_id=pid,
        published_at=published,
        angle=angle,
        link_mode=link_mode,
        character_count=chars,
        trigger=trigger,
        article_id=article,
        article_title=f"article {article}",
        topic="topic",
        proposal_id=pid,
        snapshots=tuple(snapshots),
    )


def _report(pubs, *, as_of=_NOW):
    return analyze(pubs, as_of=as_of, policy=_POLICY, tz=_TZ)


def _value(report, dimension, value):
    return next(v for v in report["dimensions"][dimension]["values"] if v["value"] == value)


def _rated(pid, rate, **kwargs):
    """views=1000 で interaction_rate がちょうど ``rate`` になる投稿。"""

    return _pub(pid, views=1000, likes=round(rate * 1000), **kwargs)


# == T4 policy reuse ============================================================
def test_the_t4_measurement_policy_is_reused_not_duplicated() -> None:
    assert _POLICY.mature_after_hours == _POLICY.maturity_hours["initial_sample"] == 72
    assert _POLICY.minimum_mature_posts == 3
    assert _POLICY.minimum_views_for_ratio == 30
    report = _report([])
    assert report["thresholds"]["mature_after_hours"] == 72
    assert report["thresholds"]["comparable_snapshot_window_hours"] == [72, 96]
    assert report["thresholds"]["minimum_mature_posts_per_value"] == 3
    assert report["thresholds"]["minimum_views_for_rate"] == 30
    assert report["policy_version"] == _POLICY.policy_version


# == maturity ===================================================================
def test_a_post_younger_than_72h_is_immature() -> None:
    pub = _pub(1, snapshots=())
    canonical = select_canonical(pub, pub.published_at + timedelta(hours=71.9), _POLICY)
    assert canonical.state == EVIDENCE_IMMATURE
    assert canonical.maturity_stage == "initial_sample"


def test_a_mature_post_needs_a_snapshot_inside_the_window() -> None:
    published = _T0
    early_only = _pub(1, published=published, snapshots=(_snap(1, published, 50),))
    at_80 = published + timedelta(hours=80)
    at_100 = published + timedelta(hours=100)
    # 窓 (72-96h) がまだ開いているうちは「観測待ち」。閉じたら「比較できる観測が無い」。
    assert select_canonical(early_only, at_80, _POLICY).state == EVIDENCE_AWAITING
    assert select_canonical(early_only, at_100, _POLICY).state == EVIDENCE_MISSED


# == canonical snapshot =========================================================
def test_the_first_observed_snapshot_in_the_window_is_canonical() -> None:
    published = _T0
    pub = _pub(
        1,
        published=published,
        snapshots=(
            _snap(1, published, 30, views=10),
            _snap(2, published, 72.5, outcome="failed", views=None),
            _snap(3, published, 73, views=200),
            _snap(4, published, 79, views=300),
            _snap(5, published, 200, views=90000),
        ),
    )
    canonical = select_canonical(pub, _NOW, _POLICY)
    assert canonical.state == EVIDENCE_COMPARABLE
    assert canonical.snapshot.snapshot_id == 3
    assert canonical.snapshot_age_hours == 73


def test_a_snapshot_with_views_is_preferred_inside_the_window() -> None:
    published = _T0
    pub = _pub(
        1,
        published=published,
        snapshots=(_snap(1, published, 73, views=None), _snap(2, published, 74, views=150)),
    )
    assert select_canonical(pub, _NOW, _POLICY).snapshot.snapshot_id == 2


def test_the_window_end_is_exclusive_and_the_future_is_not_seen() -> None:
    published = _T0
    late = _pub(1, published=published, snapshots=(_snap(1, published, 96),))
    assert select_canonical(late, _NOW, _POLICY).state == EVIDENCE_MISSED
    pub = _pub(2, published=published, snapshots=(_snap(2, published, 80),))
    assert select_canonical(pub, published + timedelta(hours=79), _POLICY).state == (
        EVIDENCE_AWAITING
    )


def test_one_publication_is_one_example_however_many_snapshots_it_has() -> None:
    published = _T0
    snaps = [_snap(i, published, 72 + i) for i in range(1, 20)]
    report = _report([_pub(1, published=published, snapshots=snaps)])
    assert report["counts"]["comparable"] == 1
    assert report["overall"]["views"]["observed"] == 1


def test_later_fluctuations_do_not_change_the_report() -> None:
    published = _T0
    base = [_snap(1, published, 73, views=100, likes=5)]
    before = _report([_pub(1, published=published, snapshots=base)])
    wild = [
        *base,
        _snap(2, published, 150, views=1_000_000, likes=900),
        _snap(3, published, 300, views=3, likes=0),
    ]
    after = _report([_pub(1, published=published, snapshots=wild)])
    assert json.dumps(before) == json.dumps(after)


# == dimensions =================================================================
def test_jst_hour_daypart_and_weekday() -> None:
    # 2026-09-01T15:30Z = 2026-09-02 (Wed) 00:30 JST
    pub = _pub(1, published=datetime(2026, 9, 1, 15, 30, tzinfo=UTC))
    values = dimension_values(pub, _POLICY, _TZ)
    assert values["hour"] == "00"
    assert values["daypart"] == "night"
    assert values["weekday"] == "Wed"
    # naive (SQLite) は UTC とみなす
    naive = _pub(2, published=datetime(2026, 9, 1, 13, 0))
    assert dimension_values(naive, _POLICY, _TZ)["hour"] == "22"


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (5, "morning"),
        (9, "morning"),
        (10, "midday"),
        (14, "afternoon"),
        (18, "evening"),
        (21, "evening"),
        (22, "night"),
        (4, "night"),
        (0, "night"),
    ],
)
def test_dayparts(hour, expected) -> None:
    assert daypart_of(hour, _POLICY.dayparts) == expected


@pytest.mark.parametrize(
    ("chars", "band"),
    [(1, "short"), (150, "short"), (151, "medium"), (300, "medium"), (301, "long"), (500, "long")],
)
def test_the_t4_length_bands(chars, band) -> None:
    assert dimension_values(_pub(1, chars=chars), _POLICY, _TZ)["length_band"] == band


def test_unknown_future_values_remain_reportable() -> None:
    pubs = [
        _pub(1, angle="hot_take", link_mode="note"),
        _pub(2, angle=None, link_mode=None, trigger=None),
    ]
    report = _report(pubs)
    angles = [v["value"] for v in report["dimensions"]["angle"]["values"]]
    assert angles == [
        "insight",
        "common_mistake",
        "comparison",
        "question",
        "beginner_tip",
        "hot_take",
        "unknown",
    ]
    links = [v["value"] for v in report["dimensions"]["link_mode"]["values"]]
    assert links == ["none", "article", "note", "unknown"]
    assert _value(report, "angle", "hot_take")["counts"]["comparable"] == 1
    json.dumps(report)  # 機械可読のまま


def test_grouping_counts_each_publication_once_per_dimension() -> None:
    pubs = [_pub(1), _pub(2, angle="comparison"), _pub(3, angle="comparison", article=24)]
    report = _report(pubs)
    for dimension, data in report["dimensions"].items():
        assert sum(v["counts"]["publications"] for v in data["values"]) == 3, dimension
    assert _value(report, "source_article", "24")["label"] == "article 24"


# == metrics ====================================================================
def test_interactions_need_all_five_metrics() -> None:
    full = _snap(1, _T0, 73, likes=2, replies=1, reposts=1, quotes=0, shares=3)
    assert interactions_of(full) == 7
    partial = _snap(2, _T0, 73, likes=2, shares=None)
    assert interactions_of(partial) is None


def test_the_rate_is_only_computed_for_enough_observed_views() -> None:
    assert interaction_rate(100, 5, _POLICY) == 0.05
    assert interaction_rate(29, 5, _POLICY) is None
    assert interaction_rate(0, 0, _POLICY) is None
    assert interaction_rate(None, 5, _POLICY) is None
    assert interaction_rate(100, None, _POLICY) is None


def test_zero_views_is_zero_and_missing_views_is_missing() -> None:
    report = _report([_pub(1, views=0, likes=0), _pub(2, views=None)])
    overall = report["overall"]
    assert overall["zero_views"] == 1
    assert overall["missing"]["views"] == 1
    assert overall["views"]["observed"] == 1
    assert overall["views"]["total"] == 0
    posts = {p["publication_id"]: p for p in report["publications"]}
    assert posts[1]["metrics"]["views"] == 0
    assert posts[1]["interaction_rate"] is None
    assert posts[2]["metrics"]["views"] is None
    assert posts[2]["missing_metrics"] == ["views"]


# == small-sample safety ========================================================
@pytest.mark.parametrize("n", [1, 2])
def test_one_or_two_mature_posts_are_insufficient(n) -> None:
    report = _report([_rated(i, 0.05) for i in range(1, n + 1)])
    assert report["evidence_status"] == STATUS_INSUFFICIENT_SAMPLE
    assert _value(report, "angle", "insight")["status"] == STATUS_INSUFFICIENT_SAMPLE
    assert report["findings"] == []


def test_three_mature_posts_are_sufficient_only_when_every_rule_passes() -> None:
    assert _report([_rated(i, 0.05) for i in (1, 2, 3)])["evidence_status"] == (STATUS_SUFFICIENT)
    missing = [_rated(1, 0.05), _rated(2, 0.05), _pub(3, views=None)]
    assert _report(missing)["evidence_status"] == STATUS_INSUFFICIENT_COMPARABLE
    low = [_rated(1, 0.05), _rated(2, 0.05), _pub(3, views=20, likes=1)]
    assert _report(low)["evidence_status"] == STATUS_INSUFFICIENT_VIEWS
    partial = [_rated(1, 0.05), _rated(2, 0.05), _pub(3, shares=None)]
    assert _report(partial)["evidence_status"] == STATUS_INSUFFICIENT_COMPARABLE


def test_many_views_on_one_post_do_not_replace_the_sample_count() -> None:
    report = _report([_pub(1, views=5_000_000, likes=90_000), _pub(2, views=4_000_000)])
    assert report["evidence_status"] == STATUS_INSUFFICIENT_SAMPLE
    assert report["overall"]["views"]["median"] is None  # 母数不足なら中央値も出さない
    assert report["overall"]["views"]["total"] == 9_000_000  # 生の件数は見せる


def test_an_immature_viral_post_does_not_move_mature_conclusions() -> None:
    mature = [_rated(i, 0.10) for i in (1, 2, 3)] + [
        _rated(i, 0.05, angle="comparison") for i in (4, 5, 6)
    ]
    before = _report(mature)
    viral = _pub(
        7,
        angle="comparison",
        published=_NOW - timedelta(hours=10),
        snapshots=(_snap(7, _NOW - timedelta(hours=10), 9, views=2_000_000, likes=500_000),),
    )
    after = _report([*mature, viral])
    assert after["findings"] == before["findings"]
    assert after["counts"]["immature"] == 1
    assert (
        _value(after, "angle", "comparison")["interaction_rate"]
        == (_value(before, "angle", "comparison")["interaction_rate"])
    )
    viral_row = next(p for p in after["publications"] if p["publication_id"] == 7)
    assert viral_row["metrics"] is None  # 若い投稿の数字は学習に出さない


def test_one_huge_outlier_is_flagged_and_does_not_dominate() -> None:
    pubs = [_pub(1, views=40), _pub(2, views=50), _pub(3, views=60), _pub(4, views=100_000)]
    overall = _report(pubs)["overall"]
    assert overall["views"]["median"] == 55
    assert overall["outliers"] == [4]
    assert any("median" in reason for reason in overall["reasons"])


def test_no_finding_while_one_side_is_insufficient() -> None:
    pubs = [_rated(i, 0.10) for i in (1, 2, 3)] + [
        _rated(i, 0.01, angle="comparison") for i in (4, 5)
    ]
    report = _report(pubs)
    assert report["findings"] == []
    assert report["dimensions"]["angle"]["comparable"] is False


# == findings ===================================================================
def test_a_robust_difference_is_described_with_its_evidence() -> None:
    pubs = [_rated(i, 0.10) for i in (1, 2, 3)] + [
        _rated(i, 0.05, angle="comparison") for i in (4, 5, 6)
    ]
    report = _report(pubs)
    finding = next(f for f in report["findings"] if f["dimension"] == "angle")
    assert finding["kind"] == FINDING_OBSERVED_DIFFERENCE
    assert (finding["higher"], finding["lower"]) == ("insight", "comparison")
    assert finding["relative_difference"] == 0.5
    assert [s["posts"] for s in finding["values"]] == [3, 3]
    assert "higher median interaction rate" in finding["statement"]
    assert "current mature sample" in finding["statement"]
    assert any("correlation" in u for u in finding["uncertainty"])
    text = json.dumps(report).lower()
    for word in _FORBIDDEN:
        assert word not in text, word


def test_a_difference_carried_by_one_post_is_not_a_finding() -> None:
    pubs = [_rated(1, 0.05), _rated(2, 0.05), _rated(3, 0.01)] + [
        _rated(i, 0.03, angle="comparison") for i in (4, 5, 6)
    ]
    finding = next(f for f in _report(pubs)["findings"] if f["dimension"] == "angle")
    assert finding["leave_one_out_consistent"] is False
    assert finding["kind"] == FINDING_NO_CLEAR_DIFFERENCE
    assert finding["higher"] is None


def test_a_small_gap_is_no_clear_difference() -> None:
    pubs = [_rated(i, 0.050) for i in (1, 2, 3)] + [
        _rated(i, 0.045, angle="comparison") for i in (4, 5, 6)
    ]
    finding = next(f for f in _report(pubs)["findings"] if f["dimension"] == "angle")
    assert finding["kind"] == FINDING_NO_CLEAR_DIFFERENCE


def test_the_trigger_is_never_compared() -> None:
    pubs = [_rated(i, 0.10, trigger="automatic") for i in (1, 2, 3)] + [
        _rated(i, 0.01, trigger="manual") for i in (4, 5, 6)
    ]
    report = _report(pubs)
    assert report["dimensions"]["trigger"]["diagnostic_only"] is True
    assert report["dimensions"]["trigger"]["comparable"] is False
    assert not [f for f in report["findings"] if f["dimension"] == "trigger"]


# == stability ==================================================================
def _later(pid, rate, hours_after, angle):
    return _rated(pid, rate, angle=angle, published=_T0 + timedelta(hours=hours_after))


def test_an_established_finding_weakens_before_it_is_withdrawn() -> None:
    first = [_later(i, 0.10, i, "insight") for i in (1, 2, 3)] + [
        _later(i, 0.05, i, "comparison") for i in (4, 5, 6)
    ]
    narrowing = [_later(10 + i, 0.085, 200 + i, "comparison") for i in range(3)] + [
        _later(20 + i, 0.088, 400 + i, "comparison") for i in range(4)
    ]
    finding = next(f for f in _report(first + narrowing)["findings"] if f["dimension"] == "angle")
    # 中央値の差は 0.15: 立てる閾値 (0.2) には届かないが、保つ閾値 (0.1) は上回る。
    assert finding["relative_difference"] == 0.15
    assert finding["kind"] == FINDING_OBSERVED_DIFFERENCE
    assert finding["weakening"] is True
    assert finding["since"] == (_T0 + timedelta(hours=6 + 73)).isoformat()

    # 同じ証拠が最初から全部そろっていたなら、所見は立たない (ヒステリシスの確認)。
    together = [
        _rated(p.publication_id, rate, angle=p.angle, published=_T0 + timedelta(hours=1))
        for p, rate in zip(
            first + narrowing,
            [0.10] * 3 + [0.05] * 3 + [0.085] * 3 + [0.088] * 4,
            strict=True,
        )
    ]
    fresh = next(f for f in _report(together)["findings"] if f["dimension"] == "angle")
    assert fresh["kind"] == FINDING_NO_CLEAR_DIFFERENCE


def test_a_finding_is_withdrawn_when_the_gap_closes() -> None:
    first = [_later(i, 0.10, i, "insight") for i in (1, 2, 3)] + [
        _later(i, 0.05, i, "comparison") for i in (4, 5, 6)
    ]
    closing = [_later(10 + i, 0.097, 200 + i, "comparison") for i in range(8)]
    early = _report(first, as_of=_T0 + timedelta(hours=150))
    late = _report(first + closing)
    assert next(f for f in early["findings"] if f["dimension"] == "angle")["kind"] == (
        FINDING_OBSERVED_DIFFERENCE
    )
    withdrawn = next(f for f in late["findings"] if f["dimension"] == "angle")
    assert withdrawn["kind"] == FINDING_NO_CLEAR_DIFFERENCE
    changes = compare_reports(late, early)
    assert changes["material"] is True
    change = next(c for c in changes["finding_changes"] if c["id"] == "angle:insight|comparison")
    assert change["change"] == "changed"
    assert change["from"] == {"kind": FINDING_OBSERVED_DIFFERENCE, "higher": "insight"}
    assert change["to"] == {"kind": FINDING_NO_CLEAR_DIFFERENCE, "higher": None}


def test_no_new_mature_evidence_means_no_material_change() -> None:
    pubs = [_rated(i, 0.10) for i in (1, 2, 3)] + [
        _rated(i, 0.05, angle="comparison") for i in (4, 5, 6)
    ]
    now = _report(pubs)
    earlier = _report(pubs, as_of=_NOW - timedelta(hours=24))
    changes = compare_reports(now, earlier)
    assert changes["material"] is False
    assert changes["summary"] == "no material change"
    assert changes["finding_changes"] == []


def test_a_new_mature_example_is_reported_as_a_change() -> None:
    pubs = [_rated(1, 0.1), _rated(2, 0.1)]
    before = _report(pubs)
    after = _report([*pubs, _rated(3, 0.1)])
    changes = compare_reports(after, before)
    assert changes["new_examples"] == [3]
    assert changes["overall_status_change"] == {
        "from": STATUS_INSUFFICIENT_SAMPLE,
        "to": STATUS_SUFFICIENT,
    }
    assert changes["material"] is True


# == determinism ================================================================
def test_the_output_is_deterministic_and_order_independent() -> None:
    pubs = [
        _rated(i, 0.01 * (i % 4 + 1), angle=("insight", "comparison", "question")[i % 3])
        for i in range(1, 16)
    ]
    first = json.dumps(_report(pubs))
    shuffled = list(pubs)
    random.Random(7).shuffle(shuffled)
    assert json.dumps(_report(shuffled)) == first
    assert json.dumps(_report(pubs)) == first


def test_medians_and_rates_are_withheld_until_the_sample_is_sufficient() -> None:
    value = _value(_report([_rated(1, 0.2), _rated(2, 0.2)]), "angle", "insight")
    assert value["views"]["median"] is None
    assert value["interaction_rate"]["median"] is None
    assert value["views"]["total"] == 2000
    assert value["metric_totals"]["likes"] == {"observed": 2, "total": 400}


def test_every_hour_belongs_to_exactly_one_daypart() -> None:
    for hour in range(24):
        owners = [
            part["name"]
            for part in _POLICY.dayparts
            if (
                part["start_hour"] <= hour < part["end_hour"]
                if part["start_hour"] < part["end_hour"]
                else hour >= part["start_hour"] or hour < part["end_hour"]
            )
        ]
        assert len(owners) == 1, (hour, owners)
