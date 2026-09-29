"""T6.5B.3: 計画の実行の CLI (``plan_threads_observation.py --execute``)。偽のページと DB だけ。"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, ThreadsExternalObservation, ThreadsExternalPost, ThreadsObserverRun
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.planning import PlanStep, build_daily_plan, load_policy
from scripts import plan_threads_observation as cli
from tests.support.threads_observer_pages import (
    FakePage,
    card,
    drifted_page,
    login_page,
    page,
    suggestion_url,
    trends_page,
)

DAY = date(2026, 9, 29)
NOW = datetime(2026, 9, 29, 1, 0, tzinfo=UTC)
HEAD = "2cfa0ccb2059"


def _cards(prefix: str, n: int) -> list[str]:
    return [card(prefix, f"{prefix.upper()}{i}", f"{prefix} の投稿 {i}", likes=str(i + 1))
            for i in range(n)]  # fmt: skip


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("create table alembic_version (version_num varchar(32))"))
        conn.execute(text(f"insert into alembic_version values ('{HEAD}')"))
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Default").mkdir()
    browsers = tmp_path / "browsers"
    browsers.mkdir()
    return {"factory": sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
            "profile": profile, "browsers": str(browsers), "shots": tmp_path / "shots"}  # fmt: skip


def _pages(**overrides) -> dict:
    plan = build_daily_plan(load_policy(), day=DAY, stage=1, authors=[])
    q0, q1 = plan.selected_queries
    pages = {sel.for_you_url(): page(*_cards("fy", 10)),
             sel.search_url(q0): page(*_cards("sa", 5)),
             sel.search_url(q1): page(*_cards("sb", 5)),
             sel.trends_url(): trends_page("トピックA"),
             suggestion_url("トピックA"): page(*_cards("ta", 6))}  # fmt: skip
    pages.update(overrides)
    return pages


def _main(env, args, *, pages=None, fake=None):
    fake = fake or FakePage(pages if pages is not None else _pages())

    def build(**kwargs):
        build.opened = True
        return fake

    build.opened = False
    code = cli.main(["--stage", "1", "--date", DAY.isoformat(), *args],
                    session_factory=env["factory"], now=NOW, page_factory=build,
                    profile_dir=env["profile"], browsers_path=env["browsers"],
                    expected_revision=HEAD, artifact_root=env["shots"])  # fmt: skip
    return code, build, fake


def _counts(env) -> tuple[int, int, int]:
    with env["factory"]() as session:
        return (len(session.scalars(select(ThreadsObserverRun)).all()),
                len(session.scalars(select(ThreadsExternalPost)).all()),
                len(session.scalars(select(ThreadsExternalObservation)).all()))  # fmt: skip


def test_the_planner_default_opens_no_browser_and_writes_nothing(env, capsys) -> None:
    code, build, _ = _main(env, [])
    assert code == cli.EXIT_OK and build.opened is False
    assert "計画の投稿の数" in capsys.readouterr().out
    assert _counts(env) == (0, 0, 0)


@pytest.mark.parametrize("args", [["--execute"], ["--store"], ["--dry-run"],
                                  ["--execute", "--dry-run", "--store"]])  # fmt: skip
def test_execution_needs_an_explicit_mode(env, args) -> None:
    with pytest.raises(SystemExit):
        _main(env, args)
    assert _counts(env) == (0, 0, 0)


def test_a_dry_run_executes_the_plan_and_writes_nothing(env, capsys) -> None:
    code, build, fake = _main(env, ["--execute", "--dry-run", "--show-posts"])
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK and build.opened is True
    assert out["mode"] == "dry_run" and out["stored"] is False and out["gate"] == "passed"
    assert out["orchestration"]["unique_posts"] == 21  # 8 + 4 + 4 + 5 (知っているアカウントなし)
    assert out["orchestration"]["unique_posts"] <= out["plan"]["stage_cap"]
    assert _counts(env) == (0, 0, 0)


def test_a_stored_run_records_the_plan_and_provenance_once(env, capsys) -> None:
    shared = card("shared", "SHARED1", "二つの出どころの投稿", likes="9")
    plan = build_daily_plan(load_policy(), day=DAY, stage=1, authors=[])
    q0 = plan.selected_queries[0]
    pages = _pages(**{sel.for_you_url(): page(shared, *_cards("fy", 9)),
                      sel.search_url(q0): page(shared, *_cards("sa", 4))})  # fmt: skip
    code, _, _ = _main(env, ["--execute", "--store"], pages=pages)
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK and out["stored"] is True
    runs, posts, observations = _counts(env)
    assert runs == 1
    assert observations == out["orchestration"]["accepted_observations"] == posts + 1
    assert out["provenance_multi"]["threads:SHARED1"][1]["source_query"] == q0
    with env["factory"]() as session:
        run = session.scalars(select(ThreadsObserverRun)).one()
        plan_record = run.artifacts_json["observation_plan"]
        assert plan_record["policy_version"] == "threads-observation-policy-1"
        assert run.artifacts_json["observation_plan"]["stage"] == 1
        assert run.artifacts_json["orchestration"]["unique_posts"] == posts
        assert len(run.artifacts_json["provenance"]["threads:SHARED1"]) == 2
        versions = {s["source_type"]: s["surface_selector_version"]
                    for s in run.artifacts_json["candidate_accounting"]["sources"]}  # fmt: skip
        assert versions["search"] == "threads-search-verified-2026-09-28-v1"


def test_a_short_source_is_accepted_without_back_fill(env, capsys) -> None:
    plan = build_daily_plan(load_policy(), day=DAY, stage=1, authors=[])
    code, _, _ = _main(env, ["--execute", "--store"],
                       pages=_pages(**{sel.search_url(plan.selected_queries[1]):
                                       page(*_cards("sb", 1))}))  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK and out["stored"] is True
    assert out["orchestration"]["unique_posts"] == 18
    assert out["orchestration"]["backfilled"] is False


@pytest.mark.parametrize("bad", ["drift", "login", "accounting"])
def test_a_failed_step_blocks_the_stored_run(env, capsys, bad: str) -> None:
    plan = build_daily_plan(load_policy(), day=DAY, stage=1, authors=[])
    q0 = plan.selected_queries[0]
    hidden = ('<div><a href="/@x/post/HIDDEN1"><time datetime="2026-09-27T01:00:00Z">1日</time>'
              "</a></div>")  # fmt: skip
    broken = {"drift": drifted_page(), "login": login_page(),
              "accounting": page(card("k", "K1", "本文", inner=hidden))}[bad]  # fmt: skip
    code, _, _ = _main(env, ["--execute", "--store"], pages=_pages(**{sel.search_url(q0): broken}))
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_NOT_STORED and out["stored"] is False
    assert _counts(env) == (0, 0, 0)


def test_a_later_stage_cannot_be_stored_yet(env, capsys) -> None:
    code = cli.main(["--stage", "2", "--date", DAY.isoformat(), "--execute", "--store"],
                    session_factory=env["factory"], now=NOW,
                    page_factory=lambda **k: pytest.fail("browser opened"),
                    profile_dir=env["profile"], browsers_path=env["browsers"],
                    expected_revision=HEAD)  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_GATE and any("cannot be stored yet" in p for p in out["problems"])


@pytest.mark.parametrize("change", ["profile", "browsers", "revision", "tables"])
def test_environment_gates_refuse_before_the_browser(env, capsys, change: str) -> None:
    kwargs = {"profile_dir": env["profile"], "browsers_path": env["browsers"],
              "expected_revision": HEAD}  # fmt: skip
    if change == "profile":
        kwargs["profile_dir"] = env["profile"].parent / "missing"
    elif change == "browsers":
        kwargs["browsers_path"] = ""
    elif change == "revision":
        kwargs["expected_revision"] = "somethingelse"
    else:
        with env["factory"]() as session:
            session.execute(text("DROP TABLE threads_external_observations"))
            session.commit()
    code = cli.main(["--stage", "1", "--date", DAY.isoformat(), "--execute", "--store"],
                    session_factory=env["factory"], now=NOW,
                    page_factory=lambda **k: pytest.fail("browser opened"), **kwargs)  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_GATE and out["gate"] == "refused"


def _gate(plan) -> list[str]:
    return cli.gate_problems(plan, load_policy(), store=False, profile_dir=Path("."),
                             browsers_path=".", db_revision=None, expected_revision=None,
                             tables_present=True)  # fmt: skip


def test_custom_feed_and_global_trending_are_refused() -> None:
    plan = build_daily_plan(load_policy(), day=DAY, stage=1, authors=[])
    for source in ("custom_feed", "global_trending"):
        bad = dataclasses.replace(plan, steps=(*plan.steps, PlanStep(source, "x", 1, 1, 3)))
        problems = _gate(bad)
        assert any(source in p for p in problems), source


def test_the_hard_cap_and_surface_gates(monkeypatch) -> None:
    plan = build_daily_plan(load_policy(), day=DAY, stage=1, authors=[])
    over = dataclasses.replace(plan, steps=(*plan.steps, PlanStep("for_you", None, 25, 1, 3)))
    assert any("exceeds cap" in p for p in _gate(over))
    original = sel.surface_verification
    monkeypatch.setattr(sel, "surface_verification",
                        lambda s: {"version": None, "verified": False, "verified_fields": ()}
                        if s == "search" else original(s))  # fmt: skip
    assert any("surface not verified: search" in p for p in _gate(plan))


def test_no_social_mutation_is_reachable_from_the_execution_path() -> None:
    from app.social.threads.observer.driver import PlaywrightPage, ReadOnlyPage

    for cls in (PlaywrightPage, ReadOnlyPage):
        public = {n for n in dir(cls) if not n.startswith("_")}
        for word in ("click", "type", "fill", "press", "like", "reply", "follow", "repost",
                     "post", "publish", "send", "evaluate"):  # fmt: skip
            assert not any(n == word or n.startswith(f"{word}_") for n in public), (cls, word)
    source = Path(cli.__file__).read_text(encoding="utf-8")
    for word in ("schtasks", "Register-ScheduledTask", "openai", "OpenAI"):
        assert word not in source, word
