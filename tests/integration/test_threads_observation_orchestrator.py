"""T6.5B.2: 計画の実行 (偽のページだけ) と、投稿者の一覧 (手元の DB だけ)。本番では実行しない。"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ThreadsExternalObservation, ThreadsExternalPost
from app.models.threads_observer import (
    RUN_LOGIN_REQUIRED,
    RUN_PARTIAL,
    RUN_SUCCEEDED,
    SOURCE_FOR_YOU,
    SOURCE_KNOWN_ACCOUNT,
    SOURCE_SEARCH,
    SOURCE_TOPIC_FOR_YOU,
)
from app.services.threads_author_pool import BOOTSTRAP_POSTS, author_pool
from app.services.threads_observer_service import record_run
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.orchestrator import (
    REPEAT_SNAPSHOT,
    STEP_SKIPPED_AFTER_LOGIN,
    STEP_SKIPPED_CAP,
    STEP_SKIPPED_UNVERIFIED,
    count_unique,
    execute_plan,
)
from app.social.threads.observer.planning import build_daily_plan, load_policy
from tests.support.threads_observer_pages import (
    FakePage,
    card,
    drifted_page,
    login_page,
    page,
    suggestion_url,
    trends_page,
)

DAY = date(2026, 9, 30)
MOMENT = datetime(2026, 9, 30, 13, 0, tzinfo=UTC)


def _cards(prefix: str, n: int, author: str | None = None) -> list[str]:
    return [card(author or prefix, f"{prefix.upper()}{i}", f"{prefix} の投稿 {i}", likes=str(i + 1))
            for i in range(n)]  # fmt: skip


def _pilot_plan():
    authors = [{"author_handle": "known_one", "first_source_type": "search",
                "baseline_sample": 0, "bootstrap_needed": True,
                "first_seen_at": "2026-09-28T00:00:00+00:00"}]  # fmt: skip
    return build_daily_plan(load_policy(), day=DAY, stage=1, authors=authors)


def _pages(plan, **overrides) -> dict:
    queries = list(plan.selected_queries)
    pages = {
        sel.for_you_url(): page(*_cards("fy", 12)),
        sel.search_url(queries[0]): page(*_cards("sa", 6)),
        sel.search_url(queries[1]): page(*_cards("sb", 6)),
        sel.trends_url(): trends_page("トピックA", "トピックB"),
        suggestion_url("トピックA"): page(*_cards("ta", 7)),
        sel.account_url("known_one"): page(*_cards("ko", 9, author="known_one")),
    }
    pages.update(overrides)
    return pages


def _run(plan, pages):
    fake = FakePage(pages)
    return fake, execute_plan(fake, plan, clock=lambda: MOMENT)


def test_the_pilot_plan_runs_within_every_budget() -> None:
    plan = _pilot_plan()
    fake, out = _run(plan, _pages(plan))
    summary = out.summary()
    assert out.result.status == RUN_SUCCEEDED
    by = {(s["source_type"], s["query"]): s for s in summary["steps"]}
    assert by[(SOURCE_FOR_YOU, None)]["accepted"] == 8
    assert all(by[(SOURCE_SEARCH, q)]["accepted"] == 4 for q in plan.selected_queries)
    assert by[(SOURCE_TOPIC_FOR_YOU, None)]["accepted"] == 5
    assert by[(SOURCE_KNOWN_ACCOUNT, "known_one")]["accepted"] == 5
    assert summary["unique_posts"] == 26 <= plan.stage_cap
    assert summary["backfilled"] is False
    # ページもスクロールも上限の中 (無限にスクロールしない)。
    assert out.result.pages_opened <= plan.estimated_max_pages
    assert fake.scrolls <= plan.estimated_max_scrolls


def test_a_post_seen_in_two_sources_counts_once_and_keeps_both_provenances(
    session: Session,
) -> None:
    plan = _pilot_plan()
    shared = card("shared_author", "SHARED1", "二つの出どころで見えた投稿", likes="10")
    q0 = plan.selected_queries[0]
    pages = _pages(plan, **{sel.for_you_url(): page(shared, *_cards("fy", 7)),
                            sel.search_url(q0): page(shared, *_cards("sa", 3))})  # fmt: skip
    _, out = _run(plan, pages)
    assert out.provenance["threads:SHARED1"] == [
        {"step": 1, "source_type": SOURCE_FOR_YOU, "source_query": None},
        {"step": 2, "source_type": SOURCE_SEARCH, "source_query": q0}]  # fmt: skip
    summary = out.summary()
    assert summary["accepted_observations"] == summary["unique_posts"] + 1
    assert summary["posts_with_multiple_sources"] == 1
    step2 = next(s for s in summary["steps"] if s["query"] == q0)
    assert step2["duplicates_of_earlier"] == 1 and step2["new_unique"] == 3
    # 保存の形: 投稿は 1 つ、観測 (出どころ) は 2 つ。上書きしない。
    record_run(session, out.result)
    post = session.scalars(select(ThreadsExternalPost).where(
        ThreadsExternalPost.external_post_key == "threads:SHARED1")).one()  # fmt: skip
    observations = session.scalars(select(ThreadsExternalObservation).where(
        ThreadsExternalObservation.post_id == post.id)).all()  # fmt: skip
    assert sorted((o.source_type, o.source_query) for o in observations) == sorted(
        [(SOURCE_FOR_YOU, None), (SOURCE_SEARCH, q0)])  # fmt: skip


def test_a_short_source_is_not_back_filled() -> None:
    plan = _pilot_plan()
    q1 = plan.selected_queries[1]
    _, out = _run(plan, _pages(plan, **{sel.search_url(q1): page(*_cards("sb", 1))}))
    summary = out.summary()
    step = next(s for s in summary["steps"] if s["query"] == q1)
    assert (step["accepted"], step["shortfall"]) == (1, 3)
    assert summary["unique_posts"] == 23  # 26 - 3: ほかの出どころを増やさない
    other = next(s for s in summary["steps"] if s["source_type"] == SOURCE_FOR_YOU)
    assert other["accepted"] == 8
    assert summary["shortfall_vs_plan"] == 3


def test_login_required_stops_everything_and_keeps_nothing() -> None:
    plan = _pilot_plan()
    q0 = plan.selected_queries[0]
    _, out = _run(plan, _pages(plan, **{sel.search_url(q0): login_page()}))
    assert out.result.status == RUN_LOGIN_REQUIRED
    assert out.result.posts == [] and out.provenance == {}
    later = [s for s in out.steps[2:]]
    assert later and all(s.reason == STEP_SKIPPED_AFTER_LOGIN for s in later)


def test_a_drifted_page_only_loses_its_own_step() -> None:
    plan = _pilot_plan()
    q0 = plan.selected_queries[0]
    _, out = _run(plan, _pages(plan, **{sel.search_url(q0): drifted_page()}))
    assert out.result.status == RUN_PARTIAL
    step = next(s for s in out.steps if s.query == q0)
    assert step.status == "dom_unrecognized" and step.accepted == 0
    assert out.summary()["unique_posts"] == 22  # 26 - 4 (その手順だけ捨てる)


def test_the_stage_cap_bounds_the_total() -> None:
    plan = dataclasses.replace(_pilot_plan(), stage_cap=10)
    _, out = _run(plan, _pages(plan))
    assert len(out.result.posts) <= 10 and out.unique_posts <= 10
    first_search = next(s for s in out.steps if s.source_type == SOURCE_SEARCH)
    assert first_search.accepted == 2  # 残りの 2 だけ
    assert any(s.reason == STEP_SKIPPED_CAP for s in out.steps)


def test_an_unverified_surface_in_a_plan_is_refused(monkeypatch) -> None:
    plan = _pilot_plan()
    original = sel.surface_verification
    monkeypatch.setattr(sel, "surface_verification",
                        lambda s: {"version": None, "verified": False, "verified_fields": ()}
                        if s == SOURCE_KNOWN_ACCOUNT else original(s))  # fmt: skip
    fake, out = _run(plan, _pages(plan))
    step = next(s for s in out.steps if s.source_type == SOURCE_KNOWN_ACCOUNT)
    assert step.status == "skipped" and step.reason == STEP_SKIPPED_UNVERIFIED
    assert sel.account_url("known_one") not in fake.visited


def test_topic_for_you_posts_keep_their_own_topics() -> None:
    plan = _pilot_plan()
    pages = _pages(plan, **{suggestion_url("トピックA"): page(
        card("t1", "TP1", "トピックなし", likes="2"),
        card("t2", "TP2", "トピックあり", topic="インサイト祭り", likes="3"))})  # fmt: skip
    _, out = _run(plan, pages)
    topics = {p.record.external_post_key: p.record.topic for p in out.result.posts
              if p.source_type == SOURCE_TOPIC_FOR_YOU}  # fmt: skip
    assert topics == {"threads:TP1": None, "threads:TP2": "インサイト祭り"}
    assert all(p.source_query == "トピックA" for p in out.result.posts
               if p.source_type == SOURCE_TOPIC_FOR_YOU)  # fmt: skip


def test_repeat_snapshots_never_count_as_unique() -> None:
    from app.social.threads.observer.collector import CollectedPost

    plan = _pilot_plan()
    _, out = _run(plan, _pages(plan))
    record = out.result.posts[0].record
    posts = [*out.result.posts, CollectedPost(REPEAT_SNAPSHOT, None, record)]
    assert count_unique(posts) == out.unique_posts


def test_no_scheduler_side_effects_in_the_observer_code() -> None:
    root = Path(__file__).resolve().parents[2]
    files = [*root.glob("app/social/threads/observer/*.py"),
             root / "scripts" / "plan_threads_observation.py",
             root / "scripts" / "observe_threads.py"]  # fmt: skip
    for path in files:
        text = path.read_text(encoding="utf-8")
        for word in ("schtasks", "Register-ScheduledTask", "New-ScheduledTask", "ScheduledTasks"):
            assert word not in text, (path.name, word)


# -- 投稿者の一覧 -------------------------------------------------------------------------


def test_the_author_pool_tracks_bootstrap_needs(session: Session) -> None:
    plan = _pilot_plan()
    _, out = _run(plan, _pages(plan))
    record_run(session, out.result)
    pool = {a["author_handle"]: a for a in author_pool(session)}
    known = pool["known_one"]
    assert known["posts"] == 5 and known["baseline_sample"] == 5
    assert known["first_source_type"] == SOURCE_KNOWN_ACCOUNT
    assert known["known_account_observed"] is True
    assert known["baseline_ready_for_new_posts"] is True  # 次の新しい投稿には基準が付く
    assert known["bootstrap_needed"] is True  # 持っている投稿のどれにも付くのは 6 件から
    assert BOOTSTRAP_POSTS == 6
    for row in pool.values():
        assert set(row) >= {"first_seen_at", "sources", "observations", "latest_observed_at",
                            "known_account_surface_verified", "bootstrap_needed"}  # fmt: skip
        assert "followers" not in row and "bio" not in row


def test_the_author_pool_is_empty_without_observer_tables(session: Session) -> None:
    from sqlalchemy import text

    for table in ("threads_external_observations", "threads_trending_topics",
                  "threads_external_posts", "threads_observer_runs"):  # fmt: skip
        session.execute(text(f"DROP TABLE {table}"))
    session.commit()
    assert author_pool(session) == []


@pytest.mark.parametrize("stage", [1, 2, 3, 4])
def test_every_stage_plan_is_executable_within_its_cap(stage: int) -> None:
    authors = [{"author_handle": f"au{i}", "first_source_type": "search", "baseline_sample": 0,
                "bootstrap_needed": True, "first_seen_at": f"2026-09-2{i}T00:00:00+00:00"}
               for i in range(3)]  # fmt: skip
    plan = build_daily_plan(load_policy(), day=DAY, stage=stage, authors=authors)
    pages = {sel.for_you_url(): page(*_cards("fy", 30)),
             sel.trends_url(): trends_page("T1", "T2", "T3", "T4"),
             **{sel.search_url(q): page(*_cards(f"s{i}", 8)) for i, q in
                enumerate(plan.selected_queries)},
             **{suggestion_url(t): page(*_cards(f"t{i}", 8)) for i, t in
                enumerate(("T1", "T2", "T3", "T4"))},
             **{sel.account_url(f"au{i}"): page(*_cards(f"a{i}", 12, author=f"au{i}"))
                for i in range(3)}}  # fmt: skip
    _, out = _run(plan, pages)
    assert out.result.status == RUN_SUCCEEDED
    assert out.unique_posts == plan.planned_posts <= plan.stage_cap <= 100
