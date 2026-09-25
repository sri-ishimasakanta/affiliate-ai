"""T7C: T7 の閉じ (policy の note・完了の条件・roadmap の移り変わり・on-demand・比較)。

偽の WordPress・手元の SQLite・偽のスケジューラ・偽の worker のログだけを使う (本番に触れない)。

pin する契約:

- ``automatic_publication.note`` は説明の文だけ (動作を決めない)。note を変えても消しても、
  ポリシーの動作 (公開の可否・窓・間隔・在庫など) は同じ。コミットした policy は note を除いて
  T7B の時点と同じ (正規化した JSON の SHA-256)。
- note が値と食い違えば ``stale_doc``、直せば消える。worker の ``mode=plan`` は info のまま。
- T7 は完了、N0 は次 (始めていない)。N0 の前提が済んでいなければ roadmap の問題として出す。
- 報告は on-demand を ``generation_policy`` として出すだけで、警告にしない。
- 運用の警告 (daily の partial・携帯の表示の確認待ち・tracking の欠けなど) は strict を
  失敗させない。次の行動は H の順。
- 比較は T7B → T7 の移り変わりと、ドキュメントの健康の解決を見つける。
- offline は外部を読まず、DB を変えない。秘密は伏せる。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from app.project_state import compare, docs_health, findings, strict
from app.project_state.generator import build_report, render_markdown
from app.project_state.roadmap import load_roadmap, verify_phases
from app.social.threads.policy import load_operations_policy
from tests.unit.test_project_state import NOW, REPO, _context

POLICY = REPO / "app/config/threads_operations_policy.json"
# T7B の時点 (85c40ce) の policy から note を除いて正規化した JSON の SHA-256。
BEHAVIOUR_SHA256 = "7baa2fcf054ea3fe855138f4c45c4b0bec212d0820dc97b2f02d6f36cee10e2d"
OLD_NOTE = (
    "Committed disabled. Flip only after Meta API access is restored and a human has approved "
    "production activation. The worker flag alone never publishes."
)
BEHAVIOUR = (
    "automatic_publication_enabled", "automatic_publication_preflight", "soft_min_gap_minutes",
    "daily_target_low", "daily_target_high", "publication_window",
    "approval_notification_window", "heartbeat_max_seconds", "stale_lock_after_minutes",
    "idle_publication_reevaluation_minutes", "starvation_guard_hours", "digest_max_items",
    "digest_min_items", "digest_cooldown_minutes", "digest_gather_minutes",
    "approval_ttl_hours", "digest_preview_characters", "stock_days_low", "stock_days_high",
    "policy_version",
)  # fmt: skip


def _behaviour(path: Path) -> dict:
    policy = load_operations_policy(path)
    return {name: getattr(policy, name) for name in BEHAVIOUR}


def _write_policy(path: Path, change) -> None:
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    change(data["automatic_publication"])
    path.write_text(json.dumps(data), encoding="utf-8")


def _action_ids(report) -> list[str]:
    return [a["id"] for a in report["next_actions"]]


# == A: the policy note is descriptive only =========================================
def test_the_policy_note_does_not_change_behaviour(tmp_path) -> None:
    base = _behaviour(POLICY)
    assert base["automatic_publication_enabled"] is True
    for label, change in (
        ("old", lambda a: a.update(note=OLD_NOTE)),
        ("other", lambda a: a.update(note="disabled disabled OFF")),
        ("none", lambda a: a.pop("note")),
    ):
        path = tmp_path / f"{label}.json"
        _write_policy(path, change)
        assert _behaviour(path) == base, label


def test_no_production_code_reads_the_note() -> None:
    readers = []
    for path in sorted((REPO / "app").rglob("*.py")):
        if "project_state" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "automatic_publication" in text and ('"note"' in text or "'note'" in text):
            readers.append(path.relative_to(REPO).as_posix())
    # threads_worker_service は自分の subsystem の summary に "note" を書くだけ (policy は読まない)
    assert readers in ([], ["app/services/threads_worker_service.py"])
    source = (REPO / "app/social/threads/policy.py").read_text(encoding="utf-8")
    assert 'section("automatic_publication").get("note")' not in source


def test_only_the_note_changed_in_the_committed_policy() -> None:
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    note = data["automatic_publication"].pop("note")
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == BEHAVIOUR_SHA256
    assert data["automatic_publication"] == {"enabled": True, "preflight_read": True}
    assert "Enabled in production" in note and not docs_health.note_contradicts_value(True, note)


def test_a_contradicting_note_is_stale_and_clears_after_correction(tmp_path) -> None:
    assert docs_health.note_contradicts_value(True, OLD_NOTE)
    assert docs_health.note_contradicts_value(False, "Enabled in production since x")
    assert not docs_health.note_contradicts_value(False, OLD_NOTE)
    ctx, _, _ = _context(tmp_path)
    policy = ctx.root / "app/config/threads_operations_policy.json"
    clean = build_report(ctx)
    assert clean["documentation_health"]["unresolved_mismatches"] == []
    lock = next(f for f in clean["drift"] if f["id"] == "threads-worker-lock-label-mode-plan")
    assert (lock["classification"], lock["severity"]) == ("expected_difference", "info")
    _write_policy(policy, lambda a: a.update(note=OLD_NOTE))
    stale = build_report(ctx)
    ids = [d["id"] for d in stale["documentation_health"]["unresolved_mismatches"]]
    assert ids == ["config-note-threads-autopublish"]
    assert "review-policy-note-text" in _action_ids(stale)
    assert strict.failures(stale) == []  # 説明の文の食い違いでは止めない


# == E / F: T7 completion + roadmap ==================================================
def test_the_t7_completion_criteria_hold(tmp_path) -> None:
    ctx, wp, db_path = _context(tmp_path)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    live = build_report(ctx)
    assert live["mode"] == "live" and set(wp.calls) == {"posts", "categories", "tags", "media"}
    assert strict.validate(live) == [] and strict.failures(live) == []
    for name, fact in live["facts"].items():
        assert fact["authority"] and fact["source"], name  # provenance が付いている
    assert isinstance(live["drift"], list) and live["drift"]  # 検出が動いている
    # roadmap は機械で読める (根拠のファイルは本物のリポジトリで確かめる。fixture は一部だけ)
    real = verify_phases(REPO, load_roadmap(REPO), commit_exists=lambda sha: True)
    assert real["problems"] == [] and real["next_phase"] == "N0"
    assert not [f for f in live["drift"] if f["blocking"] or f["classification"] == "unresolved"]
    order = [findings.action_sort_key(a) for a in live["next_actions"]]
    assert order == sorted(order)
    md = render_markdown(live)
    for fact in ("25/25", "afc2f36bb3ca", "next: **N0** (not started)"):
        assert fact in md
    offline_ctx, _, _ = _context(tmp_path / "offline", offline=True, factory=_refuse)
    offline = build_report(offline_ctx)
    assert offline["mode"] == "offline" and strict.validate(offline) == []
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert "T7" in live["project"]["completed_phases"]
    assert live["project"]["next_phase"] == "N0"


def _refuse():
    raise AssertionError("offline mode must not build a WordPress client")


def test_the_roadmap_needs_the_prerequisites_of_the_next_phase() -> None:
    roadmap = {
        "current_phase": None, "next_phase": "N0", "last_completed_phase": "T7B",
        "phases": [
            {"id": "T7B", "title": "t", "status": "complete", "evidence": {}},
            {"id": "T7", "title": "t", "status": "active", "evidence": {}},
            {"id": "N0", "title": "n", "status": "planned", "prerequisites": ["T7"],
             "evidence": {}},
        ],
    }  # fmt: skip
    out = verify_phases(REPO, roadmap, commit_exists=None)
    assert out["next_phase_prerequisites_unmet"] == ["T7"]
    assert any("unmet prerequisites" in p for p in out["problems"])
    assert any("current_phase is null but ['T7'] are active" in p for p in out["problems"])
    roadmap["phases"][1]["status"] = "complete"
    assert verify_phases(REPO, roadmap, commit_exists=None)["problems"] == []


# == G / H: on-demand + action order =================================================
def test_on_demand_generation_is_a_decision_not_a_warning(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    policy = report["project"]["generation_policy"]
    assert (policy["mode"], policy["scheduled"]) == ("on_demand", False)
    text = json.dumps(report["warnings"] + report["next_actions"], ensure_ascii=False).lower()
    assert "project-state" not in text and "project_state" not in text
    assert not any("schedule project" in a["action"] for a in report["next_actions"])
    assert "Project-state generation: on_demand" in render_markdown(report)


def test_operational_warnings_do_not_block_and_actions_follow_the_order(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)  # 12:00 JST: 06:30 の daily の確認は due
    report = build_report(ctx)
    ids = {w["id"] for w in report["warnings"]}
    assert {"ops-latest-daily-run-not-clean", "monetization-missing-tracking",
            "approvals-mobile-render-unobserved", "git-ahead-of-remote",
            "threads-stock-maintenance-off", "wp-media-99-duplicate"} <= ids  # fmt: skip
    assert not [w for w in report["warnings"] if w["blocking"]]
    assert strict.failures(report) == []
    order = _action_ids(report)
    expected = ["check-daily-run", "observe-mobile-approval-render",
                "set-up-missing-affiliate-programs", "prepare-n0", "note-n1-n3",
                "c10-after-maturity"]  # fmt: skip
    assert [a for a in order if a in expected] == expected
    assert "t7-validate-project-state" not in order
    n0 = next(a for a in report["next_actions"] if a["id"] == "prepare-n0")
    assert n0["prerequisites"] == [] and "not started" in n0["action"]


def test_a_due_diagnostic_sits_after_tracking_and_before_n0(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report_path = ctx.root / "reports/threads_performance_diagnostic_latest.json"
    old = json.loads(report_path.read_text(encoding="utf-8"))
    old["generated_at"] = old["as_of"] = (NOW - timedelta(days=9)).isoformat()
    report_path.write_text(json.dumps(old), encoding="utf-8")
    order = _action_ids(build_report(ctx))
    assert (
        order.index("set-up-missing-affiliate-programs")
        < order.index("rerun-threads-performance-diagnostic")
        < order.index("prepare-n0")
    )


# == J: report diff ==================================================================
def test_compare_sees_the_t7_transition_and_the_resolved_note(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    roadmap_path = ctx.root / "docs/project-roadmap.json"
    current = roadmap_path.read_text(encoding="utf-8")
    t7b = json.loads(current)
    t7b.update(current_phase="T7B", next_phase=None, last_completed_phase=None)
    for phase in t7b["phases"]:
        if phase["id"] == "T7B":
            phase["status"] = "active"
    t7b["phases"] = [p for p in t7b["phases"] if p["id"] != "T7"]
    roadmap_path.write_text(json.dumps(t7b), encoding="utf-8")
    policy = ctx.root / "app/config/threads_operations_policy.json"
    fixed = policy.read_text(encoding="utf-8")
    _write_policy(policy, lambda a: a.update(note=OLD_NOTE))
    old = build_report(ctx)
    roadmap_path.write_text(current, encoding="utf-8")
    policy.write_text(fixed, encoding="utf-8")
    ctx.now = NOW + timedelta(minutes=2)  # 時刻・heartbeat だけの違いは無視される
    new = build_report(ctx)
    diff = compare.compare(old, new)
    assert diff["facts"]["current_phase"] == {"old": "T7B", "new": None}
    assert diff["facts"]["next_phase"] == {"old": None, "new": "N0"}
    assert diff["facts"]["last_completed_phase"] == {"old": None, "new": "T7"}
    assert "config-note-threads-autopublish" in diff["drift"]["removed"]
    assert "t7-validate-project-state" in diff["next_actions"]["removed"]
    assert not any("generated_at" in p or "heartbeat" in p for p in diff["other_changes"])


# == K / redaction ==================================================================
def test_offline_mode_reads_nothing_external_and_writes_nothing(tmp_path) -> None:
    ctx, _, db_path = _context(tmp_path, offline=True, factory=_refuse)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    report = build_report(ctx)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    for name in ("wordpress", "featured_images", "taxonomy"):
        assert report[name]["provenance"]["status"] == "skipped"
    assert report["scheduler"]["changes_made"] is False
    assert json.loads(json.dumps(report)) == report
    assert "## Drift" in render_markdown(report)
    # offline では観測していない WordPress の不変条件は「不明」(失敗にしない)
    inv = {i["id"]: i["result"] for i in report["invariants"]["results"]}
    assert inv["wordpress-taxonomy-matches-plan"] == "unknown"
    assert strict.failures(report) == []


def test_secrets_in_drift_values_are_redacted(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    policy = ctx.root / "app/config/threads_operations_policy.json"
    token = "THAA" + "x" * 40
    _write_policy(
        policy,
        lambda a: a.update(
            note=f"disabled; access_token={token} Bearer abcdefghijklmnopqrstu "
            "https://user:pa55word@host.example/go/secret"
        ),
    )
    report = build_report(ctx)
    text = json.dumps(report, ensure_ascii=False) + render_markdown(report)
    for leaked in (token, "abcdefghijklmnopqrstu", "pa55word", "/go/secret"):
        assert leaked not in text
    assert any(f["id"] == "config-note-threads-autopublish" for f in report["drift"])
