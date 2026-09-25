"""T7A: プロジェクトの状態の報告 (偽の WordPress・手元の SQLite・偽のコマンド。本番に触れない)。

pin する契約:

- 報告の形 (上の階層のキー) と、各セクションの出どころ (source / kind / status / freshness)。
- 同じ入力からは同じ報告。offline では WordPress を 1 度も呼ばない。読めない外部は
  「読めなかった」になり、落ちない。
- 秘密を出さない (キー・値の両方)。``/go/`` を読まない。tracking URL を出さない。
- WordPress 25/25・W2 のカテゴリ・article 1 は親だけ・article 25 は AI・生成AI。
- 収益化の欠け (tracking の無いプログラム) を出す。在庫の保守 OFF を正しく出す。
- スケジューラは読むだけ。Threads の集計は DB に書かない。
- 警告・決定の記録の重複を消す。次の行動の順は決まっている。
- テストを実行していなければ件数を作らない。Markdown と JSON は同じ状態を表す。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.project_state import decision_log, findings, local_state, redaction, scheduler_state
from app.project_state.db_state import readonly_engine, worker_flags
from app.project_state.generator import SECTIONS, StateContext, build_report, render_markdown
from app.wordpress.taxonomy_plan import CHILDREN

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)
TOP_KEYS = {
    "generated_at", "generator_version", "mode", "project", "git", "quality", "database",
    "wordpress", "featured_images", "taxonomy", "monetization", "threads", "approvals",
    "analytics", "scheduler", "warnings", "decisions", "known_issues", "next_actions",
    "source_freshness",
}  # fmt: skip
CHILD_IDS = {c["slug"]: 5 + i for i, c in enumerate(CHILDREN)}
TRACKING_URL = "https://aff.example.test/go/SECRET-TOKEN-123"


# == fixtures =====================================================================
def _repo_copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(REPO / "docs", root / "docs")
    shutil.copytree(REPO / "migrations", root / "migrations")
    shutil.copy2(REPO / "alembic.ini", root / "alembic.ini")
    (root / "app/config").mkdir(parents=True)
    shutil.copy2(REPO / "app/config/threads_operations_policy.json", root / "app/config")
    (root / "scripts").mkdir()
    shutil.copy2(REPO / "scripts/run_threads_worker_task.cmd", root / "scripts")
    readme = root / "wordpress/mu-plugins/bizfluxlab-approval-relay.README.md"
    readme.parent.mkdir(parents=True)
    shutil.copy2(REPO / "wordpress/mu-plugins/bizfluxlab-approval-relay.README.md", readme)
    return root


_DDL = """
create table articles (id integer primary key, title text, slug text, wordpress_post_id integer);
create table affiliate_programs (id integer primary key, name text, provider text, status text,
  tracking_url text);
create table article_affiliate_programs (id integer primary key, article_id int,
  affiliate_program_id int, is_primary int);
create table affiliate_link_targets (id integer primary key, token text, article_id int,
  affiliate_program_id int, destination_url text, status text);
create table article_link_substitution_mappings (id integer primary key,
  affiliate_link_target_id int, status text);
create table affiliate_commission_facts (id integer primary key, occurred_at text);
create table affiliate_outbound_clicks (id integer primary key, clicked_at text);
create table revenue_optimization_runs (id integer primary key, created_at text,
  monetized_article_count int, candidate_count int, commission_data_through text);
create table threads_post_proposals (id integer primary key, status text,
  learning_guidance_json text, created_at text);
create table threads_publications (id integer primary key, proposal_id int, status text,
  trigger text, remote_username text, remote_timestamp text, published_at text, permalink text);
create table threads_insight_snapshots (id integer primary key, observed_at text, outcome text);
create table operations_locks (id integer primary key, lock_name text, owner_label text,
  acquired_at text, heartbeat_at text, released_at text, owner_token text);
create table mobile_approval_sessions (id integer primary key, state text, created_at text,
  decided_at text, synchronized_at text);
create table operations_runs (id integer primary key, profile text, status text,
  effective_date text, started_at text, finished_at text, step_total int, step_succeeded int,
  step_failed int);
create table operations_step_runs (id integer primary key, operations_run_id int, step_name text,
  status text, error_category text, rows_received int);
create table operations_alerts (id integer primary key, alert_type text, severity text,
  title text, status text, last_seen_at text, resolved_at text);
create table notification_deliveries (id integer primary key, notification_type text,
  outcome text, attempted_at text, recipient_hint text);
create table search_console_import_runs (id integer primary key, status text, end_date text,
  finished_at text);
create table ga4_import_runs (id integer primary key, status text, data_through_date text,
  finished_at text);
create table seo_improvement_runs (id integer primary key, created_at text, candidate_count int,
  evaluated_article_count int);
create table alembic_version (version_num text);
"""


def _db(tmp_path: Path, *, heartbeat_age=timedelta(seconds=60)) -> Path:
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.executescript(_DDL)
    for a in range(1, 26):
        db.execute("insert into articles values (?, ?, ?, ?)", (a, f"記事 {a}", f"a-{a}", 100 + a))
    db.executemany(
        "insert into affiliate_programs (id, name, provider, status, tracking_url) "
        "values (?,?,?,?,?)",
        [(1, "Make", "make", "active", TRACKING_URL), (5, "HubSpot", "hubspot", "active", None),
         (8, "ClickUp", "clickup", "active", None), (14, "Krisp", "krisp", "active", None)],
    )  # fmt: skip
    primary = [(10, 1), (11, 1), (4, 5), (5, 5), (13, 8)] + [(a, 14) for a in (2, 3, 6, 7, 8, 9)]
    db.executemany(
        "insert into article_affiliate_programs (article_id, affiliate_program_id, is_primary) "
        "values (?, ?, 1)",
        primary,
    )
    for i, a in enumerate((10, 11), start=1):
        db.execute(
            "insert into affiliate_link_targets values (?, 'tok', ?, 1, ?, 'active')",
            (i, a, TRACKING_URL),
        )
        db.execute("insert into article_link_substitution_mappings values (?, ?, 'active')", (i, i))
    db.execute("insert into threads_post_proposals values (1, 'approved', null, '2026-09-25')")
    db.execute(
        "insert into threads_publications values (1, 1, 'published', 'automatic', 'bizfluxlab', "
        "'2026-09-25T10:43:59+0000', '2026-09-25 10:43:24', 'https://threads.example/p/1')"
    )
    db.execute(
        "insert into threads_insight_snapshots values (1, '2026-09-25 17:44:02', 'observed')"
    )
    heartbeat = (NOW - heartbeat_age).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "insert into operations_locks values (1, 'threads_worker', 'threads-worker pid=1', "
        "'2026-09-25 09:43:43', ?, null, 'SECRET-LOCK-TOKEN')",
        (heartbeat,),
    )
    db.execute(
        "insert into mobile_approval_sessions values "
        "(1, 'synchronized', '2026-09-25', null, '2026-09-25 08:42:45')"
    )
    db.execute("insert into operations_runs values (7, 'daily', 'partial', '2026-09-25', "
               "'2026-09-24 21:30:10', '2026-09-24 21:30:29', 9, 8, 0)")  # fmt: skip
    db.execute("insert into operations_step_runs values (1, 7, 'import_threads_insights', "
               "'partial', null, 1)")  # fmt: skip
    db.execute("insert into operations_alerts values (3, 'AUTOMATION_HEALTH', 'warning', 't', "
               "'resolved', '2026-09-24', '2026-09-25')")  # fmt: skip
    db.execute("insert into notification_deliveries values (1, 'daily_incident', 'sent', "
               "'2026-09-24', 'secret-recipient@example.test')")  # fmt: skip
    db.execute("insert into alembic_version values ('afc2f36bb3ca')")
    db.commit()
    db.close()
    return path


class FakeWP:
    """読むだけの WordPress (25 記事・W2 のカテゴリ・25/25 の featured image)。"""

    def __init__(self, *, featured_missing=(), taxonomy_drift=False):
        self.calls = []
        self.posts, self.media = [], []
        child_of = {a: c["slug"] for c in CHILDREN for a in c["article_ids"]}
        for a in range(1, 26):
            cats = [4] if a == 1 else [CHILD_IDS[child_of[a]], 4]  # WordPress の順で返す
            if taxonomy_drift and a == 7:
                cats = [4]
            self.posts.append({
                "id": 100 + a, "status": "publish", "slug": f"a-{a}", "categories": cats,
                "tags": [], "featured_media": 0 if a in featured_missing else 1000 + a,
            })  # fmt: skip
            self.media.append({"id": 1000 + a, "post": None})
        self.media.append({"id": 99, "post": None})
        self.categories = [
            {"id": 1, "name": "未分類", "slug": "uncategorized", "parent": 0, "count": 0},
            {"id": 4, "name": "業務効率化", "slug": "gyomu-koritsuka", "parent": 0, "count": 25},
        ] + [
            {"id": CHILD_IDS[c["slug"]], "name": c["name"], "slug": c["slug"], "parent": 4,
             "count": len(c["article_ids"])}
            for c in CHILDREN
        ]  # fmt: skip

    def list_post_states(self):
        self.calls.append("posts")
        return copy.deepcopy(self.posts)

    def list_categories(self):
        self.calls.append("categories")
        return copy.deepcopy(self.categories)

    def list_tags(self):
        self.calls.append("tags")
        return []

    def list_media_items(self):
        self.calls.append("media")
        return copy.deepcopy(self.media)

    def __getattr__(self, name):
        raise AssertionError(f"the project state must not call {name}")


def _runner(outputs=None):
    outputs = {
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n"),
        ("git", "rev-parse", "HEAD"): (0, "cdb4c6e236ace5fd99386ba0c52d693f24e7145b\n"),
        ("git", "log", "-1", "--format=%s"): (0, "docs: record W2 taxonomy rollout\n"),
        ("git", "status", "--porcelain"): (0, "?? artifacts/\n"),
        ("git", "rev-list", "--left-right", "--count", "origin/main...HEAD"): (0, "0\t26\n"),
        ("git", "log", "--oneline", "-15"): (0, "cdb4c6e docs: record W2 taxonomy rollout\n"),
        ("uv", "run", "ruff", "check", "."): (0, "All checks passed!\n"),
        ("uv", "run", "alembic", "check"): (0, "No new upgrade operations detected.\n"),
        ("git", "diff", "--check"): (0, ""),
        ("uv", "run", "pytest", "-q"): (0, "5435 passed, 1 warning in 240.00s\n"),
        **(outputs or {}),
    }
    calls = []

    def run(args):
        calls.append(tuple(args))
        return outputs[tuple(args)]

    run.calls = calls
    return run


SCHEDULER_JSON = json.dumps([
    {"name": "affiliate-ai-operations-daily", "exists": True, "state": "Ready", "enabled": True,
     "triggers": [{"kind": "MSFT_TaskWeeklyTrigger", "start": "2026-09-23T06:30:00",
                   "repetition": ""}], "action": "run_operations_task.cmd", "arguments": "daily",
     "last_run": "2026-09-25T06:30:00+09:00", "last_result": 1, "next_run": "x"},
    {"name": "affiliate-ai-threads-worker", "exists": True, "state": "Running", "enabled": True,
     "triggers": {"kind": "MSFT_TaskTimeTrigger", "start": "2026-09-25T10:10:00",
                  "repetition": "PT15M"}, "action": "run_threads_worker_task.cmd",
     "arguments": "publish", "last_run": "y", "last_result": 2147946720, "next_run": "z"},
])  # fmt: skip


def _context(tmp_path, *, offline=False, wp=None, runner=None, factory=None, **kw):
    root = _repo_copy(tmp_path)
    db_path = _db(tmp_path, **kw)
    wp = wp or FakeWP()
    return (
        StateContext(
            root=root,
            now=NOW,
            database_url=f"sqlite:///{db_path.as_posix()}",
            offline=offline,
            runner=runner or _runner(),
            wordpress_client_factory=factory or (lambda: wp),
            scheduler_reader=lambda: SCHEDULER_JSON,
            commit_exists=lambda sha: True,
        ),
        wp,
        db_path,
    )


# == contract / determinism =======================================================
def test_the_report_has_the_documented_top_level_contract(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    assert set(report) == TOP_KEYS
    assert report["generator_version"] == "project-state/1" and report["mode"] == "live"
    assert set(report["source_freshness"]) == set(SECTIONS)
    for prov in report["source_freshness"].values():
        assert {"source", "kind", "status", "observed_at", "freshness"} <= set(prov)
    assert json.loads(json.dumps(report)) == report  # JSON にそのまま出せる


def test_the_same_inputs_give_the_same_report(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    assert json.dumps(build_report(ctx), sort_keys=True) == json.dumps(
        build_report(ctx), sort_keys=True
    )


def test_offline_mode_never_touches_wordpress(tmp_path) -> None:
    def refuse():
        raise AssertionError("offline mode must not build a WordPress client")

    ctx, _, _ = _context(tmp_path, offline=True, factory=refuse)
    report = build_report(ctx)
    assert report["mode"] == "offline"
    for name in ("wordpress", "featured_images", "taxonomy"):
        assert report[name]["provenance"]["status"] == "skipped"
        assert report[name]["provenance"]["freshness"] == "unverified"
    assert report["featured_images"]["declared_count"] == 25  # リポジトリの記録は「宣言」として残る
    assert "## Featured Images" in render_markdown(report)


def test_an_unreadable_wordpress_degrades_without_crashing(tmp_path) -> None:
    def broken():
        raise ConnectionError("down")

    ctx, _, _ = _context(tmp_path, factory=broken)
    report = build_report(ctx)
    prov = report["wordpress"]["provenance"]
    assert prov["status"] == "unavailable" and "ConnectionError" in prov["reason"]
    assert any(w["id"] == "source-unavailable-wordpress" for w in report["warnings"])


# == WordPress / featured images / taxonomy =======================================
def test_wordpress_featured_images_and_taxonomy_are_summarized(tmp_path) -> None:
    ctx, wp, _ = _context(tmp_path)
    report = build_report(ctx)
    assert set(wp.calls) == {"posts", "categories", "tags", "media"}
    assert report["wordpress"]["published_count"] == 25 and report["wordpress"]["tag_count"] == 0
    fi = report["featured_images"]
    assert (fi["with_featured_image"], fi["published"], fi["without_featured_image"]) == (
        25,
        25,
        [],
    )
    assert fi["media_99"]["exists"] is True and fi["media_99"]["featured_by"] == []
    tax = report["taxonomy"]
    assert tax["matches_plan"] is True and tax["article_1_parent_only"] is True
    assert tax["parent_plus_child_posts"] == 24
    assert [c["count"] for c in tax["children"]] == [7, 7, 5, 3, 2]
    ai = next(c for c in tax["plan"]["children"] if c["slug"] == "ai-generative-ai")
    assert 25 in ai["article_ids"]  # article 25 は AI・生成AI
    assert tax["plan"]["parent_only_article_ids"] == [1]


def test_missing_featured_images_and_taxonomy_drift_are_warnings(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path, wp=FakeWP(featured_missing=(3,), taxonomy_drift=True))
    report = build_report(ctx)
    ids = {w["id"] for w in report["warnings"]}
    assert {"wp-missing-featured-images", "wp-taxonomy-drift"} <= ids
    assert report["taxonomy"]["matches_plan"] is False
    assert any(a["id"] == "resolve-blocking-warnings" for a in report["next_actions"])


# == monetization / secrets ========================================================
def test_missing_affiliate_tracking_is_surfaced_and_tracking_urls_never_appear(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    money = report["monetization"]
    assert money["live_programs"] == ["Make"]
    assert [m["article_id"] for m in money["monetized_articles"]] == [10, 11]
    assert money["missing_tracking"] == [
        {"program": "ClickUp", "article_ids": [13]},
        {"program": "HubSpot", "article_ids": [4, 5]},
        {"program": "Krisp", "article_ids": [2, 3, 6, 7, 8, 9]},
    ]
    assert money["commissions_known"] is False
    text = json.dumps(report, ensure_ascii=False) + render_markdown(report)
    for secret in (TRACKING_URL, "SECRET-TOKEN-123", "SECRET-LOCK-TOKEN", "secret-recipient"):
        assert secret not in text
    assert not re.search(r"https?://\S*/go/", text)  # /go/ の URL は 1 つも出ない


def test_the_redaction_pass_removes_secret_like_keys_and_values() -> None:
    sha = "cbc2a42518bdbb7b2813ad7efed3204465d9d6ba62346fad89547eceb5021700"
    data = {
        "wordpress_app_password": "abcd efgh",
        "nested": {"access_token": "x", "api_key": "y", "cookie": "z", "note": f"sha {sha}"},
        "url": "https://user:pa55word@host.example/path",
        "header": "Authorization: Bearer abcdefghijklmnop",
        "threads": "token THAAabcdefghijklmnopqrstuvwxyz0123",
        "link": "https://bizfluxlab.com/go/abc123",
        "dsn": "postgresql://u:p@db/x",
    }
    out = redaction.redact(data)
    text = json.dumps(out)
    for leaked in ("abcd efgh", "pa55word", "abcdefghijklmnop", "THAAabcdef", "/go/abc123", "u:p@"):
        assert leaked not in text
    assert out["nested"]["note"].endswith(sha)  # digest は根拠として残す


# == Threads / approvals / scheduler ===============================================
def test_threads_state_is_read_without_writing_the_database(tmp_path) -> None:
    ctx, _, db_path = _context(tmp_path)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    report = build_report(ctx)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    threads = report["threads"]
    assert threads["account"] == ["bizfluxlab"]
    assert threads["worker"]["running"] is True
    assert threads["worker"]["stock_maintenance_enabled"] is False  # 在庫の保守は OFF
    assert threads["policy"]["soft_min_gap_minutes"] == 120
    assert threads["policy"]["fixed_posting_times"] is False
    assert any(w["id"] == "threads-stock-maintenance-off" and w["severity"] == "info"
               for w in report["warnings"])  # fmt: skip
    assert report["approvals"]["genuine_mobile_render_observed"] is False


def test_the_readonly_engine_cannot_write(tmp_path) -> None:
    from sqlalchemy import text

    path = _db(tmp_path)
    engine = readonly_engine(f"sqlite:///{path.as_posix()}")
    with engine.connect() as conn, pytest.raises(Exception, match="readonly"):
        conn.execute(text("insert into articles values (99, 'x', 'x', 1)"))


def test_a_stale_worker_heartbeat_is_not_running(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path, heartbeat_age=timedelta(minutes=20))
    assert build_report(ctx)["threads"]["worker"]["running"] is False


def test_stock_maintenance_on_is_detected_from_the_launcher(tmp_path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/run_threads_worker_task.cmd").write_text(
        'if /I "%PROFILE%"=="publish" set "FLAGS=--auto-publish --maintain-proposal-stock"\n'
    )
    assert worker_flags(tmp_path)["stock_maintenance_enabled"] is True


def test_the_scheduler_collector_is_read_only() -> None:
    script = scheduler_state._SCRIPT
    assert "Get-ScheduledTask" in script and "Get-ScheduledTaskInfo" in script
    for verb in ("Set-", "Start-", "Stop-", "Register-", "Unregister-", "Enable-", "Disable-",
                 "schtasks"):  # fmt: skip
        assert verb not in script
    out = scheduler_state.summarize(SCHEDULER_JSON, now=NOW)
    daily = out["tasks"]["affiliate-ai-operations-daily"]
    worker = out["tasks"]["affiliate-ai-threads-worker"]
    assert daily["warnings"] and not worker["warnings"]
    assert "already running" in worker["last_result_meaning"]
    assert out["changes_made"] is False


# == quality / git =================================================================
def test_git_parsing() -> None:
    assert local_state.parse_ahead_behind("0\t26\n") == (0, 26)
    assert local_state.parse_ahead_behind("garbage") is None
    files = local_state.parse_porcelain("M  a.py\n M b.py\nMM c.py\n?? d/\n")
    assert files == {"staged": ["a.py", "c.py"], "unstaged": ["b.py", "c.py"], "untracked": ["d/"]}


def test_an_unrun_test_suite_is_never_fabricated(tmp_path) -> None:
    run = _runner()
    quality = local_state.collect_quality(run, tmp_path, now=NOW, with_tests=False)
    assert quality["pytest"]["passed"] is None and quality["pytest"]["freshness"] == "unverified"
    assert ("uv", "run", "pytest", "-q") not in run.calls
    fresh = local_state.collect_quality(run, tmp_path, now=NOW, with_tests=True)
    assert (fresh["pytest"]["passed"], fresh["pytest"]["freshness"]) == (5435, "fresh")
    recorded = local_state.collect_quality(_runner(), tmp_path, now=NOW, with_tests=False)
    assert (recorded["pytest"]["passed"], recorded["pytest"]["freshness"]) == (5435, "recorded")


def test_the_database_target_is_described_without_secrets() -> None:
    assert local_state.describe_url("sqlite:///./affiliate_ai.db") == "sqlite (affiliate_ai.db)"
    described = local_state.describe_url("postgresql+psycopg://user:pw@db.example:5432/app")
    assert described == "postgresql+psycopg (db.example)" and "pw" not in described


# == warnings / decisions / next actions ===========================================
def test_warnings_are_deduplicated_and_ordered() -> None:
    w = findings.warning
    items = [
        w("b", "low", "x", "m", evidence="e", action_required="a"),
        w("a", "high", "x", "m", evidence="e", action_required="a"),
        w("b", "low", "x", "other", evidence="e", action_required="a"),
    ]
    assert [(i["id"], i["message"]) for i in findings.dedupe(items)] == [("a", "m"), ("b", "m")]


def test_next_actions_are_deterministic_and_never_jump_to_c10(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    actions = report["next_actions"]
    order = [(a["priority"], a["id"]) for a in actions]
    assert order == sorted(order)
    c10 = next(a for a in actions if a["id"] == "c10-after-maturity")
    assert c10["blocking"] is True and "prepare-n0" in c10["prerequisites"]
    assert actions[0]["priority"] != "P3"
    money = next(a for a in actions if a["id"] == "set-up-missing-affiliate-programs")
    assert money["production_write_required"] and money["human_checkpoint_required"]


def test_the_decision_log_never_repeats_an_entry(tmp_path) -> None:
    entry = {"id": "d1", "area": "a", "decision": "決定", "rationale": "理由",
             "evidence": ["docs/x.md"], "resulting_state": "状態"}  # fmt: skip
    first = decision_log.append_entries(tmp_path, [entry, entry], moment=NOW)
    assert first == ["d1"]
    assert decision_log.append_entries(tmp_path, [entry], moment=NOW) == []
    next_week = NOW + timedelta(days=7)
    assert decision_log.append_entries(tmp_path, [entry], moment=next_week) == []  # 週をまたいでも
    path = decision_log.log_path(tmp_path, NOW)
    assert path.name == "2026-W39.md" and path.read_text("utf-8").count("decision:d1") == 1
    with pytest.raises(ValueError, match="missing"):
        decision_log.append_entries(tmp_path, [{"id": "d2"}], moment=NOW)


def test_decisions_need_their_evidence(tmp_path) -> None:
    from app.project_state.decisions import check_support

    rows = {r["id"]: r for r in check_support(REPO)}
    assert all(r["supported"] for r in rows.values()), [
        r for r in rows.values() if not r["supported"]
    ]
    fake = [
        {
            "id": "x",
            "area": "a",
            "decision": "d",
            "rationale": "r",
            "resulting_state": "s",
            "evidence": ["docs/operations/taxonomy-w2.md"],
            "phrase": "存在しない言い回し",
        }
    ]
    assert check_support(REPO, fake)[0]["supported"] is False


# == Markdown vs JSON =============================================================
def test_markdown_and_json_describe_the_same_core_state(tmp_path) -> None:
    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    md = render_markdown(report)
    assert f"**{report['project']['current_phase']}** active" in md
    assert "featured images 25/25" in md
    assert f"{report['git']['ahead']} ahead" in md
    for child in report["taxonomy"]["children"]:
        assert f"| {child['name']} | {child['id']} | `{child['slug']}` | {child['count']} |" in md
    for w in report["warnings"]:
        assert w["message"] in md
    assert "{" not in md.split("## Source Freshness")[1]  # 生の JSON を貼らない
