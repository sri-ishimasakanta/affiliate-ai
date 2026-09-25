"""T7B: 出どころの強さ・食い違いの検出・不変条件・時間で決まる状態・比較・strict の契約。

偽の WordPress・手元の SQLite・偽のスケジューラ・偽の worker のログだけを使う (本番に触れない)。
時刻はすべて固定する (``NOW`` と、各テストが渡す時刻)。

pin する契約:

- 出どころの順 (live_observed > runtime_config > … > prompt_expectation) と、事実の形。
- 食い違い A–D: 自動公開のドキュメント (stale_doc)・中継の README (stale_doc)・DB の revision の
  文 (stale_doc)・ロックの表示 mode=plan (expected_difference)。どれも本番を変えない。
- 公開してはいけないのに worker が公開できる = critical + blocking (strict が失敗する)。
- Make の tracking は article 1・10・11。違えば live_drift。
- 不変条件は hard / expected_state / advisory。advisory は警告にも strict の失敗にもしない。
- 毎日の運用: not_due / due / overdue、新しい成功で partial が置き換わる。週: not_yet_due /
  missing / ok。診断: not_due / due / overdue。待たない (時刻は渡したもの)。
- 次の行動: 時刻の来ていない確認は due=false、在庫の運用の決定は携帯の表示の確認の後だけ、
  解決済みの alert の行動は出さない、C10 は前提付き。
- 比較は時刻を無視する。strict は契約の失敗のときだけ 1。
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.project_state import (
    compare,
    docs_health,
    invariants,
    precedence,
    runtime_records,
    strict,
    timing,
)
from app.project_state.generator import build_report, render_markdown
from app.project_state.roadmap import load_roadmap, verify_phases
from scripts.generate_project_state import main
from tests.unit.test_project_state import (
    NOW,
    REPO,
    SCHEDULER_ROWS,
    WORKER_LOG,
    FakeWP,
    _context,
)

JST_0630 = datetime(2026, 9, 25, 21, 30, tzinfo=UTC)  # 2026-09-26 06:30 JST


def _scheduler(**changes):
    rows = copy.deepcopy(SCHEDULER_ROWS)
    for name, patch in changes.items():
        row = next(r for r in rows if r["name"] == f"affiliate-ai-{name}")
        if patch is None:
            rows.remove(row)
        else:
            row.update(patch)
    return lambda: json.dumps(rows)


def _drift(report, fid):
    return next((f for f in report["drift"] if f["id"] == fid), None)


def _inv(report, iid):
    return next(i for i in report["invariants"]["results"] if i["id"] == iid)


def _action(report, aid):
    return next((a for a in report["next_actions"] if a["id"] == aid), None)


def _sql(db_path, *statements):
    db = sqlite3.connect(db_path)
    for statement in statements:
        db.execute(statement)
    db.commit()
    db.close()


# == precedence / facts ============================================================
def test_the_source_precedence_order_is_fixed() -> None:
    assert precedence.AUTHORITY_LEVELS == (
        "live_observed",
        "runtime_config",
        "runtime_record",
        "committed_manifest",
        "operations_doc",
        "historical_note",
        "prompt_expectation",
    )
    assert precedence.outranks("live_observed", "operations_doc")
    assert precedence.outranks("runtime_config", "runtime_record")
    assert not precedence.outranks("prompt_expectation", "historical_note")
    with pytest.raises(ValueError):
        precedence.finding("x", "a", "f", authoritative_value=1, conflicting_value=2,
                           authoritative_source="s", conflicting_source="t",
                           classification="made_up", severity="low",
                           recommended_resolution="r")  # fmt: skip
    with pytest.raises(ValueError):
        precedence.finding("x", "a", "f", authoritative_value=1, conflicting_value=2,
                           authoritative_source="s", conflicting_source="t",
                           classification="unresolved", severity="urgent",
                           recommended_resolution="r")  # fmt: skip


def test_key_facts_carry_their_provenance(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    facts = build_report(ctx)["facts"]
    for name in ("current_phase", "db_revision", "make_tracked_articles",
                 "threads_automatic_publication", "threads_worker_can_publish",
                 "stock_maintenance_enabled", "approval_relay_deployed"):  # fmt: skip
        assert set(strict.FACT_KEYS) <= set(facts[name]), name
    assert facts["db_revision"]["value"] == ["afc2f36bb3ca"]
    assert facts["db_revision"]["authority"] == "live_observed"
    auto = facts["threads_automatic_publication"]
    assert (auto["value"], auto["authority"]) == (True, "runtime_config")
    worker = facts["threads_worker_can_publish"]
    assert (worker["value"], worker["authority"], worker["confidence"]) == (
        True,
        "runtime_record",
        "high",
    )
    assert facts["make_tracked_articles"]["value"] == [1, 10, 11]
    assert facts["current_phase"]["value"] is None  # T7 の後、進めているフェーズは無い
    assert facts["next_phase"]["value"] == "N0"
    assert facts["last_completed_phase"]["value"] == "T7"


# == disagreements A–D ==============================================================
def test_a_stale_autopublish_doc_is_a_stale_doc_and_the_policy_is_untouched(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    doc = ctx.root / "docs/operations/threads-autopublish.md"
    doc.write_text("# Threads 自動公開 (T4.3: 実装済み・本番では無効)\n", encoding="utf-8")
    policy = ctx.root / "app/config/threads_operations_policy.json"
    before = hashlib.sha256(policy.read_bytes()).hexdigest()
    report = build_report(ctx)
    found = _drift(report, "doc-threads-autopublish-disabled")
    assert found["classification"] == "stale_doc" and found["severity"] == "low"
    assert found["blocking"] is False
    assert found["conflicting_source"] == "docs/operations/threads-autopublish.md"
    assert hashlib.sha256(policy.read_bytes()).hexdigest() == before
    assert report["facts"]["threads_automatic_publication"]["value"] is True
    conflicts = report["facts"]["threads_automatic_publication"]["conflicts"]
    assert any(c["finding"] == "doc-threads-autopublish-disabled" for c in conflicts)
    assert any(d["path"].endswith("threads-autopublish.md")
               for d in report["documentation_health"]["stale_documents"])  # fmt: skip
    assert _action(report, "correct-stale-docs") is not None
    assert strict.failures(report) == []  # 古いドキュメントでは strict は失敗しない


def test_the_corrected_repository_docs_are_not_stale(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    health = report["documentation_health"]
    assert health["stale_documents"] == []
    corrected = {d["path"] for d in health["corrected_documents"]}
    assert {
        "docs/operations/threads-autopublish.md",
        "docs/operations/threads-proposal-stock.md",
        "wordpress/mu-plugins/bizfluxlab-approval-relay.README.md",
    } <= corrected
    # T7C で policy の note の文も直した (値はそのまま)
    assert health["unresolved_mismatches"] == []
    assert _action(report, "review-policy-note-text") is None
    assert _action(report, "correct-stale-docs") is None


def test_the_relay_header_contradicting_its_deployment_record_is_a_stale_doc(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    readme = ctx.root / "wordpress/mu-plugins/bizfluxlab-approval-relay.README.md"
    text = readme.read_text(encoding="utf-8")
    assert "### T6.1 deployment record" in text  # 配備の記録は残っている
    readme.write_text("**Status: NOT DEPLOYED.** C8.8 only.\n\n" + text, encoding="utf-8")
    found = _drift(build_report(ctx), "doc-relay-not-deployed")
    assert found["classification"] == "stale_doc" and not found["blocking"]
    # 配備の記録が無ければ、見出しは食い違いではない
    readme.write_text("**Status: NOT DEPLOYED.** C8.8 only.\n", encoding="utf-8")
    assert _drift(build_report(ctx), "doc-relay-not-deployed") is None


def test_a_db_revision_claim_is_checked_against_the_live_revision(tmp_path) -> None:
    ctx, _, db_path = _context(tmp_path)
    doc = ctx.root / "docs/operations/threads-proposal-stock.md"
    doc.write_text("本番 DB は `33d93394f342` のまま。\n", encoding="utf-8")
    found = _drift(build_report(ctx), "doc-stock-db-revision")
    assert found["classification"] == "stale_doc"
    assert found["authoritative_source"].startswith("alembic_version")
    # DB が本当にその revision なら、文は古くない (代わりに DB が head でない)
    _sql(db_path, "update alembic_version set version_num = '33d93394f342'")
    report = build_report(ctx)
    assert _drift(report, "doc-stock-db-revision") is None
    assert _inv(report, "db-at-code-head")["result"] == "fail"
    assert any(f.startswith("invariant db-at-code-head") for f in strict.failures(report))


def test_the_worker_lock_mode_plan_label_is_an_expected_difference(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    found = _drift(report, "threads-worker-lock-label-mode-plan")
    assert found["classification"] == "expected_difference" and found["severity"] == "info"
    assert "can_publish=True" in found["authoritative_value"]
    worker = report["threads"]["worker"]
    assert worker["lock_pid"] == 4242
    assert worker["runtime_start"]["found"] is True
    assert worker["runtime_start"]["can_publish"] is True
    warning = next(w for w in report["warnings"] if w["id"].endswith("lock-label-mode-plan"))
    assert warning["severity"] == "info" and not warning["blocking"]


def test_a_worker_that_can_publish_against_the_policy_is_critical(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    policy = ctx.root / "app/config/threads_operations_policy.json"
    data = json.loads(policy.read_text(encoding="utf-8"))
    data["automatic_publication"]["enabled"] = False
    policy.write_text(json.dumps(data), encoding="utf-8")
    report = build_report(ctx)
    found = _drift(report, "threads-auto-publish-effective-mismatch")
    assert found["classification"] == "configuration_drift"
    assert (found["severity"], found["blocking"]) == ("critical", True)
    assert _action(report, "resolve-blocking-warnings")["priority"] == "P0"
    assert "blocking drift: threads-auto-publish-effective-mismatch" in strict.failures(report)


def test_a_missing_worker_start_record_is_unresolved_not_invented(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path, worker_log="unrelated line\n")
    report = build_report(ctx)
    assert report["facts"]["threads_worker_can_publish"]["value"] is None
    assert report["facts"]["threads_worker_can_publish"]["confidence"] == "low"
    found = _drift(report, "threads-worker-start-record-missing")
    assert found["classification"] == "unresolved" and not found["blocking"]


def test_the_worker_log_start_line_is_parsed() -> None:
    event = runtime_records.last_started(WORKER_LOG + "noise\n", pid=4242)
    assert event["mode"] == "resident" and event["pid"] == 4242
    assert event["capabilities"] == ["collect_insights", "sync_approvals", "send_approval_digests"]
    assert (event["auto_publish_flag"], event["auto_publish_policy"], event["can_publish"]) == (
        True,
        "enabled",
        True,
    )
    assert runtime_records.last_started(WORKER_LOG, pid=1) is None
    assert runtime_records.lock_pid("threads-worker pid=16200 host=h mode=plan") == 16200


# == monetization / stock ===========================================================
def test_make_tracking_on_articles_1_10_11_and_drift_when_it_differs(tmp_path) -> None:
    ctx, _, db_path = _context(tmp_path)
    report = build_report(ctx)
    assert report["facts"]["make_tracked_articles"]["value"] == [1, 10, 11]
    assert _drift(report, "monetization-make-tracking") is None
    _sql(db_path, "update affiliate_link_targets set status = 'retired' where article_id = 1")
    found = _drift(build_report(ctx), "monetization-make-tracking")
    assert found["classification"] == "live_drift"
    assert found["authoritative_value"] == [10, 11]


def test_stock_maintenance_on_is_configuration_drift_reported_once(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    launcher = ctx.root / "scripts/run_threads_worker_task.cmd"
    launcher.write_text(
        'if /I "%PROFILE%"=="publish" set "FLAGS=--resident --auto-publish '
        '--maintain-proposal-stock"\n',
        encoding="utf-8",
    )
    report = build_report(ctx)
    found = _drift(report, "threads-stock-maintenance-on")
    assert (found["classification"], found["severity"]) == ("configuration_drift", "high")
    ids = [w["id"] for w in report["warnings"]]
    assert "drift-threads-stock-maintenance-on" in ids
    assert "invariant-threads-stock-maintenance-off" not in ids  # 同じことを 2 度言わない
    assert "invariant threads-stock-maintenance-off failed (expected_state, high)" in (
        strict.failures(report)
    )


# == invariants =====================================================================
def test_invariants_are_split_into_hard_expected_and_advisory(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    levels = {i["id"]: i["level"] for i in report["invariants"]["results"]}
    for iid in ("db-at-code-head", "threads-publication-window", "threads-approval-window",
                "threads-soft-min-gap", "threads-one-publication-per-cycle",
                "scheduler-contract-operations-daily", "scheduler-contract-operations-weekly",
                "scheduler-contract-threads-worker"):  # fmt: skip
        assert levels[iid] == "hard", iid
        assert _inv(report, iid)["result"] == "pass", iid
    assert levels["threads-stock-maintenance-off"] == "expected_state"
    assert levels["threads-automatic-publication-on"] == "expected_state"
    target = _inv(report, "threads-daily-activity-target")
    assert target["level"] == "advisory" and target["enforced"] is False
    assert target["result"] == "fail" and target["observed"] == 1  # 目安より少なくても誤りではない
    assert not any(w["id"] == "invariant-threads-daily-activity-target" for w in report["warnings"])
    assert _inv(report, "git-pushed")["result"] == "fail"
    assert strict.failures(report) == []


def test_a_broken_scheduler_contract_is_a_hard_failure(tmp_path) -> None:
    ctx, _, _ = _context(
        tmp_path,
        scheduler=_scheduler(
            **{"operations-weekly": None, "threads-worker": {"multiple_instances": "Parallel"}}
        ),
    )
    report = build_report(ctx)
    weekly = _inv(report, "scheduler-contract-operations-weekly")
    worker = _inv(report, "scheduler-contract-threads-worker")
    assert weekly["result"] == "unknown"  # タスクが読めない (一覧に無い) = 不明
    assert worker["result"] == "fail" and "IgnoreNew" in str(worker["observed"])
    assert report["timing"]["weekly"]["state"] == "not_scheduled"
    assert any("scheduler-contract-threads-worker" in f for f in strict.failures(report))


def test_scheduler_contract_checks_days_and_times() -> None:
    task = {
        "exists": True, "enabled": True, "multiple_instances": "IgnoreNew",
        "action": "D:/x/run_operations_task.cmd", "arguments": "daily",
        "trigger_details": [{"start_time": "06:30", "days_of_week": 127}],
    }  # fmt: skip
    problems = invariants.scheduler_contract_problems("affiliate-ai-operations-daily", task)
    assert problems == ["days of week [127] (want 126)"]
    task["trigger_details"] = [{"start_time": "06:30", "days_of_week": 126}]
    assert invariants.scheduler_contract_problems("affiliate-ai-operations-daily", task) == []
    assert invariants.scheduler_contract_problems("affiliate-ai-operations-daily", None) is None


# == timing =========================================================================
PARTIAL = [{"id": 7, "status": "partial", "effective_date": "2026-09-25",
            "started_at": "2026-09-24T21:30:10+00:00",
            "finished_at": "2026-09-24T21:30:29+00:00"}]  # fmt: skip
DAILY_TASK = {"last_run": "2026-09-25T06:30:00.0000000+09:00",
              "next_run": "2026-09-26T06:30:00.0000000+09:00"}  # fmt: skip


@pytest.mark.parametrize(
    ("now", "follow"),
    [
        (JST_0630 - timedelta(hours=3), "not_due"),  # 03:30 JST: 待たない、まだ時刻でない
        (JST_0630 + timedelta(minutes=44), "not_due"),  # 猶予の中
        (JST_0630 + timedelta(hours=2), "due"),
        (JST_0630 + timedelta(hours=7), "overdue"),
    ],
)
def test_the_daily_follow_up_depends_only_on_the_given_time(now, follow) -> None:
    state = timing.daily_state(PARTIAL, DAILY_TASK, now=now)
    assert state["state"] == "latest_partial" and state["follow_up"] == follow
    assert state["check_after"] == "2026-09-26T07:15:00+09:00"


def test_a_scheduled_run_that_left_no_record_counts_from_its_own_time() -> None:
    task = {"last_run": "2026-09-26T06:30:00.0000000+09:00",
            "next_run": "2026-09-27T06:30:00.0000000+09:00"}  # fmt: skip
    state = timing.daily_state(PARTIAL, task, now=JST_0630 + timedelta(hours=2))
    assert state["follow_up"] == "due"
    assert state["expected_superseding_run"] == "2026-09-26T06:30:00+09:00"


def test_a_newer_success_supersedes_the_partial_run(tmp_path) -> None:
    success = {"id": 8, "status": "succeeded", "effective_date": "2026-09-26",
               "started_at": "2026-09-25T21:30:05+00:00",
               "finished_at": "2026-09-25T21:31:00+00:00"}  # fmt: skip
    state = timing.daily_state([success, *PARTIAL], DAILY_TASK, now=JST_0630 + timedelta(hours=1))
    assert state["state"] == "latest_success" and state["follow_up"] == "none"
    assert state["superseded_non_success_run_ids"] == [7]
    ctx, _, db_path = _context(tmp_path)
    _sql(
        db_path,
        "insert into operations_runs values (8, 'daily', 'succeeded', '2026-09-26', "
        "'2026-09-25 21:30:05', '2026-09-25 21:31:00', 9, 9, 0)",
    )
    report = build_report(ctx)
    assert report["timing"]["daily"]["superseded_non_success_run_ids"] == [7]
    ids = {w["id"] for w in report["warnings"]}
    assert "ops-latest-daily-run-not-clean" not in ids
    assert _action(report, "confirm-next-daily-run") is None
    assert _action(report, "check-daily-run") is None


def test_the_partial_run_stays_an_action_until_superseded(tmp_path) -> None:
    before = JST_0630 - timedelta(hours=3)
    ctx, _, _ = _context(tmp_path, now=before)
    report = build_report(ctx)
    wait = _action(report, "confirm-next-daily-run")
    assert wait["due"] is False and wait["due_at"] == "2026-09-26T07:15:00+09:00"
    assert _action(report, "check-daily-run") is None
    warning = next(w for w in report["warnings"] if w["id"] == "ops-latest-daily-run-not-clean")
    assert warning["severity"] == "low" and "resolved 1" in warning["message"]
    assert "unresolved" not in warning["message"]
    assert _action(report, "investigate-active-operations-alerts") is None  # 解決済みの alert
    # 予定の時刻を過ぎても新しい記録が無ければ、確認の時刻が来る
    ctx.now = JST_0630 + timedelta(hours=2)
    due = build_report(ctx)
    assert _action(due, "check-daily-run")["priority"] == "P1"
    assert not any(w["id"].startswith("scheduler-affiliate-ai-operations-daily")
                   for w in due["warnings"])  # fmt: skip  # 結果 1 は daily の状態で説明済み


def test_weekly_states() -> None:
    task = {"exists": True, "last_run": "1999-11-30T00:00:00.0000000+09:00",
            "last_result": 267011, "next_run": "2026-09-27T07:30:00.0000000+09:00"}  # fmt: skip
    assert timing.weekly_state([], task, now=NOW)["state"] == "not_yet_due"
    ran = {**task, "last_run": "2026-09-27T07:30:00+09:00", "last_result": 0}
    later = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    assert timing.weekly_state([], ran, now=later)["state"] == "missing"
    run = {"id": 9, "status": "succeeded", "started_at": "2026-09-26T22:30:04+00:00"}
    assert timing.weekly_state([run], ran, now=later)["state"] == "ok"
    assert timing.weekly_state([], None, now=NOW)["state"] == "not_scheduled"


@pytest.mark.parametrize(
    ("newly", "age_days", "expected"),
    [([4, 5], 1, "not_due"), ([4, 5, 6], 1, "due"), (list(range(6)), 1, "overdue"),
     ([], 8, "due"), ([4], 8, "overdue")],
)  # fmt: skip
def test_diagnostic_states(newly, age_days, expected) -> None:
    generated = (NOW - timedelta(days=age_days)).isoformat()
    state = timing.diagnostic_state({"newly_past_6h": newly, "generated_at": generated}, now=NOW)
    assert state["state"] == expected


# == next actions ===================================================================
def test_the_stock_routine_decision_waits_for_the_mobile_observation(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    assert report["approvals"]["genuine_mobile_render_observed"] is False
    assert _action(report, "observe-mobile-approval-render") is not None
    assert _action(report, "decide-proposal-stock-routine") is None
    assert _action(report, "rerun-threads-performance-diagnostic") is None  # 診断は not_due
    stock_doc = ctx.root / "docs/operations/threads-proposal-stock.md"
    stock_doc.write_text(
        stock_doc.read_text(encoding="utf-8").replace("未確認", "確認済み"), encoding="utf-8"
    )
    observed = build_report(ctx)
    assert observed["approvals"]["genuine_mobile_render_observed"] is True
    assert _action(observed, "decide-proposal-stock-routine") is not None
    assert _action(observed, "observe-mobile-approval-render") is None


def test_intentional_states_get_no_fix_actions_and_c10_waits(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    text = json.dumps(report["next_actions"])
    for forbidden in ("media 99", "delete", "broaden", "--maintain-proposal-stock ON"):
        assert forbidden not in text
    c10 = _action(report, "c10-after-maturity")
    assert c10["blocking"] and c10["prerequisites"] == ["prepare-n0"]  # T7 は完了
    info = {w["id"] for w in report["warnings"] if w["severity"] == "info"}
    assert {"threads-stock-maintenance-off", "wp-media-99-duplicate",
            "wp-api-user-author-role"} <= info  # fmt: skip
    git = next(w for w in report["warnings"] if w["id"] == "git-ahead-of-remote")
    assert git["severity"] == "low"


# == roadmap / decisions ============================================================
def test_the_roadmap_marks_t7_complete_and_n0_next() -> None:
    roadmap = verify_phases(REPO, load_roadmap(REPO), commit_exists=lambda sha: True)
    assert roadmap["declared_current_phase"] is None and roadmap["active"] == []
    assert {"T7A", "T7B", "T7"} <= set(roadmap["completed"])
    assert (roadmap["last_completed_phase"], roadmap["next_phase"]) == ("T7", "N0")
    assert roadmap["next_phase_prerequisites_unmet"] == []
    kinds = {p["id"]: p["evidence_kind"] for p in roadmap["phases"]}
    for pid in ("N0", "N1", "N2", "N3", "C10"):
        assert kinds[pid] == "declared_only", pid
    status = {p["id"]: p["status"] for p in roadmap["phases"]}
    assert status["N0"] == "planned"  # 次のフェーズ。始めたとは言わない
    assert roadmap["problems"] == []


def test_the_t7b_decisions_are_supported_by_the_docs() -> None:
    from app.project_state.decisions import check_support

    rows = {r["id"]: r for r in check_support(REPO)}
    for did in ("project-state-live-outranks-stale-prose",
                "project-state-docs-never-mutate-production",
                "make-tracking-articles-1-10-11",
                "threads-worker-lock-label-mode-plan"):  # fmt: skip
        assert rows[did]["supported"], rows[did]["problems"]


# == compare / strict / schema ======================================================
def test_compare_ignores_timestamps_and_reports_meaningful_changes(tmp_path) -> None:
    ctx, _, db_path = _context(tmp_path)
    old = build_report(ctx)
    ctx.now = NOW + timedelta(minutes=2)
    same = build_report(ctx)
    assert same["generated_at"] != old["generated_at"]
    assert compare.compare(old, same)["changed"] is False
    assert "No meaningful change" in compare.render(compare.compare(old, same))
    _sql(db_path, "update affiliate_link_targets set status = 'retired' where article_id = 1")
    new = build_report(ctx)
    diff = compare.compare(old, new)
    assert diff["changed"] is True
    assert diff["facts"]["make_tracked_articles"] == {"old": [1, 10, 11], "new": [10, 11]}
    assert "monetization-make-tracking" in diff["drift"]["added"]
    assert "drift-monetization-make-tracking" in diff["warnings"]["added"]


def test_strict_ignores_intentional_and_pending_states(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    assert report["featured_images"]["media_99"]["exists"] is True
    assert report["threads"]["worker"]["stock_maintenance_enabled"] is False
    assert report["git"]["ahead"] == 26
    assert report["approvals"]["genuine_mobile_render_observed"] is False
    assert strict.validate(report) == [] and strict.failures(report) == []


def test_strict_fails_on_schema_problems_and_missing_required_sources(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    broken = copy.deepcopy(report)
    del broken["facts"]
    broken["drift"].append({"id": "bad", "classification": "whatever", "severity": "low"})
    problems = strict.validate(broken)
    assert "missing top-level key facts" in problems
    assert any("bad" in p and "classification" in p for p in problems)
    ctx.database_url = "sqlite:///" + (tmp_path / "missing" / "none.db").as_posix()
    unavailable = build_report(ctx)
    assert "required source unavailable: threads" in strict.failures(unavailable)


def test_the_cli_compare_and_strict_exit_contract(tmp_path, capsys) -> None:
    ctx, _, _ = _context(tmp_path)
    assert main(["--strict", "--no-decision-log"], context=ctx) == 0
    older = ctx.root / "reports/older.json"
    older.write_text(
        (ctx.root / "reports/project_state_latest.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    ctx.now = NOW + timedelta(minutes=2)  # heartbeat はまだ新しい
    assert main(["--no-decision-log", "--compare", str(older)], context=ctx) == 0
    diff = json.loads((ctx.root / "reports/project_state_diff_latest.json").read_text("utf-8"))
    assert diff["changed"] is False
    assert main(["--no-decision-log", "--compare", str(tmp_path / "nope.json")], context=ctx) == 2
    policy = ctx.root / "app/config/threads_operations_policy.json"
    data = json.loads(policy.read_text(encoding="utf-8"))
    data["soft_min_gap_minutes"] = 30
    policy.write_text(json.dumps(data), encoding="utf-8")
    assert main(["--strict", "--no-decision-log"], context=ctx) == 1
    assert "invariant threads-soft-min-gap failed" in capsys.readouterr().out
    assert main(["--no-decision-log"], context=ctx) == 0  # strict でなければ 0


def test_the_markdown_shows_the_t7b_sections(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path, wp=FakeWP())
    md = render_markdown(build_report(ctx))
    for heading in ("## Facts (source precedence)", "## Drift", "## Invariants", "## Timing",
                    "## Documentation Health"):  # fmt: skip
        assert heading in md
    assert "live_observed > runtime_config > runtime_record" in md


def test_documentation_health_only_counts_dated_markers(tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/a.md").write_text(
        "<!-- state-corrected: 2026-09-26 fixed x -->\n`<!-- state-corrected: ... -->`\n",
        encoding="utf-8",
    )
    assert docs_health.corrected(tmp_path) == [{"path": "docs/a.md", "note": "2026-09-26 fixed x"}]
