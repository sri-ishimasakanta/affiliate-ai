"""Growth Action の効果の観測と変換の対応表 (C9 Batch 3、pure)。

pin する契約:

- ``effective_at`` は実際の変化 (適用の成功・公開) の時刻だけ。承認・変換の時刻は使わない。
  まだ変わっていなければ ``None``。失敗した適用は数えない。
- 窓は 24h / 72h / 7d / 14d / 28d。窓が終わっていない・取り込み (GSC の遅れ) が届いていない →
  waiting。前の窓の観測・量が足りない → insufficient。取り込みが古い → stale_data。
- 観測の文は記述だけ (因果・評価の言葉を使わない)。欠測は None のまま。1 つの点数を作らない。
- Threads は T6.5 のチェックポイント (24h / 72h) だけ。
- 変換の対応表: いま実行できるのは内部リンクだけ。ほかは計画だけ・対応なしと明示する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.growth import analysis as ga
from app.growth import conversion as gc
from app.growth import outcome as go


def _effect(status="observed", reasons=(), pre=None, post=None, deltas=None):
    return {"maturity": {"status": status, "reasons": list(reasons)},
            "pre": pre or {"impressions": 40, "clicks": 1, "affiliate_clicks": None,
                           "affiliate_clicks_trusted": True},
            "post": post or {"impressions": 55, "clicks": 2, "affiliate_clicks": None,
                             "affiliate_clicks_trusted": True},
            "deltas": deltas or {"impressions": 15.0, "clicks": 1.0}}  # fmt: skip


# == 実際の変化の時刻 ================================
def test_effective_at_is_the_first_successful_application_only() -> None:
    apps = [{"outcome": "failed", "finished_at": "2026-10-01T00:00:00+00:00"},
            {"outcome": "succeeded", "finished_at": "2026-10-03T02:00:00+00:00"},
            {"outcome": "reconciled", "finished_at": "2026-10-05T02:00:00+00:00"}]
    assert go.effective_at_for("change_request", applications=apps) == (
        "2026-10-03T02:00:00+00:00", "change_applications.finished_at")  # fmt: skip
    assert go.effective_at_for("change_request", applications=apps[:1]) == (None, None)
    assert go.effective_at_for("change_request") == (None, None)  # 承認だけでは None
    published = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)
    assert go.effective_at_for("threads_publication", published_at=published)[0] == (
        published.isoformat())  # fmt: skip
    assert go.effective_at_for("article", published_at=None) == (None, None)


# == 窓の状態 ================================
@pytest.mark.parametrize(("reasons", "state"), [
    (("POST_WINDOW_NOT_ELAPSED",), go.WAITING),
    (("POST_WINDOW_NOT_COVERED",), go.WAITING),  # GSC の遅れ
    (("NO_PRE_DATA",), go.INSUFFICIENT),
    (("BELOW_MINIMUM_VOLUME",), go.INSUFFICIENT),
    (("POST_WINDOW_NOT_COVERED", "NO_PRE_DATA"), go.WAITING),
])  # fmt: skip
def test_effect_maturity_maps_to_outcome_states(reasons, state) -> None:
    cp = go.checkpoint_from_effect("7d", 7, _effect("insufficient_data", reasons))
    assert cp.state == state and cp.reasons == tuple(reasons)
    if state == go.WAITING:
        assert cp.observations == ()


def test_a_completed_window_is_described_without_causal_words() -> None:
    cp = go.checkpoint_from_effect("28d", 28, _effect())
    assert cp.state == go.COMPLETED_WINDOW
    assert "変更後の 28d の窓では impressions=55 を観測" in cp.observations
    assert "変更前の 28d の窓との差は impressions +15" in cp.observations
    assert cp.after["affiliate_clicks"] is None  # 欠測は None のまま
    joined = "".join(cp.observations)
    assert all(word not in joined for word in ("により", "効果", "改善", "成功"))
    with pytest.raises(ValueError, match="descriptive"):
        go.neutral("この変更により増えた")


def test_stale_sources_and_untrusted_clicks() -> None:
    assert go.checkpoint_from_effect("7d", 7, _effect(), source_stale=True).state == go.STALE_DATA
    untrusted = _effect(post={"impressions": 50, "affiliate_clicks": None,
                              "affiliate_clicks_trusted": False})
    cp = go.checkpoint_from_effect("7d", 7, untrusted)
    assert any("信頼できる計測開始より前" in o for o in cp.observations)
    assert go.checkpoint_from_effect("7d", 7, None).state == go.WAITING


def test_threads_checkpoints_reuse_t65() -> None:
    post = {"checkpoints": {"24h": {"reached": True, "comparable": True,
                                    "metrics": {"views": 30}},
                            "72h": {"reached": False, "comparable": False}}}
    assert go.threads_checkpoint("24h", post).state == go.OBSERVABLE
    assert go.threads_checkpoint("72h", post).state == go.WAITING
    assert go.threads_checkpoint("7d", post).state == go.NOT_APPLICABLE
    assert go.threads_checkpoint("24h", None).state == go.WAITING


def test_the_outcome_has_no_score_and_no_cause() -> None:
    anchor = go.MeasurementAnchor(1, ga.REVIEW_INTERNAL_LINKS, "article:1", 1,
                                  "change_request", 5, "applied", "2026-10-01T00:00:00+00:00")
    checkpoints = tuple(go.checkpoint_from_effect(n, d, _effect(
        "insufficient_data", ("POST_WINDOW_NOT_ELAPSED",)) if d > 3 else _effect())
                        for n, d in go.CHECKPOINTS)  # fmt: skip
    outcome = go.GrowthActionOutcome(anchor, checkpoints).as_dict()
    assert [c["name"] for c in outcome["checkpoints"]] == ["24h", "72h", "7d", "14d", "28d"]
    assert outcome["measurement_state"] == go.COMPLETED_WINDOW
    assert outcome["score"] is None and outcome["causal_claim"] == "none"
    assert go.measurement_state(()) == go.WAITING


# == 変換の対応表 ================================
def test_only_internal_links_can_be_executed_now() -> None:
    modes = {a: gc.plan_conversion({"action_type": a, "article_id": 1}).execution_mode
             for a in ga.ACTION_TYPES}  # fmt: skip
    assert [a for a, m in modes.items() if m == gc.EXEC_LOCAL_HANDOFF] == [
        ga.REVIEW_INTERNAL_LINKS]  # fmt: skip
    assert modes[ga.CREATE_NEW_ARTICLE] == gc.EXEC_PLAN_ONLY
    assert modes[ga.CREATE_GROWTH_POST] == gc.EXEC_PLAN_ONLY
    for action in (ga.CREATE_REGULAR_THREADS_POST, ga.CREATE_THREADS_ALTERNATIVE_ANGLE,
                   ga.REVIEW_AFFILIATE_PLACEMENT, ga.UPDATE_EXISTING_ARTICLE,
                   ga.IMPROVE_SEARCH_SNIPPET):
        plan = gc.plan_conversion({"action_type": action, "article_id": 1})
        assert plan.execution_mode == gc.EXEC_UNSUPPORTED and plan.missing
    assert modes[ga.WAIT_FOR_MORE_DATA] == gc.EXEC_NOT_APPLICABLE
