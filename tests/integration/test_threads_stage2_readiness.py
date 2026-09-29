"""T6.5B.4: 段階 2 の前の監査の強化 (伸びた候補の表示・画面の証拠の門・段階 1 の記録)。

手元の DB (メモリの SQLite) と偽のページだけ。Threads・ブラウザ・OpenAI には触れない。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, ThreadsObserverRun
from app.services.threads_observer_service import record_run
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import CollectionPlan, collect
from app.social.threads.observer.planning import build_daily_plan, load_policy
from app.social.threads.observer.visual_audit import (
    CROSSCHECK_MATCHED,
    CROSSCHECK_NOT_REVIEWED,
    VISUAL_EVIDENCE_MISSING,
    VISUAL_VERIFIED,
    load_reviews,
    review_statuses,
    run_report,
    run_review,
)
from scripts import analyze_threads_trends, audit_threads_observation
from scripts import plan_threads_observation as cli
from tests.support.threads_observer_pages import (
    FakePage,
    card,
    layout_of,
    page,
    suggestion_url,
    trends_page,
)

T0 = datetime(2026, 9, 29, 1, 0, tzinfo=UTC)
DAY = date(2026, 9, 29)
HEAD = "2cfa0ccb2059"
ROOT = Path(__file__).resolve().parents[2]


def _clock():
    moments = iter(T0 + timedelta(seconds=i) for i in range(1000))
    return lambda: next(moments)


def _store_account(session: Session, handle: str, rows: list[tuple[str, str | None, str | None]]):
    """``(code, likes, replies)`` の投稿を、知っているアカウントの 1 回の観察として保存する。"""

    cards = [card(handle, code, f"{handle} の投稿 {code}", likes=likes, replies=replies)
             for code, likes, replies in rows]  # fmt: skip
    fake = FakePage({sel.account_url(handle): page(*cards)})
    result = collect(fake, CollectionPlan(known_accounts=(handle,)), clock=_clock())
    record_run(session, result)


def _report(session: Session) -> dict:
    from app.services.threads_trend_analysis_service import build_report

    return build_report(session, now=T0)


# -- 伸びた候補の表示 ---------------------------------------------------------------------------


def test_a_replies_breakout_is_rendered_as_replies(session: Session) -> None:
    # 2026-09-29 の run 2 と同じ形: 返信は 5 件で基準あり (中央値 1)、いいねは数のある投稿が 3 件。
    _store_account(session, "cha", [("P1", "1808", "16"), ("O1", "5", "2"), ("O2", "1", "1"),
                                    ("O3", None, "1"), ("O4", "2", "1"),
                                    ("O5", None, "1")])  # fmt: skip
    [candidate] = _report(session)["external"]["candidate_breakouts"]
    assert candidate["likes_breakout"] == "insufficient_author_baseline"
    expected = {"metric": "replies", "value": 16, "baseline_median": 1.0, "ratio": 16.0,
                "baseline_n": 5, "min_sample": 5}
    assert candidate["breakouts"] == [expected]
    [line] = analyze_threads_trends.breakout_lines(candidate)
    assert "返信 16" in line and "通常中央値 1.0" in line and "倍率 16.0倍" in line
    assert "比較対象 5投稿" in line
    assert "いいね" not in line and "None" not in line


def test_a_likes_breakout_is_rendered_as_likes(session: Session) -> None:
    _store_account(session, "bob", [("NEW", "180", "1")]
                   + [(f"U{i}", "20", "1") for i in range(7)])  # fmt: skip
    [candidate] = _report(session)["external"]["candidate_breakouts"]
    assert [e["metric"] for e in candidate["breakouts"]] == ["likes"]
    [line] = analyze_threads_trends.breakout_lines(candidate)
    assert "いいね 180 (通常中央値 20.0・倍率 9.0倍・比較対象 7投稿)" in line
    assert "返信" not in line


def test_both_metrics_render_one_line_each(session: Session) -> None:
    _store_account(session, "dan", [("NEW", "200", "30")]
                   + [(f"U{i}", "10", "2") for i in range(6)])  # fmt: skip
    [candidate] = _report(session)["external"]["candidate_breakouts"]
    lines = analyze_threads_trends.breakout_lines(candidate)
    assert [e["metric"] for e in candidate["breakouts"]] == ["likes", "replies"]
    assert len(lines) == 2 and "いいね 200" in lines[0] and "返信 30" in lines[1]


_LINE = re.compile(r"伸びた候補: @(\S+) (いいね|返信) (\d+) \(通常中央値 ([\d.]+)・"
                   r"倍率 ([\d.]+)倍・比較対象 (\d+)投稿\) (\S+)")  # fmt: skip


def test_the_cli_text_matches_the_json_for_every_candidate(session: Session, capsys) -> None:
    _store_account(session, "cha", [("P1", "1808", "16"), ("O1", "5", "2"), ("O2", "1", "1"),
                                    ("O3", None, "1"), ("O4", "2", "1"), ("O5", None, "1")])
    _store_account(session, "dan", [("NEW", "200", "30")]
                   + [(f"U{i}", "10", "2") for i in range(6)])  # fmt: skip
    factory = sessionmaker(bind=session.get_bind())
    analyze_threads_trends.main(["--json"], session_factory=factory, now=T0)
    report = json.loads(capsys.readouterr().out)
    analyze_threads_trends.main([], session_factory=factory, now=T0)
    rendered = capsys.readouterr().out
    labels = {"いいね": "likes", "返信": "replies"}
    from_text = sorted(
        (m.group(7), labels[m.group(2)], int(m.group(3)), float(m.group(4)), float(m.group(5)),
         int(m.group(6)))
        for m in _LINE.finditer(rendered)
    )  # fmt: skip
    from_json = sorted(
        (c["external_post_key"], e["metric"], e["value"], e["baseline_median"], e["ratio"],
         e["baseline_n"])
        for c in report["external"]["candidate_breakouts"] for e in c["breakouts"]
    )  # fmt: skip
    assert from_text == from_json and len(from_json) == 3
    assert "None倍" not in rendered and "None 倍" not in rendered


def test_the_evidence_thresholds_are_unchanged() -> None:
    from app.social.threads import trends

    policy = load_policy()
    assert policy["evidence_thresholds"] == {
        "external_min_usable_posts": 30, "group_candidate_pattern_min": 10,
        "group_stronger_descriptive_min": 30, "author_baseline_min_other_posts": 5}  # fmt: skip
    assert trends.MIN_AUTHOR_SAMPLE == 5


# -- 段階 1 の記録と段階 ---------------------------------------------------------------------------


def test_the_stage1_run2_review_keeps_the_22_of_26_truth() -> None:
    review = run_review(2)
    assert review["stored_posts"] == 26 == len(review["posts"])
    assert review["screenshot_crosschecked"] == 22
    assert review["screenshot_not_painted"] == 4
    assert review["disagreements_among_reviewed"] == 0
    assert review["frame_audit_recorded"] is False
    statuses = review_statuses(review)
    assert sum(1 for s in statuses.values() if s == VISUAL_VERIFIED) == 22
    unseen = {k for k, s in statuses.items() if s == VISUAL_EVIDENCE_MISSING}
    assert len(unseen) == 4
    for key in unseen:  # 見ていない 4 件を「確かめた」にしない
        assert review["posts"][key]["visual_crosscheck"] == CROSSCHECK_NOT_REVIEWED
        assert review["posts"][key]["visual_evidence_available"] is False
    assert all(review["posts"][k]["visual_crosscheck"] == CROSSCHECK_MATCHED
               for k in statuses if k not in unseen)  # fmt: skip
    report = run_report(2, list(review["posts"]), {"screenshots": {}}, review)
    assert report["coverage_pct"] == 84.6 and report["complete"] is False
    assert report["visual_status"] == {"visual_verified": 22, "visual_evidence_available": 0,
                                       "visual_evidence_missing": 4}  # fmt: skip


def test_the_review_record_holds_no_post_text_or_handles() -> None:
    raw = (ROOT / "app" / "config" / "threads_observation_reviews.json").read_text("utf-8")
    data = json.loads(raw)
    for item in data["runs"][0]["posts"].values():
        assert set(item) <= {"source_type", "visual_evidence_available", "visual_crosscheck",
                             "note"}  # fmt: skip
    assert "@" not in raw


def test_rollout_stays_at_stage_zero() -> None:
    policy = load_policy()
    assert policy["rollout"]["current_stage"] == 0
    stages = {s["stage"]: s["status"] for s in policy["rollout"]["stages"]}
    assert stages[2] == "planned"


def test_no_migration_was_added() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    # T6.5B の段階 2 の準備は migration を足していない: 観察の表 (HEAD) より後にあるのは、
    # C9 の Growth Action の表 (Batch 2 の履歴 74bfaf6c9c9f・Batch 3 の変換 74dbecaa4bb2・
    # C9-B の引き渡しの依頼 4fe83827d695・C10-2 の夜の分析 c1d0e233e180) だけ。
    later = [r.revision for r in script.walk_revisions(base=HEAD, head="heads")
             if r.revision != HEAD]
    assert later == ["c1d0e233e180", "4fe83827d695", "74dbecaa4bb2", "74bfaf6c9c9f"]


# -- 保存の門 (段階 2 以上) ----------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("create table alembic_version (version_num varchar(32))"))
        conn.execute(text(f"insert into alembic_version values ('{HEAD}')"))
    profile = tmp_path / "profile"
    (profile / "Default").mkdir(parents=True)
    browsers = tmp_path / "browsers"
    browsers.mkdir()
    # 試験だけの方針の写し: 段階 1 が済んだと仮定 (本物の方針の current_stage は 0 のまま)。
    policy = load_policy()
    policy["rollout"]["current_stage"] = 1
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    return {"factory": sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
            "profile": profile, "browsers": str(browsers), "shots": tmp_path / "shots",
            "policy": policy_path}  # fmt: skip


def _stage2_fake(*, visible: bool) -> FakePage:
    plan = build_daily_plan(load_policy(), day=DAY, stage=2, authors=[])
    pages, layouts = {}, {}

    def add(url: str, prefix: str, n: int) -> None:
        pages[url] = page(*[card(prefix, f"{prefix.upper()}{i}", f"{prefix} {i}", likes="1")
                            for i in range(n)])  # fmt: skip
        # 見えるなら 1 画面に全部 (高さ 100 ずつ)。見えないなら最後の 1 件は画面の外のまま。
        boxes = [(prefix, f"{prefix.upper()}{i}", i * 100, i * 100 + 100) for i in range(n)]
        if not visible:
            boxes[-1] = (prefix, f"{prefix.upper()}{n - 1}", 20_000, 20_100)
        layouts[url] = layout_of(*boxes, height=10_000)

    add(sel.for_you_url(), "fy", 15)
    for i, query in enumerate(plan.selected_queries):
        add(sel.search_url(query), f"s{i}", 4)
    pages[sel.trends_url()] = trends_page("トピックA", "トピックB")
    add(suggestion_url("トピックA"), "ta", 4)
    add(suggestion_url("トピックB"), "tb", 4)
    return FakePage(pages, layouts=layouts)


def _main(env, args, fake):
    def build(**kwargs):
        build.opened = True
        return fake

    build.opened = False
    code = cli.main(["--stage", "2", "--date", DAY.isoformat(), *args],
                    session_factory=env["factory"], now=T0, page_factory=build,
                    profile_dir=env["profile"], browsers_path=env["browsers"],
                    expected_revision=HEAD, artifact_root=env["shots"],
                    policy_path=env["policy"])  # fmt: skip
    return code, build


def _runs(env) -> int:
    with env["factory"]() as session:
        return len(session.scalars(select(ThreadsObserverRun)).all())


def test_stage2_store_without_screenshots_is_refused_before_the_browser(env, capsys) -> None:
    code, build = _main(env, ["--execute", "--store"], _stage2_fake(visible=True))
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_GATE and build.opened is False
    assert any("need --screenshots" in p for p in out["problems"])
    assert _runs(env) == 0


def test_stage2_store_with_missing_visual_evidence_is_not_stored(env, capsys) -> None:
    code, build = _main(env, ["--execute", "--store", "--screenshots"],
                        _stage2_fake(visible=False))  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert build.opened is True and code == cli.EXIT_NOT_STORED and out["stored"] is False
    assert any(r.startswith("visual_audit:-=visual_evidence_missing=") for r in
               out["not_stored_reason"])  # fmt: skip
    assert out["visual_audit"]["complete"] is False and out["visual_audit"]["coverage_pct"] < 100
    assert _runs(env) == 0


def test_stage2_store_with_full_visual_evidence_records_the_audit(env, capsys) -> None:
    code, _ = _main(env, ["--execute", "--store", "--screenshots", "--show-posts"],
                    _stage2_fake(visible=True))  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK and out["stored"] is True
    visual = out["visual_audit"]
    assert visual["complete"] is True and visual["coverage_pct"] == 100.0
    assert visual["accepted_posts"] == out["orchestration"]["unique_posts"] == 35
    assert all(p["visual_evidence_available"] and p["audit_frames"] for p in out["posts"])
    # 画面の file は実行の画面のフォルダからの相対 (手順のフォルダ + 出どころ + frame)。
    frame = out["posts"][0]["audit_frames"][0]
    assert re.fullmatch(r"step\d+/[\w-]+/frame-\d{3}\.png", frame), frame
    assert not Path(frame).is_absolute()
    with env["factory"]() as session:
        run = session.scalars(select(ThreadsObserverRun)).one()
        stored = run.artifacts_json["visual_audit"]
        assert stored["complete"] is True and stored["enabled"] is True
        assert set(stored["posts"]) == {p["external_post_key"] for p in out["posts"]}
        assert all(v["visual_crosscheck"] == CROSSCHECK_NOT_REVIEWED
                   for v in stored["posts"].values())  # 自動の記録は照らしていない
        assert "visual" not in json.dumps(run.artifacts_json["candidate_accounting"])


def test_stage1_store_is_unchanged_by_the_visual_gate(env, capsys) -> None:
    # 段階 1 は画面なしでも保存の門を通る (段階 1 の実行の道は変えない)。
    code, build = _main(env, [], _stage2_fake(visible=True))
    assert code == cli.EXIT_OK and build.opened is False  # 計画だけ
    problems = cli.gate_problems(
        build_daily_plan(load_policy(), day=DAY, stage=1, authors=[]), load_policy(),
        store=True, profile_dir=env["profile"], browsers_path=env["browsers"],
        db_revision=HEAD, expected_revision=HEAD, tables_present=True)  # fmt: skip
    assert not any("screenshots" in p for p in problems)


def test_the_audit_cli_reports_a_stored_run(env, capsys) -> None:
    _main(env, ["--execute", "--store", "--screenshots"], _stage2_fake(visible=True))
    capsys.readouterr()
    code = audit_threads_observation.main(["--run", "1", "--json"],
                                          session_factory=env["factory"],
                                          reviews={"runs": []})  # fmt: skip
    report = json.loads(capsys.readouterr().out)
    assert code == audit_threads_observation.EXIT_OK
    assert report["frame_audit_recorded"] is True and report["coverage_pct"] == 100.0
    assert report["visual_status"]["visual_verified"] == 0  # 照らすまでは verified にしない
    assert report["visual_crosscheck"]["not_reviewed"] == report["accepted_posts"]
    assert audit_threads_observation.main(["--run", "9"], session_factory=env["factory"],
                                          reviews={"runs": []}) == 2  # fmt: skip


def test_the_real_review_file_loads() -> None:
    assert load_reviews()["schema"] == "threads-observation-review-1"
