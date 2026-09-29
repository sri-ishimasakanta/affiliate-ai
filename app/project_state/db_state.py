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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text

from app.project_state.provenance import provenance, unavailable
from app.project_state.runtime_records import lock_pid
from app.social.threads.queue import MAX_PUBLICATIONS_PER_CYCLE

THREADS_POLICY = Path("app/config/threads_operations_policy.json")
WORKER_LAUNCHER = Path("scripts/run_threads_worker_task.cmd")
STOCK_STATUS = Path("data/threads-generation/status.json")
GROWTH_DIRECTORY = Path("data/threads-growth")
PERFORMANCE_REPORT = Path("reports/threads_performance_diagnostic_latest.json")
STOCK_DOC = Path("docs/operations/threads-proposal-stock.md")
RELAY_README = Path("wordpress/mu-plugins/bizfluxlab-approval-relay.README.md")
WORKER_LOCK = "threads_worker"
PERFORMANCE_RERUN_NEW_6H = 3
PERFORMANCE_MAX_AGE = timedelta(days=7)
RECENT_RUNS = 7
RUN_KEYS = ("id", "status", "effective_date", "started_at", "finished_at")


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


def _has_column(conn, table: str, column: str) -> bool:
    rows = _rows(conn, f"select name from pragma_table_info('{table}')")
    return any(r["name"] == column for r in rows)


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
        #: T6.3.3: 毎日 1 本の Growth Post を用意するか (ランチャーの宣言)。
        "growth_maintenance_enabled": "--maintain-growth-posts" in flags,
    }


def pending_generation_requests(root: Path) -> list[dict]:
    """manual provider の答え待ちの依頼 (``pending/*.request.json``。読むだけ)。"""

    pending = root / STOCK_STATUS.parent / "pending"
    out = []
    for path in sorted(pending.glob("*.request.json")) if pending.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        rid = data.get("request_id") or path.name.removesuffix(".request.json")
        record = pending / f"{rid}.openai.json"
        automatic = None
        if record.exists():
            try:
                raw = json.loads(record.read_text(encoding="utf-8"))
                automatic = {"result": raw.get("result"), "fallback": raw.get("fallback")}
            except ValueError:
                automatic = {"result": "unreadable", "fallback": None}
        out.append(
            {
                "request_id": rid,
                "article_id": data.get("article_id"),
                "angles": data.get("angles"),
                "created_at": data.get("created_at"),
                "has_response": (pending / f"{rid}.response.json").exists(),
                "automatic_attempt": automatic,
            }
        )
    return out


def generation_state(stock: dict | None, pending: list[dict]) -> dict:
    """投稿案の生成の状態 (status.json と依頼のファイルから。鍵は読まない)。"""

    stock = stock or {}
    provider = (stock.get("provider") or {}).get("name") or "manual"
    generation = stock.get("generation") or {}
    if provider == "openai":
        automatic = "misconfigured" if generation.get("mode") == "misconfigured" else "enabled"
    else:
        automatic = "disabled"
    return {
        "generation_provider": provider,
        "automatic_generation": automatic,
        "generation_model": generation.get("model") if provider == "openai" else None,
        "manual_fallback_pending": sum(1 for r in pending if not r.get("has_response")),
        "last_generation_at": generation.get("last_generation_at")
        or stock.get("last_generation_attempt_at"),
        "last_generation_result": generation.get("last_generation_result"),
        "source": "data/threads-generation/status.json (last maintenance) + pending/ files",
    }


def topic_policy(conn) -> dict:
    """T6.3.2 の固定トピックの方針と、本番のコンテナ作成で実際に送った数 (読むだけ)。

    本番で "AI Threads" が受け入れられたかは、トピック付きの作成が成功した記録でだけ示す。
    """

    from app.social.threads.topic import (
        CONTENT_KIND_ACCOUNT_GROWTH,
        CONTENT_KIND_ARTICLE,
        THREADS_NORMAL_TOPIC_TAG,
        TOPIC_BY_CONTENT_KIND,
    )

    counts: Counter = Counter()
    growth_counts: Counter = Counter()
    growth_tag = TOPIC_BY_CONTENT_KIND[CONTENT_KIND_ACCOUNT_GROWTH]
    for row in _rows(
        conn,
        "select outcome, detail_json from threads_publication_attempts "
        "where step = 'create_container'",
    ):
        raw = row["detail_json"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if not (isinstance(raw, dict) and raw.get("topic_tag_sent")):
            continue
        if raw.get("content_kind") == CONTENT_KIND_ACCOUNT_GROWTH:
            # T6.3.3b: Growth のトピックの受け入れは別に数える (今の値で送ったものだけ)。
            if raw.get("topic_tag") == growth_tag:
                growth_counts[row["outcome"]] += 1
        else:
            counts[row["outcome"]] += 1
    accepted = counts.get("succeeded", 0)
    growth_accepted = growth_counts.get("succeeded", 0)
    return {
        "enabled": True,
        "normal_topic_tag": THREADS_NORMAL_TOPIC_TAG,
        "by_content_kind": dict(TOPIC_BY_CONTENT_KIND),
        "normal_content_kind": CONTENT_KIND_ARTICLE,
        # T6.3.3b: Growth Post にも固定のトピック (インサイト祭り)。
        "growth_topic_tag": TOPIC_BY_CONTENT_KIND[CONTENT_KIND_ACCOUNT_GROWTH],
        "selection": "deterministic by content kind (not Luna, hook, angle, category, link)",
        "api_field": "topic_tag (POST /{threads-user-id}/threads)",
        "fail_closed": True,
        "alters_body_hash_or_character_count": False,
        "tagged_container_attempts": sum(counts.values()),
        "tagged_containers_accepted": accepted,
        "tagged_containers_rejected": counts.get("failed", 0),
        "production_acceptance": "observed" if accepted else "pending_canary",
        "growth_tagged_containers_accepted": growth_accepted,
        "growth_tagged_containers_rejected": growth_counts.get("failed", 0),
        "growth_topic_production_acceptance": "observed" if growth_accepted else "pending_canary",
    }


OBSERVER_TABLES = ("threads_observer_runs", "threads_external_posts",
                   "threads_external_observations", "threads_trending_topics")  # fmt: skip


def _collection_policy() -> dict:
    """T6.5B.2 の観察の集め方 (方針のファイル。読むだけ)。"""

    try:
        from app.social.threads.observer.planning import load_policy

        policy = load_policy()
    except Exception as exc:  # noqa: BLE001 - 読めないときは読めなかったとだけ言う
        return {"collection_policy": {"available": False, "reason": type(exc).__name__}}
    unique = policy["unique_posts"]
    return {"collection_policy": {
        "available": True,
        "version": policy["policy_version"],
        "unique_soft_min": unique["soft_min"],
        "unique_target": unique["target"],
        "unique_hard_max": unique["hard_max"],
        "rollout_stage": policy["rollout"]["current_stage"],
        "next_stage_requires_approval": True,
        "repeat_observation_enabled": policy["repeat_observation"]["enabled"],
    }}  # fmt: skip


def observer_state(conn) -> dict:
    """T6.5B の外の観察 (読むだけ)。表が無い DB (migration 2cfa0ccb2059 の前) では数えない。"""

    from app.social.threads.observer import selectors as sel

    names = {r["name"] for r in _rows(conn, "select name from sqlite_master where type='table'")}
    present = all(t in names for t in OBSERVER_TABLES)
    base = {
        "schema_ready": present,
        "read_only": True,
        "social_actions": False,
        "scheduled": False,
        "fed_back_to_generation": False,
        "collector_version": sel.COLLECTOR_VERSION,
        "selector_version": sel.SELECTOR_VERSION,
        "selector_verified": sel.SELECTOR_VERIFIED,
        "limits": dict(sel.LIMITS),
        # 2026-09-28 に画面で確認: Web のトピックの一覧は、このアカウント向けの「おすすめの
        # トピック」(topic_for_you)。世の中のトレンドの順位ではない。本物のトレンドは未確認。
        "topic_list_semantics": "topic_for_you (personalized topic suggestions for this account)",
        "global_trending": "unverified",
        "surfaces_verified": sorted(sel.SURFACE_VERIFICATION),
        **_collection_policy(),
    }
    if not present:
        return base
    runs = _rows(conn, "select status, count(*) as n from threads_observer_runs group by status")
    last = _rows(conn, "select started_at, status from threads_observer_runs "
                       "order by started_at desc, id desc limit 1")  # fmt: skip
    counts = _rows(
        conn,
        "select (select count(*) from threads_external_posts) as posts, "
        "(select count(*) from threads_external_observations) as observations, "
        "(select count(*) from threads_trending_topics) as trending_topics",
    )[0]
    return {
        **base,
        "runs_by_status": {r["status"]: r["n"] for r in runs},
        "last_run": {"started_at": str(last[0]["started_at"]), "status": last[0]["status"]}
        if last else None,
        **counts,
    }  # fmt: skip


def growth_state(conn, root: Path, *, now: datetime | None = None) -> dict:
    """T6.3.3 の Growth Post の方針と、提案・公開・フォロワーの観測 (読むだけ)。"""

    from app.social.threads.growth import (
        FOLLOWER_OBSERVATION_MAX_AGE,
        GROWTH_ELIGIBLE_FROM,
        GROWTH_FOLLOWER_TARGET,
        GROWTH_POLICY_VERSION,
        GROWTH_POST_TARGET_PER_JST_DAY,
        GROWTH_POSTS_ENABLED,
        FollowerObservation,
        target_reached,
    )
    from app.social.threads.topic import CONTENT_KIND_ACCOUNT_GROWTH, TOPIC_BY_CONTENT_KIND

    columns = _rows(
        conn, "select name, \"notnull\" from pragma_table_info('threads_post_proposals')"
    )
    nullable = any(c["name"] == "source_article_id" and not c["notnull"] for c in columns)
    proposals = (
        _rows(conn, "select id, status, learning_guidance_json from threads_post_proposals "
                    "where source_article_id is null")  # fmt: skip
        if nullable
        else []
    )
    by_status = Counter(p["status"] for p in proposals)
    latest = None
    for row in sorted(proposals, key=lambda r: r["id"], reverse=True)[:1]:
        raw = row["learning_guidance_json"]
        meta = (json.loads(raw) if isinstance(raw, str) else raw or {}).get("growth") or {}
        latest = {"id": row["id"], "status": row["status"], "date_jst": meta.get("date_jst"),
                  "angle": meta.get("angle")}  # fmt: skip
    published = (
        _rows(conn, "select count(*) as n from threads_publications "
                    "where source_article_id is null and status = 'published'")[0]["n"]  # fmt: skip
        if nullable
        else 0
    )

    def _json(name):
        path = root / GROWTH_DIRECTORY / name
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except ValueError:
            return None

    observation = FollowerObservation.from_dict(_json("followers.json"))
    posts_today = None
    if now is not None and _has_column(conn, "threads_publications", "source_article_id"):
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Tokyo")
        day = now.astimezone(tz).date()
        counts = Counter()
        sql = (
            "select source_article_id, published_at from threads_publications "
            "where status = 'published' and published_at is not null"
        )
        for row in _rows(conn, sql):
            moment = _parse(row["published_at"])
            if moment is not None and moment.astimezone(tz).date() == day:
                counts["growth" if row["source_article_id"] is None else "article"] += 1
        posts_today = {
            "date_jst": day.isoformat(),
            "article_posts_today": counts["article"],
            "growth_posts_today": counts["growth"],
            "total_posts_today": counts["article"] + counts["growth"],
        }
    last = _json("status.json") or {}
    return {
        "policy": {
            "version": GROWTH_POLICY_VERSION,
            "enabled_by_policy": GROWTH_POSTS_ENABLED,
            "per_jst_day": GROWTH_POST_TARGET_PER_JST_DAY,
            "eligible_from_jst": GROWTH_ELIGIBLE_FROM.strftime("%H:%M"),
            "supplemental_to_article_cadence": True,
            "no_catch_up": True,
            "follower_target": GROWTH_FOLLOWER_TARGET,
            "manual_follow_back_by_user": True,
            "human_approval": True,
            "topic_tag": TOPIC_BY_CONTENT_KIND[CONTENT_KIND_ACCOUNT_GROWTH],
            "link_mode": "none",
            "target_reached_pauses_for_human": True,
            # T6.3.3a: 足し分の公開の枠 (記事の 120 分の間隔と 1 回 1 本の枠を使わない)。
            "supplemental_publication_lane": True,
            "article_gap_applies": False,
            "article_cycle_cap_applies": False,
            "failure_isolated_from_article_queue": True,
            "follower_observation_max_age_hours": FOLLOWER_OBSERVATION_MAX_AGE.total_seconds()
            / 3600,
        },
        "schema_ready": nullable,
        "proposals": len(proposals),
        "proposals_by_status": dict(sorted(by_status.items())),
        "latest_proposal": latest,
        "published": published,
        "follower_observation": observation.as_dict() if observation else None,
        "follower_target_reached": target_reached(observation, GROWTH_FOLLOWER_TARGET),
        #: 3〜5 本の目安は記事の投稿だけ。Growth Post は足し分として別に数える (JST の今日)。
        "posts_today": posts_today,
        "last_maintenance": {k: last.get(k) for k in ("date_jst", "due", "reason", "created",
                                                      "written_at", "follower_read")}
        if last else None,  # fmt: skip
    }


def conversation_state(conn) -> dict:
    """会話のきっかけ (T6.3) の方針と、提案のきっかけ別の数 (T6.3 より前は legacy)。"""

    from app.social.threads.conversation import BRIEF_VERSION, HOOKS, hook_from_provenance

    counts: Counter = Counter()
    source = (
        "source_article_id"
        if _has_column(conn, "threads_post_proposals", "source_article_id")
        else "1 as source_article_id"
    )
    for row in _rows(
        conn, f"select {source}, learning_guidance_json from threads_post_proposals"
    ):
        raw = row["learning_guidance_json"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if row["source_article_id"] is None:
            # T6.3.3: Growth Post はきっかけの集計に混ぜない (別の行)。
            counts["account_growth"] += 1
            continue
        counts[hook_from_provenance(raw if isinstance(raw, dict) else None)] += 1
    return {
        "conversation_style": "supported",
        "conversation_hook_policy": {
            "brief_version": BRIEF_VERSION,
            "hooks": list(HOOKS),
            "selection": "deterministic 1-in-5 bucket per request (SHA-256); no randomness",
            "target": "about 4 in 5 with a conversation hook, about 1 in 5 none",
            "self_optimizing": False,
            "reach_guarantee": False,
        },
        "proposals_by_conversation_hook": dict(sorted(counts.items())),
        "quality_policy": quality_policy(),
    }


def quality_policy() -> dict:
    """T6.3.1 の質の方針 (コードの定数から。鍵や本文は読まない)。"""

    from app.social.threads.quality import (
        PREFERRED_RANGE,
        QUALITY_CEILING,
        QUALITY_VERSION,
        RECENT_WINDOW,
    )

    return {
        "version": QUALITY_VERSION,
        "prose_target_chars": list(PREFERRED_RANGE),
        "prose_ceiling_before_repair": QUALITY_CEILING,
        "hard_platform_ceiling": 500,
        "one_main_point": True,
        "hook_semantics_enforced": True,
        "recent_topic_window": RECENT_WINDOW,
        "active_hook_target": "about 4 in 5 (unchanged)",
        "self_tuning": False,
        "per_call_audit": True,
        "repair_reason_persisted": True,
        "link_mode_binding": True,
        "overlap_decisions_audited": True,
    }


def generation_audit(root: Path) -> dict:
    """自動生成の呼び出しの記録から数える (読むだけ)。本番で重なりを止めた例があるか。"""

    base = root / STOCK_STATUS.parent
    records = legacy = with_history = repairs = overlap_blocks = link_mismatches = 0
    for path in sorted(base.glob("*/*.openai.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        records += 1
        history = data.get("history")
        if not history:
            legacy += 1
            continue
        with_history += 1
        repairs += int(data.get("repair_calls") or 0)
        ids = {
            rid
            for call in history
            for rid in (call.get("repair_reason_ids") or [])
            + ((call.get("validation") or {}).get("reason_ids") or [])
        }
        overlap_blocks += int("recent_topic_overlap" in ids)
        link_mismatches += int("link_mode_mismatch" in ids)
    return {
        "records": records,
        "legacy_records_without_call_history": legacy,
        "records_with_call_history": with_history,
        "repair_calls": repairs,
        "production_overlap_blocks": overlap_blocks,
        "production_overlap_block_observed": overlap_blocks > 0,
        "link_mode_mismatches": link_mismatches,
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
    last_24h = [
        p
        for p in published
        if (t := _parse(p["remote_timestamp"]) or _parse(p["published_at"]))
        and timedelta(0) <= now - t <= timedelta(hours=24)
    ]
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
            lock_pid=lock_pid(row["owner_label"]),
            lock_acquired_at=_iso(row["acquired_at"]),
            heartbeat_at=_iso(row["heartbeat_at"]),
            heartbeat_age_seconds=round(age) if age is not None else None,
            heartbeat_max_seconds=max_age,
            running=row["released_at"] is None and age is not None and age <= max_age,
            lock_label_note=(
                "owner_label mode=plan names the worker core's only mode; publishing is a "
                "capability (flags + policy), see docs/operations/threads-worker.md"
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
            "policy_note": auto.get("note"),
            "daily_activity_target": policy.get("daily_activity_target"),
            "max_publications_per_cycle": MAX_PUBLICATIONS_PER_CYCLE,
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
            "published_last_24h": len(last_24h),
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
        "generation": {
            **generation_state(stock, pending_generation_requests(root)),
            **conversation_state(conn),
            "audit": generation_audit(root),
        },
        "topic": topic_policy(conn),
        "growth": growth_state(conn, root, now=now),
        "observer": observer_state(conn),
        "stock": {
            "pending_generation_requests": pending_generation_requests(root),
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

    def recent(profile):
        return [
            {k: _iso(r[k]) if k.endswith("_at") else r[k] for k in RUN_KEYS}
            for r in runs
            if r["profile"] == profile
        ][:RECENT_RUNS]

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
        "recent_daily_runs": recent("daily"),
        "recent_weekly_runs": recent("weekly"),
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
