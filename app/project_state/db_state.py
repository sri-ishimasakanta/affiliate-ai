"""アプリの DB から読む状態: 収益化・Threads・承認・C8 の運用 (読むだけ)。

- SQLite は ``mode=ro`` で開く (エンジンの段階で書けない)。ほかの DB は SELECT だけ。
- 秘密の列は選ばない: ``affiliate_programs.tracking_url`` (有無だけ)、
  ``operations_locks.owner_token``、
  通知の宛先、``affiliate_link_targets.token`` / ``destination_url``。``/go/`` は読まない。
- Threads の API・承認の中継・WordPress には問い合わせない (DB とリポジトリのファイルだけ)。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text

from app.project_state.provenance import provenance, unavailable

THREADS_POLICY = Path("app/config/threads_operations_policy.json")
WORKER_LAUNCHER = Path("scripts/run_threads_worker_task.cmd")
STOCK_STATUS = Path("data/threads-generation/status.json")
PERFORMANCE_REPORT = Path("reports/threads_performance_diagnostic_latest.json")
STOCK_DOC = Path("docs/operations/threads-proposal-stock.md")
RELAY_README = Path("wordpress/mu-plugins/bizfluxlab-approval-relay.README.md")
WORKER_LOCK = "threads_worker"
PERFORMANCE_RERUN_NEW_6H = 3
PERFORMANCE_MAX_AGE = timedelta(days=7)


def readonly_engine(database_url: str):
    """SQLite は URI の ``mode=ro`` で開く。ほかは通常の接続 (SELECT だけを送る)。"""

    if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
        path = Path(database_url.removeprefix("sqlite:///")).resolve()
        return create_engine(
            f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true", connect_args={"uri": True}
        )
    return create_engine(database_url)


def _rows(conn, sql: str, **params) -> list[dict]:
    return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.isoformat(timespec="seconds")
    parsed = _parse(value) if len(str(value)) > 10 else None
    return parsed.isoformat(timespec="seconds") if parsed else str(value)


def _parse(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text_value = str(value).replace("Z", "+00:00")
    if len(text_value) >= 5 and text_value[-5] in "+-" and text_value[-3] != ":":
        text_value = text_value[:-2] + ":" + text_value[-2:]
    try:
        dt = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _read_json(root: Path, rel: Path):
    path = root / rel
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# == monetization ================================================================
def collect_monetization(conn, *, now: datetime) -> dict:
    programs = _rows(
        conn,
        "select id, name, provider, status, (tracking_url is not null and tracking_url != '') "
        "as has_tracking from affiliate_programs order by id",
    )
    primary = _rows(
        conn,
        "select article_id, affiliate_program_id from article_affiliate_programs "
        "where is_primary = 1 order by article_id",
    )
    targets = _rows(
        conn,
        "select t.article_id, t.affiliate_program_id, m.status as mapping_status "
        "from affiliate_link_targets t join article_link_substitution_mappings m "
        "on m.affiliate_link_target_id = t.id where t.status = 'active' and m.status = 'active' "
        "order by t.article_id",
    )
    by_id = {p["id"]: p for p in programs}
    tracked = sorted({(t["article_id"], t["affiliate_program_id"]) for t in targets})
    monetized_articles = sorted({a for a, _ in tracked})
    live_programs = sorted(
        {by_id[p]["name"] for _, p in tracked if by_id.get(p, {}).get("has_tracking")}
    )
    missing: dict[int, list[int]] = {}
    for row in primary:
        program = by_id.get(row["affiliate_program_id"]) or {}
        has_target = (row["article_id"], row["affiliate_program_id"]) in tracked
        if not program.get("has_tracking") or not has_target:
            missing.setdefault(row["affiliate_program_id"], []).append(row["article_id"])
    placements = Counter(t["article_id"] for t in targets)
    commissions = _rows(
        conn, "select count(*) as n, max(occurred_at) as latest from affiliate_commission_facts"
    )[0]
    clicks = _rows(
        conn, "select count(*) as n, max(clicked_at) as latest from affiliate_outbound_clicks"
    )[0]
    revenue_run = _rows(
        conn,
        "select id, created_at, monetized_article_count, candidate_count, commission_data_through "
        "from revenue_optimization_runs order by id desc limit 1",
    )
    return {
        "provenance": provenance(
            "local_db",
            kind="observed",
            status="ok",
            observed_at=now,
            freshness="fresh",
            detail=(
                "affiliate tables (tracking URLs are reported as present/absent only; /go/ is "
                "never requested)"
            ),
        ),
        "live_programs": live_programs,
        "monetized_articles": [
            {
                "article_id": a,
                "programs": sorted(by_id[p]["name"] for x, p in tracked if x == a),
                "active_placements": placements[a],
            }
            for a in monetized_articles
        ],
        "missing_tracking": [
            {"program": by_id[p]["name"], "article_ids": sorted(ids)}
            for p, ids in sorted(missing.items(), key=lambda kv: by_id[kv[0]]["name"])
        ],
        "programs_without_tracking_link": sorted(
            p["name"] for p in programs if not p["has_tracking"]
        ),
        "commission_facts": {"count": commissions["n"], "latest": _iso(commissions["latest"])},
        "outbound_clicks": {"count": clicks["n"], "latest": _iso(clicks["latest"])},
        "latest_revenue_run": {
            k: _iso(v) if k == "created_at" else v for k, v in revenue_run[0].items()
        }
        if revenue_run
        else None,
        "revenue_report_available": bool(revenue_run),
        "commissions_known": commissions["n"] > 0,
    }


# == threads =====================================================================
def worker_flags(root: Path) -> dict:
    """スケジュールの launcher が publish で渡す flag (リポジトリの宣言)。"""

    path = root / WORKER_LAUNCHER
    text_value = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    line = next((ln for ln in text_value.splitlines() if '"publish"' in ln and "FLAGS=" in ln), "")
    flags = line.split("FLAGS=", 1)[-1].strip().rstrip('"').split() if line else []
    return {
        "publish_profile_flags": flags,
        "stock_maintenance_enabled": "--maintain-proposal-stock" in flags,
    }


def performance_state(root: Path, conn, *, now: datetime) -> dict:
    report = _read_json(root, PERFORMANCE_REPORT)
    if report is None:
        return {
            "status": "unavailable",
            "reason": "no diagnostic report yet",
            "rerun_recommended": True,
            "rerun_reason": "never run",
        }
    comparable_6h = {
        p["publication_id"]
        for p in report.get("publications", [])
        if (p.get("checkpoints", {}).get("6h") or {}).get("comparable")
    }
    pubs = _rows(
        conn,
        (
            "select id, remote_timestamp, published_at from threads_publications where status = "
            "'published'"
        ),
    )
    # 「新しく 6h を過ぎた」= 報告の時点 (as_of) では 6h 未満で、今は 6h 以上。報告の時点で
    # 既に 6h を過ぎていたのに比べられなかったもの (観測の欠け) は数えない。
    as_of = _parse(report.get("as_of")) or _parse(report.get("generated_at"))
    six = timedelta(hours=6)
    matured = [
        p["id"]
        for p in pubs
        if (t := _parse(p["remote_timestamp"]) or _parse(p["published_at"]))
        and now - t >= six
        and (as_of is None or as_of - t < six)
        and p["id"] not in comparable_6h
    ]
    generated = _parse(report.get("generated_at"))
    reasons = []
    if len(matured) >= PERFORMANCE_RERUN_NEW_6H:
        reasons.append(f"{len(matured)} publication(s) newly past 6h: {sorted(matured)}")
    if generated and now - generated > PERFORMANCE_MAX_AGE:
        reasons.append(f"report is older than {PERFORMANCE_MAX_AGE.days} days")
    return {
        "status": (report.get("diagnostic") or {}).get("status"),
        "generated_at": report.get("generated_at"),
        "data_cutoff": report.get("data_cutoff"),
        "publication_count_in_report": report.get("publication_count"),
        "publication_count_now": len(pubs),
        "comparable_at_6h_in_report": sorted(comparable_6h),
        "newly_past_6h": sorted(matured),
        "rerun_rule": (
            f"re-run when >= {PERFORMANCE_RERUN_NEW_6H} publications are newly past 6h, or the "
            f"report is > {PERFORMANCE_MAX_AGE.days} days old"
        ),
        "rerun_recommended": bool(reasons),
        "rerun_reason": "; ".join(reasons) or None,
        "changed_production_behaviour": False,
    }


def collect_threads(root: Path, conn, *, now: datetime) -> dict:
    policy = _read_json(root, THREADS_POLICY) or {}
    proposals = Counter(
        r["status"] for r in _rows(conn, "select status from threads_post_proposals")
    )
    pubs = _rows(
        conn,
        "select id, status, trigger, remote_username, remote_timestamp, published_at, permalink "
        "from threads_publications order by id",
    )
    published = [p for p in pubs if p["status"] == "published"]
    approved_unpublished = _rows(
        conn,
        "select p.id from threads_post_proposals p where p.status = 'approved' and not exists "
        "(select 1 from threads_publications x where x.proposal_id = p.id) order by p.id",
    )
    insights = _rows(
        conn,
        "select count(*) as n, max(observed_at) as latest, "
        "sum(case when outcome != 'observed' then 1 else 0 end) as not_observed "
        "from threads_insight_snapshots",
    )[0]
    guidance = _rows(
        conn,
        "select count(*) as n, max(created_at) as latest from threads_post_proposals "
        "where learning_guidance_json is not null",
    )[0]
    lock = _rows(
        conn,
        "select owner_label, acquired_at, heartbeat_at, released_at from operations_locks "
        "where lock_name = :name",
        name=WORKER_LOCK,
    )
    worker = {**worker_flags(root)}
    max_age = (policy.get("worker") or {}).get("heartbeat_max_seconds", 300)
    if lock:
        row = lock[0]
        heartbeat = _parse(row["heartbeat_at"])
        age = (now - heartbeat).total_seconds() if heartbeat else None
        worker.update(
            lock_owner=row["owner_label"],
            lock_acquired_at=_iso(row["acquired_at"]),
            heartbeat_at=_iso(row["heartbeat_at"]),
            heartbeat_age_seconds=round(age) if age is not None else None,
            heartbeat_max_seconds=max_age,
            running=row["released_at"] is None and age is not None and age <= max_age,
            lock_label_note=(
                "owner_label says mode=plan while the task runs the publish profile (label only)"
            )
            if "mode=plan" in (row["owner_label"] or "")
            else None,
        )
    else:
        worker.update(running=False, lock_owner=None)
    stock = _read_json(root, STOCK_STATUS)
    latest = published[-1] if published else None
    auto = policy.get("automatic_publication") or {}
    return {
        "provenance": provenance(
            "local_db",
            kind="observed",
            status="ok",
            observed_at=now,
            freshness="fresh",
            detail=(
                "threads tables, the worker lock heartbeat and repository policy (no Threads API "
                "call)"
            ),
        ),
        "account": sorted({p["remote_username"] for p in pubs if p["remote_username"]}),
        "policy": {
            "policy_version": policy.get("policy_version"),
            "publication_window": policy.get("publication_window"),
            "approval_notification_window": policy.get("approval_notification_window"),
            "soft_min_gap_minutes": policy.get("soft_min_gap_minutes"),
            "automatic_publication_enabled": auto.get("enabled"),
            "one_publication_per_cycle": True,
            "one_publication_per_cycle_source": (
                "docs/operations/threads-worker.md (enforced in code)"
            ),
            "fixed_posting_times": False,
            "catch_up_bursts": False,
        },
        "worker": worker,
        "proposals": dict(sorted(proposals.items())),
        "approved_not_published": [r["id"] for r in approved_unpublished],
        "awaiting_approval": proposals.get("awaiting_approval", 0),
        "publications": {
            "total": len(pubs),
            "by_status": dict(sorted(Counter(p["status"] for p in pubs).items())),
            "by_trigger": dict(sorted(Counter(p["trigger"] for p in pubs).items())),
            "latest": {
                "id": latest["id"],
                "published_at": _iso(latest["remote_timestamp"] or latest["published_at"]),
            }
            if latest
            else None,
        },
        "insights": {
            "snapshots": insights["n"],
            "latest_observed_at": _iso(insights["latest"]),
            "not_observed": insights["not_observed"] or 0,
        },
        "learning_guidance": {
            "proposals_with_guidance": guidance["n"],
            "latest": _iso(guidance["latest"]),
        },
        "stock": {
            "source": str(STOCK_STATUS).replace("\\", "/") if stock else None,
            "freshness": "recorded" if stock else "unverified",
            "note": "runtime file from the last stock run; the live queue counts above are "
            "authoritative",
            **(
                {
                    k: stock.get(k)
                    for k in (
                        "last_maintenance_at",
                        "usable",
                        "needs_generation",
                        "created",
                        "last_generation_attempt_at",
                    )
                }
                if stock
                else {}
            ),
        },
        "performance": performance_state(root, conn, now=now),
    }


# == approvals ===================================================================
def collect_approvals(root: Path, conn, *, now: datetime) -> dict:
    sessions = Counter(
        r["state"] for r in _rows(conn, "select state from mobile_approval_sessions")
    )
    latest = _rows(
        conn,
        "select max(created_at) as created, max(decided_at) as decided, "
        "max(synchronized_at) as synced from mobile_approval_sessions",
    )[0]
    readme = (
        (root / RELAY_README).read_text(encoding="utf-8") if (root / RELAY_README).exists() else ""
    )
    stock_doc = (
        (root / STOCK_DOC).read_text(encoding="utf-8") if (root / STOCK_DOC).exists() else ""
    )
    render_line = next((ln for ln in stock_doc.splitlines() if "実物の携帯表示" in ln), "")
    observed = None if not render_line else "未確認" not in render_line
    discrepancies = []
    if "NOT DEPLOYED" in readme and "T6.1 deployment record" in readme:
        discrepancies.append(
            "relay README header says NOT DEPLOYED but the same file records the T6.1 deployment"
        )
    return {
        "provenance": provenance(
            "local_db",
            kind="observed",
            status="ok",
            observed_at=now,
            freshness="fresh",
            detail=(
                "mobile approval tables + relay README / stock doc (the relay itself is not "
                "contacted)"
            ),
        ),
        "c8_8_gateway": "implemented (mobile_approval_* tables in use)"
        if sessions
        else "no sessions yet",
        "sessions_by_state": dict(sorted(sessions.items())),
        "latest_session_created_at": _iso(latest["created"]),
        "latest_decision_at": _iso(latest["decided"]),
        "latest_sync_at": _iso(latest["synced"]),
        "relay_t6_1_deployment_recorded": "T6.1 deployment record" in readme,
        "genuine_mobile_render_observed": observed,
        "evidence": f"{STOCK_DOC} (row 実物の携帯表示), {RELAY_README}",
        "discrepancies": discrepancies,
    }


# == C8 analytics / operations ===================================================
def collect_analytics(conn, *, now: datetime) -> dict:
    runs = _rows(
        conn,
        "select id, profile, status, effective_date, started_at, finished_at, "
        "step_total, step_succeeded, step_failed from operations_runs order by id desc",
    )
    latest = {}
    for run in runs:
        latest.setdefault(run["profile"], run)
    daily = latest.get("daily")
    steps = (
        _rows(
            conn,
            "select step_name, status, error_category, rows_received from operations_step_runs "
            "where operations_run_id = :rid order by id",
            rid=daily["id"],
        )
        if daily
        else []
    )
    alerts = _rows(
        conn,
        "select id, alert_type, severity, title, status, last_seen_at, resolved_at "
        "from operations_alerts order by id",
    )
    active = [a for a in alerts if a["status"] not in ("resolved",)]
    deliveries = _rows(
        conn,
        "select notification_type, outcome, attempted_at from notification_deliveries "
        "order by id desc limit 5",
    )

    def last(sql):
        rows = _rows(conn, sql)
        return (
            {
                k: _iso(v) if k.endswith("_at") or k.endswith("date") or k == "latest" else v
                for k, v in rows[0].items()
            }
            if rows
            else None
        )

    sources = {
        "search_console": last(
            "select status, end_date, finished_at from search_console_import_runs order by id "
            "desc limit 1"
        ),
        "ga4": last(
            "select status, data_through_date, finished_at from ga4_import_runs order by id "
            "desc limit 1"
        ),
        "affiliate_clicks": last(
            "select count(*) as rows_total, max(clicked_at) as latest from "
            "affiliate_outbound_clicks"
        ),
        "affiliate_commissions": last(
            "select count(*) as rows_total, max(occurred_at) as latest from "
            "affiliate_commission_facts"
        ),
        "seo_candidates": last(
            "select created_at, candidate_count, evaluated_article_count from "
            "seo_improvement_runs order by id desc limit 1"
        ),
        "revenue_candidates": last(
            "select created_at, candidate_count, monetized_article_count from "
            "revenue_optimization_runs order by id desc limit 1"
        ),
        "indexability": {
            "latest_step": next(
                (
                    {k: v for k, v in s.items()}
                    for s in steps
                    if s["step_name"] == "check_indexability"
                ),
                None,
            )
        },
        "threads_insights": {
            "latest_step": next(
                (
                    {k: v for k, v in s.items()}
                    for s in steps
                    if s["step_name"] == "import_threads_insights"
                ),
                None,
            )
        },
    }
    return {
        "provenance": provenance(
            "local_db",
            kind="observed",
            status="ok",
            observed_at=now,
            freshness="recorded",
            detail="operations_runs / steps / alerts / import runs as recorded by the C8 pipeline",
        ),
        "latest_daily_run": {
            **{k: _iso(v) if k.endswith("_at") else v for k, v in daily.items()},
            "steps": steps,
            "alerts": [
                {
                    "type": a["alert_type"],
                    "severity": a["severity"],
                    "message": a["title"],
                    "evidence": f"operations_alerts #{a['id']}",
                    "action": "a human checks",
                }
                for a in active
            ],
        }
        if daily
        else None,
        "latest_weekly_run": {
            k: _iso(v) if k.endswith("_at") else v for k, v in latest["weekly"].items()
        }
        if latest.get("weekly")
        else None,
        "alerts": {
            "active": len(active),
            "resolved": len(alerts) - len(active),
            "recent": [
                {k: _iso(v) if k.endswith("_at") else v for k, v in a.items()} for a in alerts[-5:]
            ],
        },
        "email_notifications_recent": [
            {k: _iso(v) if k.endswith("_at") else v for k, v in d.items()} for d in deliveries
        ],
        "sources": sources,
    }


def collect_db_sections(root: Path, database_url: str, *, now: datetime, engine=None) -> dict:
    """DB から 4 つのセクションを読む。読めなければ 4 つとも「読めなかった」。"""

    try:
        engine = engine or readonly_engine(database_url)
        with engine.connect() as conn:
            return {
                "monetization": collect_monetization(conn, now=now),
                "threads": collect_threads(root, conn, now=now),
                "approvals": collect_approvals(root, conn, now=now),
                "analytics": collect_analytics(conn, now=now),
            }
    except Exception as exc:
        reason = f"database not readable ({type(exc).__name__})"
        return {
            name: {"provenance": unavailable("local_db", reason)}
            for name in ("monetization", "threads", "approvals", "analytics")
        }


def phase_discrepancies(root: Path, sections: Mapping) -> list[str]:
    """リポジトリの記述どうし / 記述と実際の値の食い違い (隠さずに出す)。"""

    out = []
    threads = sections.get("threads") or {}
    if (threads.get("policy") or {}).get("automatic_publication_enabled"):
        doc = root / "docs/operations/threads-autopublish.md"
        if doc.exists() and "本番では無効" in doc.read_text(encoding="utf-8"):
            out.append(
                "threads_operations_policy.json has automatic_publication.enabled=true, but "
                "docs/operations/threads-autopublish.md still says 本番では無効 (disabled in "
                "production)"
            )
    out += (sections.get("approvals") or {}).get("discrepancies") or []
    stock_doc = root / STOCK_DOC
    if stock_doc.exists() and "33d93394f342" in stock_doc.read_text(encoding="utf-8"):
        out.append(
            "docs/operations/threads-proposal-stock.md mentions the production DB at 33d93394f342; "
            "compare with the observed database revision"
        )
    note = (threads.get("worker") or {}).get("lock_label_note")
    if note:
        out.append(note)
    return out
