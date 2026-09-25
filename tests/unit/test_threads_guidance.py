"""学習から生成への弱い参考 (T5.5、pure)。

pin する契約:

- 参考は T5 の ``threads-learning/1`` だけから作る (学習を計算し直さない)。
- 証拠が足りなければ中立。今の本番と同じ形 (若い 3 本) からは何も推測しない。
- 強さは weak だけ。スコア・重みは作らない。禁止も強制もしない。
- 控えめ (de-emphasize) は、縮まっていない・1 本に依存しない所見からだけ。
- trigger・hour・source_article は使わない。作れない値の参考は prompt に入れない。
- 同じ証拠からは同じ指紋。as_of より後のデータで指紋は変わらない。
- prompt では事実・文体・多様性・学習を別の節にし、学習がいちばん弱い。
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.social.threads.guidance import (
    DIRECTION_DEEMPHASIZE,
    DIRECTION_PREFER,
    MODE_NEUTRAL,
    MODE_WEAK,
    build_guidance,
    render_prompt_sections,
)
from app.social.threads.learning import PublicationFact, SnapshotFact, analyze
from app.social.threads.policy import get_measurement_policy, get_policy
from app.social.threads.prompt import build_prompt
from app.social.threads.proposal import LINK_MODES
from app.social.threads.style import POST_ANGLES

_TZ = ZoneInfo("Asia/Tokyo")
_MEASUREMENT = get_measurement_policy()
_T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
_NOW = _T0 + timedelta(days=90)
_SUPPORTED = {
    "angle": POST_ANGLES,
    "link_mode": LINK_MODES,
    "length_band": ("short", "medium", "long"),
}
_BANDS = {"short": (1, 150), "medium": (151, 300), "long": (301, 500)}
_CHARS = {"short": 120, "medium": 200, "long": 400}


def _post(pid, rate, *, angle="insight", link="none", band="medium", hours=None, views=1000):
    published = _T0 + timedelta(hours=pid if hours is None else hours)
    snapshot = SnapshotFact(
        snapshot_id=pid * 10,
        observed_at=published + timedelta(hours=73),
        outcome="observed",
        age_hours=73,
        metrics={
            "views": views,
            "likes": round(rate * views),
            "replies": 0,
            "reposts": 0,
            "quotes": 0,
            "shares": 0,
        },
    )
    return PublicationFact(
        publication_id=pid,
        published_at=published,
        angle=angle,
        link_mode=link,
        character_count=_CHARS[band],
        trigger="automatic",
        article_id=21,
        topic="topic",
        snapshots=(snapshot,),
    )


def _guidance(posts, *, as_of=_NOW):
    report = analyze(posts, as_of=as_of, policy=_MEASUREMENT, tz=_TZ)
    return build_guidance(report, supported_values=_SUPPORTED, length_bands=_BANDS)


def _prefs(guidance, dimension):
    return {(p.value, p.direction) for p in guidance.preferences if p.dimension == dimension}


def _medium_beats_long():
    return [_post(i, 0.10, band="medium") for i in (1, 2, 3)] + [
        _post(i, 0.05, band="long") for i in (4, 5, 6)
    ]


# == no evidence ================================================================
def _production_like():
    """本番と同じ形: 若い 3 本 (成熟した例は 0)。"""

    now = _NOW
    return [
        _post(1, 0.0, hours=None, views=146),
        _post(2, 0.0, angle="beginner_tip", views=16),
        _post(3, 0.0, views=0),
    ], now


def test_the_current_production_shape_gives_neutral_guidance() -> None:
    posts = [
        PublicationFact(**{**p.__dict__, "published_at": _NOW - timedelta(hours=h)})
        for p, h in zip(_production_like()[0], (34, 4, 1), strict=True)
    ]
    guidance = _guidance(posts)
    assert guidance.mode == MODE_NEUTRAL
    assert guidance.preferences == ()
    assert guidance.evidence_status == "insufficient_sample"
    assert "No evidence-sufficient Threads learning is available" in guidance.notes[0]
    assert {d for d, _r in guidance.neutral} == {
        "angle",
        "link_mode",
        "length_band",
        "topic",
        "daypart",
        "weekday",
    }
    lines = render_prompt_sections(guidance)
    assert any("十分な証拠のある学習はまだ無い" in line for line in lines)
    assert not any("優先してよい" in line or "控えめ" in line for line in lines)


def test_without_guidance_the_prompt_is_exactly_the_pre_t55_prompt() -> None:
    policy = get_policy()
    kwargs = dict(
        source_article_id=21,
        source_article_title="記事",
        source_article_body="記事本文。",
        source_article_body_hash="h" * 64,
        angles=policy.angles,
        policy=policy,
    )
    plain = build_prompt(**kwargs).rendered_prompt
    neutral = build_prompt(**kwargs, guidance=_guidance([])).rendered_prompt
    sections = "\n".join([*render_prompt_sections(_guidance([])), ""]) + "\n"
    assert neutral.replace(sections, "") == plain
    assert neutral != plain


# == CASE 1: medium beats long ==================================================
def test_case1_a_sufficient_length_finding_is_a_weak_preference() -> None:
    guidance = _guidance(_medium_beats_long())
    assert guidance.mode == MODE_WEAK
    assert _prefs(guidance, "length_band") == {
        ("medium", DIRECTION_PREFER),
        ("long", DIRECTION_DEEMPHASIZE),
    }
    assert all(p.strength == "weak" for p in guidance.preferences)
    medium = next(p for p in guidance.preferences if p.value == "medium")
    evidence = medium.evidence[0]
    assert (evidence["mature_posts"], evidence["other_mature_posts"]) == (3, 3)
    assert evidence["metric"] == "interaction_rate"
    assert evidence["compared_with"] == "long"
    # 証拠の無い次元は中立のまま。
    assert {"angle", "link_mode"} <= {d for d, _r in guidance.neutral}

    text = "\n".join(render_prompt_sections(guidance))
    assert "長さ medium (151〜300 文字) を、ほんの少しだけ優先してよい" in text
    assert "長さ long (301〜500 文字) は、ほんの少しだけ控えめにしてよい (禁止ではない" in text
    assert "どの長さの投稿も引き続き有効" in text
    assert "少なくとも 1 本は含める" in text


# == CASE 2: angle samples are insufficient =====================================
def test_case2_insufficient_angle_samples_stay_neutral() -> None:
    posts = [_post(i, 0.10, angle="insight") for i in (1, 2, 3)] + [
        _post(i, 0.01, angle="beginner_tip") for i in (4, 5)
    ]
    guidance = _guidance(posts)
    assert _prefs(guidance, "angle") == set()
    reason = dict(guidance.neutral)["angle"]
    assert reason.startswith("insufficient evidence")


# == CASE 3: link_mode none beats article =======================================
def test_case3_none_is_weakly_preferred_and_article_stays_allowed() -> None:
    posts = [_post(i, 0.10, link="none") for i in (1, 2, 3)] + [
        _post(i, 0.05, link="article") for i in (4, 5, 6)
    ]
    guidance = _guidance(posts)
    assert _prefs(guidance, "link_mode") == {
        ("none", DIRECTION_PREFER),
        ("article", DIRECTION_DEEMPHASIZE),
    }
    text = "\n".join(render_prompt_sections(guidance))
    assert "link_mode=none を、ほんの少しだけ優先してよい" in text
    assert "link_mode=article は、ほんの少しだけ控えめにしてよい (禁止ではない。書いてよい)" in text
    assert "リンクあり・なしのどちらの投稿も引き続き有効" in text


# == CASE 4 / 5: weakening and reversal =========================================
def _hours(offset, rate, band):
    return _post(offset, rate, band=band, hours=offset)


def _narrowing():
    first = [_hours(i, 0.10, "medium") for i in (1, 2, 3)] + [
        _hours(i, 0.05, "long") for i in (4, 5, 6)
    ]
    narrowing = [_hours(200 + i, 0.085, "long") for i in range(3)] + [
        _hours(400 + i, 0.088, "long") for i in range(4)
    ]
    return first + narrowing


def test_case4_a_weakening_finding_is_tentative_and_never_de_emphasizes() -> None:
    guidance = _guidance(_narrowing())
    medium = next(p for p in guidance.preferences if p.dimension == "length_band")
    assert (medium.value, medium.direction, medium.tentative) == ("medium", "prefer", True)
    assert ("long", DIRECTION_DEEMPHASIZE) not in _prefs(guidance, "length_band")
    assert "(差は縮まりつつある。参考程度に)" in "\n".join(render_prompt_sections(guidance))


def test_case5_reversed_evidence_removes_the_old_preference() -> None:
    reversing = [_hours(700 + i, 0.2, "long") for i in range(12)]
    guidance = _guidance(_narrowing() + reversing)
    assert ("medium", DIRECTION_PREFER) not in _prefs(guidance, "length_band")


# == CASE 6 / 7: as_of and immature data ========================================
def test_case6_future_data_after_as_of_does_not_change_the_fingerprint() -> None:
    posts = _medium_beats_long()
    as_of = _T0 + timedelta(hours=200)
    before = _guidance(posts, as_of=as_of)
    later_snapshots = [
        PublicationFact(
            **{
                **p.__dict__,
                "snapshots": (
                    *p.snapshots,
                    SnapshotFact(
                        snapshot_id=p.publication_id * 10 + 1,
                        observed_at=as_of + timedelta(hours=1),
                        outcome="observed",
                        metrics={
                            "views": 10**6,
                            "likes": 0,
                            "replies": 0,
                            "reposts": 0,
                            "quotes": 0,
                            "shares": 0,
                        },
                    ),
                ),
            }
        )
        for p in posts
    ]
    future_posts = [_post(90 + i, 0.9, band="long", hours=250 + i) for i in range(6)]
    after = _guidance(later_snapshots + future_posts, as_of=as_of)
    assert after.fingerprint == before.fingerprint
    assert json.dumps(after.as_dict()) == json.dumps(before.as_dict())


def test_case7_one_viral_immature_post_changes_nothing() -> None:
    posts = _medium_beats_long()
    viral = _post(99, 0.9, band="long", hours=90 * 24 - 5, views=2_000_000)
    assert _guidance([*posts, viral]).fingerprint == _guidance(posts).fingerprint


def test_the_fingerprint_is_stable_and_independent_of_the_as_of_time() -> None:
    posts = _medium_beats_long()
    one, two = _guidance(posts), _guidance(posts)
    later = _guidance(posts, as_of=_NOW + timedelta(days=3))
    assert one.fingerprint == two.fingerprint == later.fingerprint
    assert one.as_of != later.as_of
    assert len(one.fingerprint) == 64


# == CASE 8: unknown future values ==============================================
def test_case8_an_unknown_link_mode_is_informational_only() -> None:
    posts = [_post(i, 0.10, link="note") for i in (1, 2, 3)] + [
        _post(i, 0.05, link="none") for i in (4, 5, 6)
    ]
    guidance = _guidance(posts)
    link = [p for p in guidance.preferences if p.dimension == "link_mode"]
    assert {(p.value, p.direction) for p in link} == {("note", "prefer"), ("none", "de-emphasize")}
    assert all(p.actionable is False for p in link)
    assert guidance.mode == MODE_NEUTRAL  # prompt には入れない
    text = "\n".join(render_prompt_sections(guidance))
    assert "note" not in text
    assert "十分な証拠のある学習はまだ無い" in text


# == safety =====================================================================
def _report(findings, *, dimensions=None):
    return {
        "schema_version": "threads-learning/1",
        "generated_at": "2026-12-01T00:00:00+00:00",
        "generated_at_local": "2026-12-01T09:00+09:00",
        "policy_version": "t5.0",
        "evidence_status": "sufficient_evidence",
        "dimensions": dimensions or {},
        "findings": findings,
    }


def _finding(dimension, higher, lower, *, weakening=False, consistent=True):
    return {
        "id": f"{dimension}:{higher}|{lower}",
        "dimension": dimension,
        "metric": "interaction_rate",
        "kind": "observed_difference",
        "higher": higher,
        "lower": lower,
        "values": [
            {"value": higher, "posts": 4, "median_interaction_rate": 0.1},
            {"value": lower, "posts": 4, "median_interaction_rate": 0.05},
        ],
        "relative_difference": 0.5,
        "leave_one_out_consistent": consistent,
        "weakening": weakening,
        "since": "2026-11-01T00:00:00+00:00",
    }


@pytest.mark.parametrize("dimension", ["trigger", "hour", "source_article"])
def test_diagnostic_and_confounded_dimensions_never_become_guidance(dimension) -> None:
    guidance = build_guidance(_report([_finding(dimension, "a", "b")]), supported_values=_SUPPORTED)
    assert guidance.preferences == ()
    assert dimension in {d["dimension"] for d in guidance.as_dict()["ignored_dimensions"]}


def test_conflicting_evidence_is_neutral_for_the_conflicted_value() -> None:
    guidance = build_guidance(
        _report(
            [
                _finding("length_band", "medium", "long"),
                _finding("length_band", "short", "medium"),
            ]
        ),
        supported_values=_SUPPORTED,
    )
    assert _prefs(guidance, "length_band") == {
        ("short", DIRECTION_PREFER),
        ("long", DIRECTION_DEEMPHASIZE),
    }


def test_an_unstable_lower_value_is_never_de_emphasized() -> None:
    guidance = build_guidance(
        _report([_finding("angle", "insight", "question", consistent=False)]),
        supported_values=_SUPPORTED,
    )
    assert _prefs(guidance, "angle") == {("insight", DIRECTION_PREFER)}


def test_context_dimensions_are_metadata_and_never_enter_the_prompt() -> None:
    guidance = build_guidance(
        _report([_finding("weekday", "Tue", "Sat"), _finding("daypart", "evening", "night")]),
        supported_values=_SUPPORTED,
    )
    assert {p.dimension for p in guidance.context_preferences} == {"weekday", "daypart"}
    assert guidance.mode == MODE_NEUTRAL
    text = "\n".join(render_prompt_sections(guidance))
    assert "Tue" not in text and "evening" not in text


def test_an_unreadable_learning_report_is_neutral() -> None:
    guidance = build_guidance({"schema_version": "threads-learning/0"}, supported_values={})
    assert guidance.mode == MODE_NEUTRAL
    assert guidance.preferences == ()


def test_guidance_has_no_opaque_score_and_no_strong_strength() -> None:
    payload = _guidance(_medium_beats_long()).as_dict()
    text = json.dumps(payload).lower()
    for word in ("score", "weight", '"strong"', "optimal", "best", "winner"):
        assert word not in text, word


def test_the_provenance_is_small_and_complete() -> None:
    guidance = _guidance(_medium_beats_long())
    provenance = guidance.provenance(verified_against_prompt=True)
    assert provenance == {
        "schema": "threads-generation-guidance/1",
        "applied": True,
        "mode": MODE_WEAK,
        "fingerprint": guidance.fingerprint,
        "as_of": guidance.as_of,
        "learning_schema": "threads-learning/1",
        "learning_policy_version": _MEASUREMENT.policy_version,
        "evidence_status": "sufficient_evidence",
        "preferences": [
            {"dimension": "length_band", "value": "medium", "direction": "prefer"},
            {"dimension": "length_band", "value": "long", "direction": "de-emphasize"},
        ],
        "verified_against_prompt": True,
    }
    assert len(json.dumps(provenance)) < 1000


def test_the_prompt_separates_facts_style_diversity_and_learning() -> None:
    policy = get_policy()
    prompt = build_prompt(
        source_article_id=21,
        source_article_title="記事",
        source_article_body="記事本文。",
        source_article_body_hash="h" * 64,
        angles=policy.angles,
        policy=policy,
        guidance=_guidance(_medium_beats_long()),
    ).rendered_prompt
    order = [
        prompt.index("## 文体"),
        prompt.index("## 事実の扱い"),
        prompt.index("## 多様性"),
        prompt.index("## 学習からの弱い参考"),
        prompt.index("## 出力形式"),
        prompt.index("## 記事"),
    ]
    assert order == sorted(order)
    assert "事実の境界・文体・多様性の規則よりも **弱い**。矛盾したら、この節を無視する" in prompt
    # 内部の ID や生の分析の数字は入れない。
    learning = prompt[prompt.index("## 学習からの弱い参考") : prompt.index("## 出力形式")]
    for leaked in ("0.1", "0.05", "n=", "publication", "finding"):
        assert leaked not in learning, leaked
    assert not re.search(r"#[0-9]", learning)


def test_the_guidance_module_does_not_reach_approval_or_publication() -> None:
    import app.social.threads.guidance as module

    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported <= {
        "__future__",
        "hashlib",
        "json",
        "collections.abc",
        "dataclasses",
        "app.social.threads.learning",
    }
