"""承認依頼のまとめ送りの計画 (T4.2、pure)。

pin する契約:

- 通知窓 08:00-21:00 JST の外では送らない (07:59 不可 / 08:00 可 / 20:59 可 / 21:00 不可)。
- 1 件だけでも 1 通になりうる。3-5 件は 1 通にまとまる。5 件を超えても 1 通は 5 件まで。
- 残りは先送り (消さない)。見送った提案も理由付きで残る。
- 日中は gather / cooldown でまとめる。送信時刻は毎時 :00 に揃わない。
- 夜の間は送らず、朝に改めて評価してから 1 通にまとめる。
- 期限 (TTL) は送信時刻から数える。提案を作った時刻からは数えない。
- 順位付けは名前の付いた規則だけ。乱数を使わない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.social.threads.digest import (
    DEFER_APPROVED_STOCK_SUFFICIENT,
    DEFER_OVER_DIGEST_LIMIT,
    WAIT_COOLDOWN,
    WAIT_GATHERING,
    WAIT_NOTHING_TO_REQUEST,
    WAIT_OUTSIDE_NOTIFICATION_WINDOW,
    DigestCandidate,
    plan_digest,
)
from app.social.threads.policy import get_operations_policy

JST = ZoneInfo("Asia/Tokyo")
POLICY = get_operations_policy()


def _jst(d, h, mi=0, y=2026, mo=9) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=JST).astimezone(UTC)


def _candidate(pid: int, *, ready: datetime, article=None, angle=None, **kw) -> DigestCandidate:
    return DigestCandidate(
        proposal_id=pid,
        source_article_id=article if article is not None else 100 + pid,
        article_title=f"記事{pid}",
        angle=angle or f"angle{pid}",
        ready_at=ready,
        identity=kw.pop("identity", f"text-{pid}"),
        preview=f"本文{pid}",
        **kw,
    )


def _plan(candidates, *, now, last_sent_at=None, queued=None, approved=0):
    return plan_digest(
        list(candidates),
        now=now,
        last_sent_at=last_sent_at,
        queued_identities=set(queued or ()),
        approved_unpublished=approved,
        policy=POLICY,
        tz=JST,
    )


# == notification window ======================================================
@pytest.mark.parametrize(
    ("hour", "minute", "open_"),
    [(7, 59, False), (8, 0, True), (20, 59, True), (21, 0, False), (2, 0, False)],
)
def test_the_notification_window(hour: int, minute: int, open_: bool) -> None:
    now = _jst(25, hour, minute)
    plan = _plan([_candidate(1, ready=now - timedelta(hours=5))], now=now)
    assert plan.window_open is open_
    assert plan.would_send is open_


def test_no_email_at_night_even_when_everything_else_is_ready() -> None:
    now = _jst(25, 23, 30)
    plan = _plan([_candidate(i, ready=now - timedelta(hours=3)) for i in range(1, 6)], now=now)
    assert plan.would_send is False
    assert WAIT_OUTSIDE_NOTIFICATION_WINDOW in plan.waiting_for
    assert plan.due_at == _jst(26, 8, 0)


# == size =====================================================================
def test_a_single_proposal_can_form_a_digest() -> None:
    now = _jst(25, 10, 0)
    plan = _plan([_candidate(1, ready=now - timedelta(hours=2))], now=now)
    assert [i.candidate.proposal_id for i in plan.selected] == [1]
    assert plan.would_send
    assert plan.as_dict(JST)["emails_if_sent"] == 1


def test_three_to_five_proposals_share_one_email() -> None:
    now = _jst(25, 10, 0)
    plan = _plan([_candidate(i, ready=now - timedelta(hours=2)) for i in range(1, 5)], now=now)
    assert len(plan.selected) == 4
    assert plan.as_dict(JST)["emails_if_sent"] == 1


def test_more_than_five_never_becomes_a_burst() -> None:
    """9 件あっても 1 通は 5 件まで。残りは先送りで、消えない。"""

    now = _jst(25, 10, 0)
    plan = _plan([_candidate(i, ready=now - timedelta(hours=2)) for i in range(1, 10)], now=now)
    assert len(plan.selected) == POLICY.digest_max_items == 5
    assert len(plan.deferred) == 4
    assert {i.reason for i in plan.deferred} == {DEFER_OVER_DIGEST_LIMIT}
    assert plan.as_dict(JST)["emails_if_sent"] == 1


# == daytime batching =========================================================
def test_candidates_within_the_gather_window_share_one_digest() -> None:
    """13:05 / 13:22 / 13:41 に依頼の価値が生まれた 3 件は、14:05 の 1 通にまとまる。"""

    d = _candidate(1, ready=_jst(25, 13, 5))
    e = _candidate(2, ready=_jst(25, 13, 22))
    f = _candidate(3, ready=_jst(25, 13, 41))
    last = _jst(25, 10, 0)

    at_1341 = _plan([d, e, f], now=_jst(25, 13, 41), last_sent_at=last)
    assert at_1341.would_send is False
    assert WAIT_GATHERING in at_1341.waiting_for
    assert at_1341.due_at == _jst(25, 14, 5)

    at_1405 = _plan([d, e, f], now=_jst(25, 14, 5), last_sent_at=last)
    assert at_1405.would_send
    assert [i.candidate.proposal_id for i in at_1405.selected] == [1, 2, 3]


def test_batching_is_not_tied_to_the_top_of_the_hour() -> None:
    plan = _plan(
        [_candidate(1, ready=_jst(25, 13, 5))],
        now=_jst(25, 13, 10),
        last_sent_at=_jst(25, 9, 0),
    )
    assert plan.due_at.minute == 5
    assert plan.next_run_at == plan.due_at


def test_the_cooldown_keeps_digests_apart() -> None:
    plan = _plan(
        [_candidate(1, ready=_jst(25, 9, 0))],
        now=_jst(25, 10, 30),
        last_sent_at=_jst(25, 10, 0),
    )
    assert plan.would_send is False
    assert WAIT_COOLDOWN in plan.waiting_for
    assert plan.due_at == _jst(25, 11, 0)


def test_a_full_digest_does_not_wait_for_gather_but_respects_cooldown() -> None:
    now = _jst(25, 13, 10)
    ready = [_candidate(i, ready=_jst(25, 13, i)) for i in range(1, 6)]
    plan = _plan(ready, now=now, last_sent_at=_jst(25, 9, 0))
    assert plan.gather_until is None
    assert plan.would_send

    cooling = _plan(ready, now=now, last_sent_at=_jst(25, 12, 40))
    assert cooling.would_send is False
    assert cooling.due_at == _jst(25, 13, 40)


def test_gather_that_ends_after_21_00_rolls_to_the_next_morning() -> None:
    plan = _plan([_candidate(1, ready=_jst(25, 20, 30))], now=_jst(25, 20, 35))
    assert plan.due_at == _jst(26, 8, 0)


# == overnight ================================================================
def test_overnight_proposals_become_one_morning_digest() -> None:
    overnight = [_candidate(i, ready=_jst(26, 1 + i)) for i in range(1, 4)]
    plan = _plan(overnight, now=_jst(26, 8, 0), last_sent_at=_jst(25, 19, 0))
    assert plan.would_send
    assert len(plan.selected) == 3
    assert plan.as_dict(JST)["emails_if_sent"] == 1


def test_the_morning_reevaluates_before_selecting() -> None:
    """夜のうちに期限が切れた提案は、朝の 1 通に入らない (消えずに理由が残る)。"""

    fresh = _candidate(1, ready=_jst(26, 2))
    lapsed = _candidate(2, ready=_jst(26, 3), expires_at=_jst(26, 7, 0))
    plan = _plan([fresh, lapsed], now=_jst(26, 8, 0))
    assert [i.candidate.proposal_id for i in plan.selected] == [1]
    assert [(i.candidate.proposal_id, i.reason) for i in plan.suppressed] == [(2, "expired")]


# == TTL ======================================================================
def test_ttl_starts_at_the_send_time_not_at_creation() -> None:
    created = _jst(26, 1, 0)
    now = _jst(26, 8, 0)
    plan = _plan([_candidate(1, ready=created)], now=now)
    assert plan.planned_ttl_start == now
    assert plan.planned_expires_at == now + timedelta(hours=24)
    assert plan.planned_expires_at != created + timedelta(hours=24)


def test_a_planned_later_send_starts_the_ttl_later() -> None:
    plan = _plan([_candidate(1, ready=_jst(25, 22))], now=_jst(25, 23, 0))
    assert plan.planned_ttl_start == _jst(26, 8, 0)


# == suppression ==============================================================
def test_every_suppression_keeps_the_proposal_and_explains_why() -> None:
    now = _jst(25, 10, 0)
    ready = now - timedelta(hours=2)
    plan = _plan(
        [
            _candidate(1, ready=ready, held=True),
            _candidate(2, ready=ready, stale_reasons=("the source article changed",)),
            _candidate(3, ready=ready, integrity_reasons=("character count mismatch",)),
            _candidate(4, ready=ready, has_active_request=True),
            _candidate(5, ready=ready, identity="already-approved"),
            _candidate(6, ready=ready),
        ],
        now=now,
        queued={"already-approved"},
    )
    reasons = {i.candidate.proposal_id: i.reason for i in plan.suppressed}
    assert reasons == {
        1: "held",
        2: "stale",
        3: "content_integrity",
        4: "already_requested",
        5: "duplicate",
    }
    assert [i.candidate.proposal_id for i in plan.selected] == [6]


def test_the_older_of_two_identical_proposals_is_kept() -> None:
    now = _jst(25, 10, 0)
    plan = _plan(
        [
            _candidate(2, ready=now - timedelta(hours=1), identity="same"),
            _candidate(1, ready=now - timedelta(hours=3), identity="same"),
        ],
        now=now,
    )
    assert [i.candidate.proposal_id for i in plan.selected] == [1]
    assert [(i.candidate.proposal_id, i.reason) for i in plan.suppressed] == [(2, "duplicate")]


def test_nothing_to_request_waits_quietly() -> None:
    plan = _plan([], now=_jst(25, 10, 0))
    assert plan.would_send is False
    assert plan.waiting_for == (WAIT_NOTHING_TO_REQUEST,)
    assert plan.next_run_at > plan.now


# == stock (advisory) =========================================================
def test_a_full_approved_stock_defers_new_requests_without_deleting() -> None:
    now = _jst(25, 10, 0)
    plan = _plan(
        [_candidate(i, ready=now - timedelta(hours=2)) for i in range(1, 4)],
        now=now,
        approved=15,
    )
    assert plan.selected == ()
    assert {i.reason for i in plan.deferred} == {DEFER_APPROVED_STOCK_SUFFICIENT}
    assert len(plan.deferred) == 3


def test_stock_room_limits_the_digest_size() -> None:
    now = _jst(25, 10, 0)
    plan = _plan(
        [_candidate(i, ready=now - timedelta(hours=2)) for i in range(1, 6)],
        now=now,
        approved=13,
    )
    assert len(plan.selected) == 2
    assert {i.reason for i in plan.deferred} == {DEFER_APPROVED_STOCK_SUFFICIENT}


def test_low_stock_suggests_generating_more() -> None:
    plan = _plan([], now=_jst(25, 10, 0))
    assert plan.stock.below_low
    assert plan.stock.as_dict()["advisory"] is True


# == selection rules ==========================================================
def test_expiring_proposals_are_requested_first() -> None:
    now = _jst(25, 10, 0)
    ready = now - timedelta(hours=5)
    candidates = [_candidate(i, ready=ready + timedelta(minutes=i)) for i in range(1, 7)]
    candidates.append(_candidate(9, ready=ready + timedelta(hours=1), expires_at=_jst(26, 12)))
    plan = _plan(candidates, now=now)
    assert plan.selected[0].candidate.proposal_id == 9


def test_one_digest_spreads_articles_and_angles() -> None:
    now = _jst(25, 10, 0)
    ready = now - timedelta(hours=5)
    plan = _plan(
        [
            _candidate(1, ready=ready, article=21, angle="insight"),
            _candidate(2, ready=ready + timedelta(minutes=1), article=21, angle="insight"),
            _candidate(3, ready=ready + timedelta(minutes=2), article=21, angle="question"),
            _candidate(4, ready=ready + timedelta(minutes=3), article=22, angle="comparison"),
            _candidate(5, ready=ready + timedelta(minutes=4), article=23, angle="beginner_tip"),
            _candidate(6, ready=ready + timedelta(minutes=5), article=24, angle="common_mistake"),
            _candidate(7, ready=ready + timedelta(minutes=6), article=25, angle="insight"),
        ],
        now=now,
    )
    # 1 巡目で記事も切り口も新しい 1, 4, 5, 6。残る 1 枠は最も古い 2 が埋める。
    # 3 (記事 21 が既出) と 7 (切り口 insight が既出) は次の機会に回る。
    assert {i.candidate.proposal_id for i in plan.selected} == {1, 2, 4, 5, 6}
    assert {i.candidate.proposal_id for i in plan.deferred} == {3, 7}
    # 1 通の中は読みやすいよう、元の (期限・作成) 順に並べ直す。
    assert [i.candidate.proposal_id for i in plan.selected] == [1, 2, 4, 5, 6]


def test_the_plan_is_deterministic() -> None:
    now = _jst(25, 10, 0)
    candidates = [_candidate(i, ready=now - timedelta(hours=i)) for i in range(1, 8)]
    first = _plan(candidates, now=now)
    second = _plan(list(reversed(candidates)), now=now)
    assert [i.candidate.proposal_id for i in first.selected] == [
        i.candidate.proposal_id for i in second.selected
    ]


# == manual notification-window override (T4.2 follow-up) =====================
from app.social.threads.digest import (  # noqa: E402
    ManualWindowOverride,
    WindowOverrideError,
)

_OVERRIDE = ManualWindowOverride(reason="T4.2 production approval-digest pilot")


def _plan_override(candidates, *, now, override=_OVERRIDE, **kw):
    return plan_digest(
        list(candidates),
        now=now,
        last_sent_at=kw.get("last_sent_at"),
        queued_identities=set(kw.get("queued", ())),
        approved_unpublished=kw.get("approved", 0),
        policy=POLICY,
        tz=JST,
        window_override=override,
    )


def test_outside_the_window_without_override_nothing_is_sent() -> None:
    now = _jst(25, 23, 30)
    plan = _plan([_candidate(1, ready=now - timedelta(hours=3))], now=now)
    assert plan.would_send is False
    assert plan.manual_override_requested is False


def test_an_explicit_override_allows_sending_outside_the_window() -> None:
    now = _jst(25, 23, 30)
    plan = _plan_override([_candidate(1, ready=now - timedelta(hours=3))], now=now)
    view = plan.as_dict(JST)
    assert view["notification_window_open"] is False
    assert view["manual_override_requested"] is True
    assert view["override_reason"] == "T4.2 production approval-digest pilot"
    assert view["would_send"] is True
    assert plan.window_overridden is True
    # 期限は送信時刻 (23:30) から 24 時間。翌朝 08:00 からではない。
    assert plan.planned_ttl_start == now
    assert plan.planned_expires_at == now + timedelta(hours=24)


def test_an_override_needs_a_reason() -> None:
    for reason in ("", "   ", None):
        with pytest.raises(WindowOverrideError):
            ManualWindowOverride(reason=reason)


def test_only_a_human_cli_source_can_override() -> None:
    with pytest.raises(WindowOverrideError):
        ManualWindowOverride(reason="scheduled", source="resident-worker")


def test_the_planner_rejects_anything_but_a_manual_override() -> None:
    with pytest.raises(WindowOverrideError):
        plan_digest(
            [],
            now=_jst(25, 23, 0),
            last_sent_at=None,
            queued_identities=set(),
            approved_unpublished=0,
            policy=POLICY,
            tz=JST,
            window_override="please",  # type: ignore[arg-type]
        )


def test_the_override_bypasses_only_the_window() -> None:
    """保留・stale・期限切れ・中身の不整合・依頼中・重複はすべてそのまま見送られる。"""

    now = _jst(25, 23, 30)
    ready = now - timedelta(hours=3)
    plan = _plan_override(
        [
            _candidate(1, ready=ready, held=True),
            _candidate(2, ready=ready, stale_reasons=("the source article changed",)),
            _candidate(3, ready=ready, expires_at=now - timedelta(minutes=1)),
            _candidate(4, ready=ready, integrity_reasons=("character count mismatch",)),
            _candidate(5, ready=ready, has_active_request=True),
            _candidate(6, ready=ready, identity="approved-text"),
        ],
        now=now,
        queued={"approved-text"},
    )
    assert plan.selected == ()
    assert plan.would_send is False
    assert {i.reason for i in plan.suppressed} == {
        "held",
        "stale",
        "expired",
        "content_integrity",
        "already_requested",
        "duplicate",
    }


def test_the_override_does_not_bypass_the_cooldown_or_gather() -> None:
    now = _jst(25, 23, 30)
    cooling = _plan_override(
        [_candidate(1, ready=now - timedelta(hours=3))],
        now=now,
        last_sent_at=now - timedelta(minutes=10),
    )
    assert cooling.would_send is False
    assert "cooldown" in cooling.waiting_for

    gathering = _plan_override([_candidate(1, ready=now - timedelta(minutes=5))], now=now)
    assert gathering.would_send is False
    assert "gathering" in gathering.waiting_for


def test_the_override_changes_nothing_inside_the_window() -> None:
    now = _jst(25, 10, 0)
    candidates = [_candidate(1, ready=now - timedelta(hours=3))]
    normal = _plan(candidates, now=now)
    overridden = _plan_override(candidates, now=now)
    assert normal.would_send == overridden.would_send is True
    assert overridden.window_overridden is False
