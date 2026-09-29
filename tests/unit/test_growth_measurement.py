"""追跡の観測の規則 (C9-C、pure)。

pin する契約:

- 効果の始まりは、外に見える状態が実際に変わった時刻だけ (適用の成功・Threads の公開・記事の
  公開)。承認・変換・依頼の作成・提案の承認・準備・記事の作成では始まらない。
- 出所がチェックポイントの日まで届いていなければ待つ (遅れの目安の中) か、古い (目安を過ぎた)。
  欠測は 0 にしない。出所が無ければ対象外。
- 次の観測の時刻は、終わっていないチェックポイントの期日 (待っているものは間を空けて)。
- 証拠へ戻す観測は、前後・差の向き・鮮度・足りない理由・流れを持ち、点数・因果を持たない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.growth import measurement as gm
from app.growth import outcome as go

_JST = ZoneInfo("Asia/Tokyo")
_T = "2026-10-01T03:00:00+00:00"


# == 効果の始まり =================================================================================
def test_a_change_takes_effect_only_at_a_successful_application() -> None:
    approved = gm.change_lifecycle(request_state="approved", approval="approved", applications=())
    assert approved.effective_at is None and approved.waiting_for == "a successful application"
    pending = gm.change_lifecycle(request_state="awaiting_approval", approval=None,
                                  applications=())
    assert pending.waiting_for == "approval of the change request"
    failed = gm.change_lifecycle(request_state="apply_failed", approval="approved",
                                 applications=[{"outcome": "failed", "attempted_at": _T}])
    assert failed.effective_at is None
    applied = gm.change_lifecycle(request_state="applied", approval="approved", applications=[
        {"outcome": "failed", "attempted_at": "2026-09-30T00:00:00+00:00"},
        {"outcome": "succeeded", "finished_at": _T}])
    assert (applied.effective_at, applied.effective_event) == (_T, gm.EV_CHANGE_APPLIED)
    assert [s.name for s in applied.stages] == ["change_request", "approval", "application"]


def test_a_threads_request_takes_effect_only_at_publication() -> None:
    assert gm.threads_lifecycle(request_state="pending", proposals=()).waiting_for == (
        "a proposal from the stock maintenance")
    made = gm.threads_lifecycle(request_state="proposal_created",
                                proposals=[{"id": 5, "state": "awaiting_approval"}])
    assert made.effective_at is None and made.waiting_for == "approval of the proposal"
    approved = gm.threads_lifecycle(request_state="proposal_created",
                                    proposals=[{"id": 5, "state": "approved"}])
    assert approved.effective_at is None and approved.waiting_for == "publication"
    published = gm.threads_lifecycle(request_state="proposal_created", proposals=[
        {"id": 5, "state": "approved", "published_at": _T}])
    assert (published.effective_at, published.effective_event) == (_T, gm.EV_THREADS_PUBLISHED)


def test_a_planning_request_takes_effect_only_at_article_publication() -> None:
    assert gm.article_lifecycle(request_state="pending", articles=()).waiting_for == (
        "human approval of the planning request")
    assert gm.article_lifecycle(request_state="approved", articles=()).waiting_for == (
        "an article created in the existing plan flow")
    drafting = gm.article_lifecycle(request_state="materialized",
                                    articles=[{"id": 9, "state": "drafting"}])
    assert drafting.effective_at is None and drafting.waiting_for == "publication of the article"
    live = gm.article_lifecycle(request_state="materialized", articles=[
        {"id": 9, "state": "published", "published_at": _T}])
    assert (live.effective_at, live.effective_event) == (_T, gm.EV_ARTICLE_PUBLISHED)


def test_a_preparation_is_never_treated_as_applied() -> None:
    empty = gm.preparation_lifecycle(request_state="pending", downstream=())
    assert empty.effective_at is None
    revision = gm.preparation_lifecycle(request_state="prepared", downstream=[
        {"type": "editorial_revision", "id": 3, "state": "recorded"}])
    assert revision.effective_at is None and "no WordPress application" in revision.waiting_for
    linked = gm.preparation_lifecycle(request_state="prepared", downstream=[
        {"type": "change_request", "id": 4, "state": "awaiting_approval", "applied_at": None}])
    assert linked.effective_at is None
    applied = gm.preparation_lifecycle(request_state="prepared", downstream=[
        {"type": "change_request", "id": 4, "state": "applied", "applied_at": _T}])
    assert applied.effective_at == _T and applied.stages[0].name == "preparation"


# == 出所の届き方 =================================================================================
def _cov(name, through, freshness="fresh"):
    return gm.SourceCoverage(name, through, None, freshness)


def test_a_checkpoint_waits_until_the_source_covers_the_window() -> None:
    end = date(2026, 10, 8)
    gsc = gm.SRC_SEARCH_CONSOLE
    assert gm.source_window_state(_cov(gsc, date(2026, 10, 10)), window_end=end,
                                  today=end)[0] == go.WAITING  # 窓がまだ終わっていない
    state, why = gm.source_window_state(_cov(gsc, date(2026, 10, 6)), window_end=end,
                                        today=date(2026, 10, 10))
    assert state == go.WAITING and "waiting for search_console data" in why  # GSC の遅れ
    state, why = gm.source_window_state(_cov(gsc, date(2026, 10, 6)), window_end=end,
                                        today=date(2026, 10, 20))
    assert state == go.STALE_DATA and "stale search_console import" in why
    assert gm.source_window_state(_cov(gsc, date(2026, 10, 8)), window_end=end,
                                  today=date(2026, 10, 9))[0] == go.COMPLETED_WINDOW
    assert gm.source_window_state(_cov(gm.SRC_GA4, date(2026, 10, 7), "stale"), window_end=end,
                                  today=date(2026, 10, 9))[0] == go.STALE_DATA
    assert gm.source_window_state(_cov(gm.SRC_GA4, None, "unavailable"), window_end=end,
                                  today=date(2026, 10, 9))[0] == go.NOT_APPLICABLE
    assert gm.source_window_state(None, window_end=end, today=date(2026, 10, 9))[0] == (
        go.NOT_APPLICABLE)


def test_missing_sources_stay_missing_not_zero() -> None:
    metrics = {"impressions": 40, "clicks": 2, "position": 8.0, "ga4_sessions": 0,
               "ga4_organic_sessions": 0, "affiliate_clicks": 0}
    masked = gm.mask_uncovered(metrics, {gm.SRC_SEARCH_CONSOLE: go.COMPLETED_WINDOW,
                                         gm.SRC_GA4: go.WAITING})
    assert masked["impressions"] == 40
    assert masked["ga4_sessions"] is None and masked["affiliate_clicks"] is None


# == 次の観測 =====================================================================================
def test_checkpoint_due_times_follow_the_window_and_threads_hours() -> None:
    applied = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)  # 2026-10-01 12:00 JST
    due = gm.checkpoint_due_at(applied, "7d", tz=_JST)
    # 窓は 10-02〜10-08 (JST)。窓の最後の日が終わる 10-09 0:00 JST。
    assert due == datetime(2026, 10, 9, 0, 0, tzinfo=_JST)
    assert gm.checkpoint_due_at(applied, "24h", threads=True) == applied + timedelta(hours=24)


def test_next_measurement_skips_final_checkpoints_and_rechecks_waiting_ones() -> None:
    now = datetime(2026, 10, 5, tzinfo=UTC)
    future = "2026-10-09T00:00:00+09:00"
    checkpoints = [{"name": "24h", "state": go.COMPLETED_WINDOW, "due_at": "2026-10-03"},
                   {"name": "72h", "state": go.WAITING, "due_at": "2026-10-04T00:00:00+09:00"},
                   {"name": "7d", "state": go.WAITING, "due_at": future}]
    assert gm.next_measurement_at(checkpoints, now=now) == (now + gm.RECHECK).isoformat()
    assert gm.next_measurement_at(checkpoints[:1] + checkpoints[2:], now=now) == (
        datetime.fromisoformat(future).isoformat())
    assert gm.next_measurement_at(checkpoints[:1], now=now) is None  # 全部終わった
    assert gm.is_due("2026-10-04T00:00:00+00:00", now=now)
    assert not gm.is_due(None, now=now)


# == 人向け・証拠へ ================================================================================
def test_the_operator_summary_says_what_is_awaited() -> None:
    waiting = gm.threads_lifecycle(request_state="proposal_created",
                                   proposals=[{"id": 5, "state": "approved"}])
    assert gm.waiting_summary(waiting, []) == ["waiting for publication"]
    live = gm.change_lifecycle(request_state="applied", approval="approved",
                               applications=[{"outcome": "succeeded", "finished_at": _T}])
    lines = gm.waiting_summary(live, [
        {"name": "24h", "state": go.COMPLETED_WINDOW},
        {"name": "72h", "state": go.WAITING, "reasons": [
            "waiting for search_console data (through 2026-10-03; expected lag 3 day(s))"]},
        {"name": "7d", "state": go.WAITING, "reasons": ["the window ends 2026-10-08"]},
        {"name": "14d", "state": go.STALE_DATA, "reasons": ["stale ga4 import: data through x"]},
        {"name": "28d", "state": go.INSUFFICIENT, "reasons": ["BELOW_MINIMUM_VOLUME"]}])
    assert lines[0] == "completed 24h checkpoint"
    assert lines[1].startswith("72h: waiting for search_console data")
    assert lines[2] == "7d: waiting for the 7d checkpoint"
    assert lines[3].startswith("14d: stale ga4 import")
    assert lines[4] == "insufficient data at 28d: BELOW_MINIMUM_VOLUME"


def test_the_followup_item_is_an_observation_without_score_or_cause() -> None:
    live = gm.change_lifecycle(request_state="applied", approval="approved",
                               applications=[{"outcome": "succeeded", "finished_at": _T}])
    item = gm.followup_item(
        {"growth_action_id": 7, "action_type": "review_internal_links", "downstream_type":
         "change_request", "downstream_id": 3, "evidence_version": "rev1:abc"}, live,
        [{"name": "7d", "state": go.COMPLETED_WINDOW, "before": {"impressions": 40},
          "after": {"impressions": 55}, "differences": {"impressions": 15, "clicks": 0,
                                                        "position": -1.0, "ga4_sessions": None}},
         {"name": "14d", "state": go.WAITING, "reasons": ["the window ends x"]}],
        {"search_console": {"state": "fresh", "data_through": "2026-10-12"}})
    assert item["latest_checkpoint"] == "7d" and item["state"] == go.COMPLETED_WINDOW
    assert item["before"] == {"impressions": 40} and item["after"] == {"impressions": 55}
    assert item["direction"] == {"impressions": "higher", "clicks": "unchanged",
                                 "position": "lower", "ga4_sessions": None}
    assert item["causal_claim"] == "none" and item["score"] is None
    assert item["evidence_version"] == "rev1:abc"
    assert [s["name"] for s in item["downstream_context"]] == ["change_request", "approval",
                                                               "application"]
    text = repr(item)
    for word in ("success", "improved", "winner", "because"):
        assert word not in text


def test_threads_checkpoints_are_reached_by_the_t65_row_age() -> None:
    row = {"age_at_as_of_hours": 30.0,
           "checkpoints": {"24h": {"comparable": True, "metrics": {"views": 40}},
                           "72h": {"comparable": False, "reason": "no observation"}}}
    assert go.threads_checkpoint("24h", row).state == go.OBSERVABLE
    assert go.threads_checkpoint("72h", row).state == go.WAITING  # 30 時間: まだ達していない
    old = {**row, "age_at_as_of_hours": 80.0}
    assert go.threads_checkpoint("72h", old).state == go.INSUFFICIENT
