"""重要な事実を、出どころの強さ・観測時刻・確かさ・食い違いと一緒に並べる (pure)。

各事実は ``precedence.fact`` の形 (``value`` / ``authority`` / ``source`` / ``observed_at`` /
``confidence`` / ``conflicts``)。``conflicts`` は同じ項目について弱い出どころが違うことを
言っている食い違い (drift の finding) の要約。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.project_state.precedence import fact


def _get(state: Mapping, *path):
    node = state
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _conflicts(drift: list[dict], *fields: str) -> list[dict]:
    return [
        {
            "source": f["conflicting_source"],
            "value": f["conflicting_value"],
            "classification": f["classification"],
            "finding": f["id"],
        }
        for f in drift
        if any(f["field"] == name or f["field"].startswith(f"{name}.") for name in fields)
    ]


def _observed(state: Mapping, section: str):
    return _get(state, section, "provenance", "observed_at")


def build_facts(state: Mapping, drift: list[dict]) -> dict:
    fi = state.get("featured_images") or {}
    worker = _get(state, "threads", "worker") or {}
    record = worker.get("runtime_start") or {}
    money = state.get("monetization") or {}
    make = (
        sorted(
            m["article_id"]
            for m in money.get("monetized_articles") or []
            if "Make" in (m.get("programs") or [])
        )
        if money.get("monetized_articles") is not None
        else None
    )
    tasks = _get(state, "scheduler", "tasks")
    worker_task = (tasks or {}).get("affiliate-ai-threads-worker") or {}
    facts = {
        "current_phase": fact(
            _get(state, "project", "current_phase"),
            "current_phase",
            observed_at=_observed(state, "project"),
        ),
        "git_head": fact(_get(state, "git", "head"), "git", observed_at=_observed(state, "git")),
        "git_ahead_of_remote": fact(
            _get(state, "git", "ahead"),
            "git",
            observed_at=_observed(state, "git"),
            confidence="medium",
            source="git rev-list (as of the last fetch)",
        ),
        "db_revision": fact(
            _get(state, "database", "db_revisions"),
            "db_revision",
            observed_at=_observed(state, "database"),
            conflicts=_conflicts(drift, "database.revision"),
        ),
        "db_at_code_head": fact(
            _get(state, "database", "db_at_code_head"),
            "db_revision",
            observed_at=_observed(state, "database"),
        ),
        "wordpress_featured_images": fact(
            None
            if fi.get("with_featured_image") is None
            else f"{fi['with_featured_image']}/{fi.get('published')}",
            "wordpress",
            observed_at=_observed(state, "featured_images"),
            conflicts=_conflicts(drift, "wordpress.featured_media"),
        ),
        "wordpress_taxonomy_matches_plan": fact(
            _get(state, "taxonomy", "matches_plan"),
            "wordpress",
            observed_at=_observed(state, "taxonomy"),
            conflicts=_conflicts(drift, "wordpress.categories"),
        ),
        "make_tracked_articles": fact(
            make,
            "monetization",
            observed_at=_observed(state, "monetization"),
            conflicts=_conflicts(drift, "monetization.make_tracked_articles"),
        ),
        "threads_automatic_publication": fact(
            _get(state, "threads", "policy", "automatic_publication_enabled"),
            "threads_automatic_publication",
            observed_at=_observed(state, "threads"),
            conflicts=_conflicts(drift, "threads.automatic_publication"),
        ),
        "threads_worker_can_publish": fact(
            record.get("can_publish") if record.get("found") else None,
            "threads_worker_capabilities",
            observed_at=record.get("at"),
            confidence="high" if record.get("found") else "low",
            conflicts=_conflicts(drift, "threads.worker.can_publish"),
        ),
        "threads_worker_profile": fact(
            {
                "task_arguments": worker_task.get("arguments"),
                "publish_flags": worker.get("publish_profile_flags"),
            },
            "threads_worker_profile",
            observed_at=_observed(state, "scheduler"),
            conflicts=_conflicts(drift, "threads.worker.lock_owner"),
        ),
        "stock_maintenance_enabled": fact(
            worker.get("stock_maintenance_enabled"),
            "stock_maintenance",
            observed_at=_observed(state, "threads"),
            conflicts=_conflicts(drift, "threads.worker.stock_maintenance_enabled"),
        ),
        "approval_relay_deployed": fact(
            _get(state, "approvals", "relay_t6_1_deployment_recorded"),
            "approval_relay_deployment",
            observed_at=_observed(state, "approvals"),
            confidence="medium",
            conflicts=_conflicts(drift, "approvals.relay_deployment"),
        ),
        "scheduler_tasks": fact(
            None if tasks is None else sorted(n for n, t in tasks.items() if t.get("exists")),
            "scheduler",
            observed_at=_observed(state, "scheduler"),
        ),
        "daily_operations_state": fact(
            _get(state, "timing", "daily", "state"),
            "analytics",
            observed_at=_get(state, "timing", "as_of"),
        ),
    }
    return facts
