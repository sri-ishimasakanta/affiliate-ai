"""承認済み投稿案の queue 評価 (T4.1、pure)。

pin する契約:

- **承認は公開ではない。** 承認済みでも、公開の判定は別に行う。
- T4.1 では自動公開は無効。``would_publish_now`` は常に False。
- 1 回の評価で選ぶのは最大 1 件。資格が 5 件あっても次の候補は 1 件。
- T3 の不確定な公開が 1 件でもあれば、queue 全体を止める。
- 理由の語彙は小さく固定。不透明なスコアは無い。
- 何も変わらなければ順序も変わらない。
- 弱い信号 (直前と同じ切り口・記事) は順序にしか効かず、候補から外さない。
  承認から一定時間を過ぎた候補には効かない (永遠に後回しにしない)。
- 若い投稿の指標は順位の根拠にならない。
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.social.threads.policy import get_operations_policy
from app.social.threads.queue import (
    AUTOMATIC_PUBLICATION_ENABLED,
    BLOCKER_AUTOMATIC_PUBLICATION_DISABLED,
    BLOCKER_GAP_NOT_ELAPSED,
    BLOCKER_NO_ELIGIBLE_CANDIDATE,
    BLOCKER_OUTSIDE_PUBLICATION_WINDOW,
    BLOCKER_THREADS_DISABLED,
    BLOCKER_THREADS_MISCONFIGURED,
    BLOCKER_UNCERTAIN_PUBLICATION,
    CANDIDATE_REASONS,
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_USABLE,
    GLOBAL_BLOCKERS,
    MAX_PUBLICATIONS_PER_CYCLE,
    REASON_ALREADY_PUBLISHED,
    REASON_CONTENT_INTEGRITY,
    REASON_ELIGIBLE,
    REASON_NOT_APPROVED,
    REASON_STALE,
    REASON_UNCERTAIN_PUBLICATION,
    CandidateFacts,
    QueueEvaluation,
    QueueFacts,
    evaluate_queue,
)

JST = ZoneInfo("Asia/Tokyo")
POLICY = get_operations_policy()
# 2026-09-24 10:00 JST。公開窓の中。
NOW = datetime(2026, 9, 24, 1, 0, tzinfo=UTC)


def _candidate(pid: int, *, angle="insight", article=21, approved_hours_ago=1.0, **kw):
    return CandidateFacts(
        proposal_id=pid,
        status=kw.pop("status", "approved"),
        angle=angle,
        source_article_id=article,
        link_mode="none",
        approved_at=NOW - timedelta(hours=approved_hours_ago),
        **kw,
    )


def _facts(*candidates, **kw) -> QueueFacts:
    return QueueFacts(
        now=kw.pop("now", NOW),
        threads_state=kw.pop("state", "ready"),
        candidates=tuple(candidates),
        **kw,
    )


def _evaluate(facts: QueueFacts) -> QueueEvaluation:
    return evaluate_queue(facts, POLICY, JST)


# == approval != publication ==================================================
def test_automatic_publication_is_disabled_in_code() -> None:
    assert AUTOMATIC_PUBLICATION_ENABLED is False


def test_an_approved_eligible_proposal_is_still_not_published() -> None:
    """承認済み・資格あり・間隔も窓も問題なし。それでも T4.1 は出さない。"""

    evaluation = _evaluate(_facts(_candidate(1)))
    assert evaluation.next_candidate is not None
    assert evaluation.next_candidate.eligible
    assert evaluation.would_publish_now is False
    assert evaluation.blockers == (BLOCKER_AUTOMATIC_PUBLICATION_DISABLED,)


# == one post per cycle ========================================================
def test_one_evaluation_selects_at_most_one_candidate() -> None:
    evaluation = _evaluate(_facts(*[_candidate(i, angle=f"a{i}") for i in range(1, 6)]))
    assert evaluation.eligible_count == 5
    assert evaluation.next_candidate.proposal_id == 1
    assert evaluation.max_publications_per_cycle == MAX_PUBLICATIONS_PER_CYCLE == 1


def test_there_is_no_plural_selection_api() -> None:
    """「資格のあるものを全部」取り出す口を作らない。"""

    names = {f.name for f in fields(QueueEvaluation)}
    assert "next_candidate" in names
    assert not any(n in names for n in ("next_candidates", "selected", "batch", "to_publish"))


def test_after_downtime_the_queue_still_offers_only_one() -> None:
    """一晩止まっていても、朝の評価で選ばれるのは 1 件。取り返しをしない。"""

    evaluation = _evaluate(
        _facts(
            *[_candidate(i, angle=f"a{i}", approved_hours_ago=10 + i) for i in range(1, 8)],
            now=datetime(2026, 9, 24, 22, 0, tzinfo=UTC),  # 09-25 07:00 JST
            last_published_at=datetime(2026, 9, 24, 12, 45, tzinfo=UTC),  # 21:45 JST
        )
    )
    assert evaluation.timing.eligible_now
    assert evaluation.next_candidate is not None
    assert evaluation.max_publications_per_cycle == 1


# == T3 uncertain safety =======================================================
def test_an_uncertain_publication_stops_the_whole_queue() -> None:
    evaluation = _evaluate(_facts(_candidate(1), _candidate(2), uncertain_publication_ids=(7,)))
    assert BLOCKER_UNCERTAIN_PUBLICATION in evaluation.blockers
    assert BLOCKER_UNCERTAIN_PUBLICATION in evaluation.problems
    assert evaluation.would_publish_now is False


def test_a_candidate_whose_own_attempt_is_in_flight_is_never_eligible() -> None:
    evaluation = _evaluate(_facts(_candidate(1, in_flight=True)))
    assert evaluation.candidates[0].reason == REASON_UNCERTAIN_PUBLICATION
    assert evaluation.next_candidate is None


# == candidate reasons =========================================================
def test_reason_vocabulary_is_small_and_fixed() -> None:
    assert set(CANDIDATE_REASONS) == {
        REASON_ELIGIBLE,
        REASON_NOT_APPROVED,
        REASON_ALREADY_PUBLISHED,
        REASON_STALE,
        REASON_CONTENT_INTEGRITY,
        REASON_UNCERTAIN_PUBLICATION,
    }
    assert len(GLOBAL_BLOCKERS) == 7


def test_each_strong_blocker_maps_to_one_reason() -> None:
    evaluation = _evaluate(
        _facts(
            _candidate(1, already_published=True),
            _candidate(2, status="awaiting_approval"),
            _candidate(3, stale_reasons=("the source article changed",)),
            _candidate(4, integrity_reasons=("character count mismatch",)),
        )
    )
    reasons = {c.proposal_id: c.reason for c in evaluation.candidates}
    assert reasons == {
        1: REASON_ALREADY_PUBLISHED,
        2: REASON_NOT_APPROVED,
        3: REASON_STALE,
        4: REASON_CONTENT_INTEGRITY,
    }
    assert BLOCKER_NO_ELIGIBLE_CANDIDATE in evaluation.blockers


def test_no_candidate_carries_a_score() -> None:
    evaluation = _evaluate(_facts(_candidate(1)))
    assert not any("score" in key for key in evaluation.next_candidate.as_dict())


# == configuration =============================================================
def test_disabled_threads_blocks_without_being_a_problem() -> None:
    evaluation = _evaluate(_facts(_candidate(1), state="disabled"))
    assert BLOCKER_THREADS_DISABLED in evaluation.blockers
    assert evaluation.problems == ()


def test_misconfigured_threads_is_a_problem() -> None:
    evaluation = _evaluate(_facts(_candidate(1), state="misconfigured"))
    assert BLOCKER_THREADS_MISCONFIGURED in evaluation.problems


# == timing ====================================================================
def test_gap_blocks_and_sets_the_exact_next_evaluation_time() -> None:
    """09:17 公開 → 次の評価は 11:17 ちょうど。5 分の枠に合わせない。"""

    last = datetime(2026, 9, 24, 0, 17, tzinfo=UTC)  # 09:17 JST
    evaluation = _evaluate(
        _facts(_candidate(1), now=datetime(2026, 9, 24, 0, 40, tzinfo=UTC), last_published_at=last)
    )
    assert BLOCKER_GAP_NOT_ELAPSED in evaluation.blockers
    assert evaluation.next_evaluation_at == datetime(2026, 9, 24, 2, 17, tzinfo=UTC)
    assert evaluation.next_evaluation_at.minute % 5 != 0


def test_night_blocks_and_reevaluates_at_window_open() -> None:
    evaluation = _evaluate(_facts(_candidate(1), now=datetime(2026, 9, 24, 17, 0, tzinfo=UTC)))
    assert BLOCKER_OUTSIDE_PUBLICATION_WINDOW in evaluation.blockers
    assert evaluation.next_evaluation_at == datetime(2026, 9, 24, 22, 0, tzinfo=UTC)  # 07:00


def test_publications_today_never_block() -> None:
    """1 日の本数は判定に入らない (目安は助言)。"""

    evaluation = _evaluate(_facts(_candidate(1), published_today=12))
    assert evaluation.blockers == (BLOCKER_AUTOMATIC_PUBLICATION_DISABLED,)


# == ordering ==================================================================
def test_ordering_is_stable_when_nothing_changes() -> None:
    facts = _facts(
        _candidate(3, angle="comparison", approved_hours_ago=2),
        _candidate(1, angle="insight", approved_hours_ago=5),
        _candidate(2, angle="question", approved_hours_ago=3),
    )
    first = [c.proposal_id for c in _evaluate(facts).candidates]
    second = [c.proposal_id for c in _evaluate(facts).candidates]
    assert first == second == [1, 2, 3]


def test_repeating_the_last_angle_only_reorders() -> None:
    evaluation = _evaluate(
        _facts(
            _candidate(1, angle="insight", approved_hours_ago=5),
            _candidate(2, angle="question", approved_hours_ago=1),
            last_published_angle="insight",
        )
    )
    order = [c.proposal_id for c in evaluation.candidates]
    assert order == [2, 1]
    # 外されてはいない。
    assert all(c.eligible for c in evaluation.candidates)


def test_repeating_the_last_article_is_weighed_before_the_angle() -> None:
    evaluation = _evaluate(
        _facts(
            _candidate(1, angle="question", article=21, approved_hours_ago=5),
            _candidate(2, angle="insight", article=22, approved_hours_ago=1),
            last_published_angle="insight",
            last_published_article_id=21,
        )
    )
    assert [c.proposal_id for c in evaluation.candidates] == [2, 1]


def test_weak_signals_cannot_starve_an_old_evergreen_proposal() -> None:
    """承認から 48 時間を過ぎた候補には、弱い信号は効かない。"""

    old = POLICY.starvation_guard_hours + 1
    evaluation = _evaluate(
        _facts(
            _candidate(1, angle="insight", approved_hours_ago=old),
            _candidate(2, angle="question", approved_hours_ago=1),
            last_published_angle="insight",
        )
    )
    first = evaluation.candidates[0]
    assert first.proposal_id == 1
    assert first.starvation_guard_active
    assert first.repeats_last_angle


def test_immature_metrics_are_not_ranking_evidence() -> None:
    """成熟した投稿が足りないうちは、評価は insufficient_evidence と明示する。"""

    evaluation = _evaluate(_facts(_candidate(1), mature_post_count=0))
    assert evaluation.evidence_state == EVIDENCE_INSUFFICIENT
    assert any("mature" in note for note in evaluation.notes)


def test_mature_evidence_can_be_marked_usable() -> None:
    evaluation = _evaluate(_facts(_candidate(1), mature_post_count=3, minimum_mature_posts=3))
    assert evaluation.evidence_state == EVIDENCE_USABLE


def test_evidence_state_never_changes_the_order_in_t4_1() -> None:
    base = _facts(
        _candidate(1, angle="insight", approved_hours_ago=5),
        _candidate(2, angle="question", approved_hours_ago=1),
    )
    weak = [c.proposal_id for c in _evaluate(replace(base, mature_post_count=0)).candidates]
    strong = [c.proposal_id for c in _evaluate(replace(base, mature_post_count=50)).candidates]
    assert weak == strong
