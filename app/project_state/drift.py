"""食い違いの検出 (drift。pure・決定論的。本番を直さない)。

報告のセクション (観測・設定・記録) を突き合わせ、食い違いを ``precedence.finding`` の形で
出す。どれが正しいかは ``precedence`` の順で決める。食い違いを見つけても本番は変えない
(ドキュメントの食い違いは ``stale_doc``、意図した違いは ``expected_difference``)。

見るもの:

- 自動公開: policy (実行の設定) と、ロックを持つ worker の起動の記録 (公開できるか)
- worker のロックの ``mode=plan`` の表示と、ロックの pid の起動の記録
- 在庫の保守: launcher の flag と、決定 (OFF)
- 収益化: Make の tracking (決定の記録: article 1・10・11) と DB
- WordPress: featured image (W1 の manifest) とカテゴリ (W2 の計画)
- ドキュメント: ``docs_health.check_claims`` (今の状態を述べる文)
"""

from __future__ import annotations

from collections.abc import Mapping

from app.project_state.precedence import finding

# 決定の記録 (docs/decision-log) にある、Make の tracking を持つ記事。
MAKE_TRACKED_ARTICLES = (1, 10, 11)


def _get(state: Mapping, *path):
    node = state
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _auto_publish(state: Mapping) -> list[dict]:
    enabled = _get(state, "threads", "policy", "automatic_publication_enabled")
    worker = _get(state, "threads", "worker") or {}
    record = worker.get("runtime_start") or {}
    if enabled is None or not worker.get("running"):
        return []
    if not record.get("found"):
        return [
            finding(
                "threads-worker-start-record-missing",
                "threads",
                "threads.worker.can_publish",
                authoritative_value=f"policy automatic_publication.enabled={enabled}",
                conflicting_value="no start-up event for the lock pid in the worker log tail",
                authoritative_source="threads_operations_policy.json",
                conflicting_source=record.get("source") or "worker log",
                classification="unresolved",
                severity="low",
                recommended_resolution=(
                    "read the worker log by hand; do not restart the worker to refresh the record"
                ),
            )
        ]
    can_publish = record.get("can_publish")
    policy_seen = record.get("auto_publish_policy")
    consistent = bool(can_publish) == bool(enabled) and policy_seen in (
        None,
        "enabled" if enabled else "disabled",
    )
    if can_publish is None or consistent:
        return []
    dangerous = bool(can_publish) and not enabled
    return [
        finding(
            "threads-auto-publish-effective-mismatch",
            "threads",
            "threads.automatic_publication",
            authoritative_value=f"policy enabled={enabled}",
            conflicting_value=(
                f"running worker pid={record.get('pid')} can_publish={can_publish} "
                f"(policy seen at start: {policy_seen})"
            ),
            authoritative_source="threads_operations_policy.json automatic_publication.enabled",
            conflicting_source=record.get("source") or "worker log",
            classification="configuration_drift",
            severity="critical" if dangerous else "medium",
            blocking=dangerous,
            recommended_resolution=(
                "a human decides; T7 never restarts the worker or changes the policy"
            ),
        )
    ]


def _worker_lock(state: Mapping) -> list[dict]:
    worker = _get(state, "threads", "worker") or {}
    label = worker.get("lock_owner") or ""
    out = []
    if "mode=plan" in label:
        record = worker.get("runtime_start") or {}
        out.append(
            finding(
                "threads-worker-lock-label-mode-plan",
                "threads",
                "threads.worker.lock_owner",
                authoritative_value=(
                    f"capabilities {record.get('capabilities')} can_publish="
                    f"{record.get('can_publish')} (start-up record)"
                    if record.get("found")
                    else "capabilities come from the flags + policy, not the label"
                ),
                conflicting_value=label,
                authoritative_source="worker start-up event (runtime record)",
                conflicting_source="operations_locks.owner_label",
                classification="expected_difference",
                severity="info",
                recommended_resolution=(
                    "none: mode=plan names the worker core's only mode; publishing is a "
                    "capability (docs/operations/threads-worker.md, lock label)"
                ),
            )
        )
    return out


def _stock(state: Mapping) -> list[dict]:
    enabled = _get(state, "threads", "worker", "stock_maintenance_enabled")
    if enabled is not True:
        return []
    return [
        finding(
            "threads-stock-maintenance-on",
            "threads",
            "threads.worker.stock_maintenance_enabled",
            authoritative_value="OFF (decision threads-stock-maintenance-off)",
            conflicting_value="--maintain-proposal-stock is in the publish launcher flags",
            authoritative_source="docs/decision-log (threads-stock-maintenance-off)",
            conflicting_source="scripts/run_threads_worker_task.cmd",
            classification="configuration_drift",
            severity="high",
            recommended_resolution="a human confirms the routine decision or removes the flag",
        )
    ]


def _monetization(state: Mapping) -> list[dict]:
    money = state.get("monetization") or {}
    if money.get("monetized_articles") is None:
        return []
    make = sorted(
        m["article_id"] for m in money["monetized_articles"] if "Make" in (m.get("programs") or [])
    )
    if make == list(MAKE_TRACKED_ARTICLES):
        return []
    return [
        finding(
            "monetization-make-tracking",
            "monetization",
            "monetization.make_tracked_articles",
            authoritative_value=make,
            conflicting_value=list(MAKE_TRACKED_ARTICLES),
            authoritative_source="application database (active targets + mappings)",
            conflicting_source="docs/decision-log (make-tracking-articles-1-10-11)",
            classification="live_drift",
            severity="medium",
            recommended_resolution=(
                "check the tracking targets by hand; do not invent or copy tracking data"
            ),
        )
    ]


def _wordpress(state: Mapping) -> list[dict]:
    out = []
    fi = state.get("featured_images") or {}
    if fi.get("declared_vs_live_disagreements"):
        out.append(
            finding(
                "wp-featured-image-drift",
                "featured_images",
                "wordpress.featured_media",
                authoritative_value="live featured_media",
                conflicting_value=fi["declared_vs_live_disagreements"],
                authoritative_source="WordPress REST (read-only)",
                conflicting_source="W1.4 / W1.5 manifests",
                classification="live_drift",
                severity="high",
                blocking=True,
                recommended_resolution="investigate before any featured-image write",
            )
        )
    tax = state.get("taxonomy") or {}
    if tax.get("matches_plan") is False:
        out.append(
            finding(
                "wp-taxonomy-drift",
                "taxonomy",
                "wordpress.categories",
                authoritative_value="live post categories",
                conflicting_value=tax.get("assignment_mismatches"),
                authoritative_source="WordPress REST (read-only)",
                conflicting_source="app/wordpress/taxonomy_plan.py",
                classification="live_drift",
                severity="high",
                blocking=True,
                recommended_resolution="investigate before any taxonomy change",
            )
        )
    return out


def detect_drift(state: Mapping, *, doc_findings: list[dict] = ()) -> list[dict]:
    found = [
        *_auto_publish(state),
        *_worker_lock(state),
        *_stock(state),
        *_monetization(state),
        *_wordpress(state),
        *doc_findings,
    ]
    unique = {f["id"]: f for f in found}
    return sorted(unique.values(), key=lambda f: (not f["blocking"], f["area"], f["id"]))
