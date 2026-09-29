"""T6.5B.2: 観察の 1 日の計画 (決まった規則・上限・段階・証拠の目安)。ブラウザも DB も使わない。"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.social.threads import trends
from app.social.threads.observer.planning import (
    POLICY_PATH,
    PolicyError,
    build_daily_plan,
    load_policy,
    rotate_queries,
    select_known_accounts,
    stage_total,
    validate_policy,
)

DAY = date(2026, 9, 30)


@pytest.fixture
def policy() -> dict:
    return load_policy()


def _authors(*specs) -> list[dict]:
    """(名前, 最初の出どころ, 基準の標本の数, 最初に見た時刻の順)"""

    return [{"author_handle": h, "first_source_type": src, "baseline_sample": n,
             "bootstrap_needed": n < trends.MIN_AUTHOR_SAMPLE + 1,
             "first_seen_at": f"2026-09-2{order}T00:00:00+00:00"}
            for h, src, n, order in specs]  # fmt: skip


# -- 方針 -------------------------------------------------------------------------------


def test_the_policy_targets(policy: dict) -> None:
    unique = policy["unique_posts"]
    assert (unique["soft_min"], unique["target"], unique["hard_max"]) == (50, 90, 100)
    normal = policy["allocations"]["3"]
    assert normal["for_you"] == 25
    assert normal["search"]["queries"] * normal["search"]["posts_per_query"] == 20
    assert normal["topic_for_you"]["topics"] * normal["topic_for_you"]["posts_per_topic"] == 15
    assert normal["known_account"]["accounts"] * normal["known_account"]["posts_per_account"] == 30
    assert stage_total(normal) == 90
    assert policy["sources"]["search"]["query_pool"] == [
        "生成AI", "AI自動化", "AI副業", "ブログ", "Threads運用"]  # fmt: skip


def test_every_stage_stays_within_its_cap_and_the_hard_max(policy: dict) -> None:
    caps = {s["stage"]: s["unique_posts"] for s in policy["rollout"]["stages"]}
    totals = {int(k): stage_total(v) for k, v in policy["allocations"].items()}
    assert totals == {1: 26, 2: 50, 3: 90, 4: 100}
    for stage, total in totals.items():
        assert total <= min(caps[stage], policy["unique_posts"]["hard_max"])


def test_the_rollout_ladder(policy: dict) -> None:
    stages = {s["stage"]: s for s in policy["rollout"]["stages"]}
    assert stages[0]["status"] == "complete" and stages[0]["unique_posts"] == 5
    assert stages[1]["status"] == "requires_approval" and 20 <= stages[1]["unique_posts"] <= 30
    assert (stages[2]["unique_posts"], stages[3]["unique_posts"], stages[4]["unique_posts"]) == (
        50, 90, 100)  # fmt: skip
    assert policy["rollout"]["current_stage"] == 0  # 次の段階は人が許可する


def test_a_policy_over_the_hard_max_is_refused(policy: dict) -> None:
    broken = copy.deepcopy(policy)
    broken["allocations"]["4"]["for_you"] = 25
    broken["allocations"]["4"]["known_account"]["posts_per_account"] = 11  # 103 > 100
    with pytest.raises(PolicyError):
        validate_policy(broken)
    repeat = copy.deepcopy(policy)
    repeat["repeat_observation"]["counts_toward_unique"] = True
    with pytest.raises(PolicyError):
        validate_policy(repeat)


def test_evidence_thresholds_match_the_code(policy: dict) -> None:
    thresholds = policy["evidence_thresholds"]
    assert thresholds["external_min_usable_posts"] == trends.EXTERNAL_MIN_USABLE_POSTS == 30
    assert thresholds["group_candidate_pattern_min"] == trends.GROUP_CANDIDATE_MIN == 10
    assert thresholds["group_stronger_descriptive_min"] == trends.GROUP_STRONGER_MIN == 30
    assert thresholds["author_baseline_min_other_posts"] == trends.MIN_AUTHOR_SAMPLE == 5
    assert trends.group_evidence(9) == trends.EVIDENCE_INSUFFICIENT_SAMPLE
    assert trends.group_evidence(10) == trends.EVIDENCE_CANDIDATE_PATTERN
    assert trends.group_evidence(29) == trends.EVIDENCE_CANDIDATE_PATTERN
    assert trends.group_evidence(30) == trends.EVIDENCE_STRONGER_DESCRIPTIVE


# -- 検索の語 -----------------------------------------------------------------------------


def test_search_rotation_is_deterministic_and_moves_daily(policy: dict) -> None:
    pool = policy["sources"]["search"]["query_pool"]
    first = rotate_queries(pool, DAY, 4)
    assert first == rotate_queries(pool, DAY, 4)  # 同じ日 → 同じ語
    assert len(set(first)) == 4
    next_day = rotate_queries(pool, DAY + timedelta(days=1), 4)
    assert next_day != first
    # 5 日で、どの語も同じ回数だけ外れる (毎日同じ語を叩かない)。
    left_out = [set(pool) - set(rotate_queries(pool, DAY + timedelta(days=i), 4))
                for i in range(len(pool))]  # fmt: skip
    assert sorted(q for s in left_out for q in s) == sorted(pool)


def test_the_search_budget_is_split_per_query(policy: dict) -> None:
    plan = build_daily_plan(policy, day=DAY, stage=3, authors=[])
    searches = [s for s in plan.steps if s.source_type == "search"]
    assert [s.query for s in searches] == list(plan.selected_queries)
    assert [s.budget for s in searches] == [5, 5, 5, 5]
    assert plan.query_pool_version == "threads-search-queries-1"


# -- おすすめのトピック・知っているアカウント ----------------------------------------------------


def test_topic_for_you_is_three_topics_by_five_posts(policy: dict) -> None:
    plan = build_daily_plan(policy, day=DAY, stage=3, authors=[])
    [topic] = [s for s in plan.steps if s.source_type == "topic_for_you"]
    assert topic.budget == 15 and topic.max_pages == 4  # 一覧 1 + トピック 3
    assert plan.topic_for_you["max_topics"] == 3 and plan.topic_for_you["max_posts_per_topic"] == 5
    assert plan.topic_for_you["page_topic_copied_to_posts"] is False


def test_known_account_bootstrap_allocation(policy: dict) -> None:
    authors = _authors(("done_author", "search", 7, 1), ("from_search", "search", 1, 3),
                       ("from_topic", "topic_for_you", 0, 2), ("from_for_you", "for_you", 0, 1),
                       ("bizfluxlab", "search", 0, 1), ("extra", "for_you", 2, 4))  # fmt: skip
    plan = build_daily_plan(policy, day=DAY, stage=3, authors=authors)
    known = [s for s in plan.steps if s.source_type == "known_account"]
    # 基準がまだ足りない投稿者が先 → 出どころの近さ (search → topic_for_you → for_you) の順。
    assert [s.query for s in known] == ["from_search", "from_topic", "from_for_you"]
    assert [s.budget for s in known] == [10, 10, 10]
    assert all(a["bootstrap_needed"] for a in plan.known_accounts)
    assert "bizfluxlab" not in [s.query for s in known]  # 自分は選ばない


def test_likes_do_not_decide_known_accounts() -> None:
    a = _authors(("quiet", "search", 0, 1), ("viral", "search", 0, 2))
    a[1]["likes"] = 10_000
    assert [x["author_handle"] for x in select_known_accounts(a, count=1)] == ["quiet"]


def test_fewer_authors_leave_the_budget_unused(policy: dict) -> None:
    plan = build_daily_plan(policy, day=DAY, stage=3, authors=_authors(("only", "search", 0, 1)))
    assert [s.query for s in plan.steps if s.source_type == "known_account"] == ["only"]
    assert plan.planned_posts == 70  # 90 - 20: ほかの出どころに回さない
    assert any("no back-fill" in note for note in plan.notes)


# -- 計画全体 ---------------------------------------------------------------------------


def test_the_normal_plan_is_deterministic_and_within_limits(policy: dict) -> None:
    authors = _authors(("a1", "search", 0, 1), ("a2", "search", 0, 2), ("a3", "for_you", 0, 3))
    one = build_daily_plan(policy, day=DAY, stage=3, authors=authors)
    two = build_daily_plan(policy, day=DAY, stage=3, authors=authors)
    assert one.as_dict() == two.as_dict()
    assert one.planned_posts == 90 <= one.hard_max == 100
    assert one.estimated_max_pages <= policy["page_limits"]["max_pages_per_run"]
    assert all(s.max_scrolls <= policy["page_limits"]["max_scrolls_per_page"] * s.max_pages
               for s in one.steps)  # fmt: skip
    assert all(s.budget <= policy["page_limits"]["max_accepted_per_page"] for s in one.steps)


def test_unavailable_and_unverified_surfaces_are_not_planned(policy: dict) -> None:
    plan = build_daily_plan(policy, day=DAY, stage=3, authors=[])
    sources = {s.source_type for s in plan.steps}
    assert "custom_feed" not in sources and "global_trending" not in sources
    skipped = {s["source_type"] for s in plan.skipped}
    assert {"custom_feed", "global_trending"} <= skipped
    # topic_for_you を global_trending の代わりにしない。
    assert all(s.source_type != "global_trending" for s in plan.steps)


def test_an_unverified_surface_is_skipped(policy: dict, monkeypatch) -> None:
    from app.social.threads.observer import selectors as sel

    original = sel.surface_verification
    monkeypatch.setattr(sel, "surface_verification",
                        lambda s: {"version": None, "verified": False, "verified_fields": ()}
                        if s == "search" else original(s))  # fmt: skip
    plan = build_daily_plan(policy, day=DAY, stage=3, authors=[])
    assert not [s for s in plan.steps if s.source_type == "search"]
    assert {"source_type": "search", "reason": "surface_unverified"} in plan.skipped


def test_the_pilot_stage_is_small(policy: dict) -> None:
    plan = build_daily_plan(policy, day=DAY, stage=1,
                            authors=_authors(("p1", "search", 0, 1)))  # fmt: skip
    assert plan.stage_cap == 30 and plan.planned_posts == 26
    assert [(s.source_type, s.budget) for s in plan.steps] == [
        ("for_you", 8), ("search", 4), ("search", 4), ("topic_for_you", 5),
        ("known_account", 5)]  # fmt: skip


def test_repeat_snapshots_are_not_part_of_the_unique_plan(policy: dict) -> None:
    plan = build_daily_plan(policy, day=DAY, stage=3, authors=[])
    assert plan.repeat_observation["enabled"] is False
    assert plan.repeat_observation["budget"] == 0
    assert plan.repeat_observation["counts_toward_unique"] is False
    assert "repeat_snapshot" not in {s.source_type for s in plan.steps}


# -- 計画の CLI -------------------------------------------------------------------------


def test_the_planner_command_opens_no_browser_and_writes_nothing() -> None:
    root = Path(__file__).resolve().parents[2]
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "scripts/plan_threads_observation.py", "--offline",
                          "--date", "2026-09-30", "--json"], cwd=root, capture_output=True,
                         text=True, encoding="utf-8", env=env, check=True)  # fmt: skip
    plan = json.loads(out.stdout)
    assert plan["planned_posts"] == 60  # --offline: 知っているアカウントの候補なし
    assert plan["estimated_max_accepted_posts"] == 60
    probe = "\n".join([
        "import contextlib, io, runpy, sys",
        "sys.argv = ['plan', '--offline']",
        "with contextlib.redirect_stdout(io.StringIO()):",
        "    try:",
        "        runpy.run_path('scripts/plan_threads_observation.py', run_name='__main__')",
        "    except SystemExit:",
        "        pass",
        "names = [m for m in sys.modules",
        "         if m.startswith('playwright') or m.endswith('observer.driver')]",
        "print(bool(names))",
    ])
    check = subprocess.run([sys.executable, "-c", probe], cwd=root, capture_output=True,
                           text=True, env=env, check=True)  # fmt: skip
    assert check.stdout.strip() == "False"


def test_the_policy_file_is_version_controlled_config_not_env() -> None:
    root = Path(__file__).resolve().parents[2]
    # 作業ディレクトリに依らず、git で管理する app/config の中を指す。
    assert POLICY_PATH.is_absolute()
    assert POLICY_PATH.relative_to(root).as_posix() == "app/config/threads_observation_policy.json"
    env_example = root / ".env.example"
    if env_example.exists():
        assert "OBSERVATION" not in env_example.read_text(encoding="utf-8").upper()
