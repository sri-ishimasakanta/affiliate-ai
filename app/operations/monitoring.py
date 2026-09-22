"""監視ルール (C8、pure)。

証拠に基づいてのみアラートを出す。もっとも重要な区別:

    「まだ一度もデータが来ていない」 ≠ 「動いていたのに止まった」

GA4 は 2026-09-22 に計測を開始したばかりで、日次行はまだ届いていない。これを
「GA4 が壊れた」と通知するのは誤り。一方、**一度でも日次行が届いた実績がある**
のに閾値を超えて止まった場合は通知に値する。

同様に、Search Console には既知の報告遅延があり、遅延の範囲内の未着はアラートに
しない。公開直後のインデックス未登録も同じ理由でアラートにしない (C6 が扱う)。

DB にも外部にも触れない。入力は既に集計済みの事実だけ。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date

from app.operations.policy import PRIORITY_ORDER, OperationsPolicy

# -- alert types ---------------------------------------------------------------
IMPORT_FAILURE = "IMPORT_FAILURE"
DATA_STALE = "DATA_STALE"
ARTICLE_HTTP_HEALTH = "ARTICLE_HTTP_HEALTH"
INDEXABILITY_REGRESSION = "INDEXABILITY_REGRESSION"
MONETIZATION_STRUCTURE_REGRESSION = "MONETIZATION_STRUCTURE_REGRESSION"
CANDIDATE_CHANGE = "CANDIDATE_CHANGE"
AUTOMATION_HEALTH = "AUTOMATION_HEALTH"

ALERT_TYPES = (
    IMPORT_FAILURE,
    DATA_STALE,
    ARTICLE_HTTP_HEALTH,
    INDEXABILITY_REGRESSION,
    MONETIZATION_STRUCTURE_REGRESSION,
    CANDIDATE_CHANGE,
    AUTOMATION_HEALTH,
)

# -- candidate change classification (L) ---------------------------------------
CHANGE_NEW = "new"
CHANGE_PERSISTED = "persisted"
#: 「消えた」であって「直った」ではない。データ/期間/ポリシーの変化でも消える。
CHANGE_NO_LONGER_PRESENT = "no_longer_present"
CHANGE_PRIORITY_INCREASED = "priority_increased"
CHANGE_PRIORITY_DECREASED = "priority_decreased"


@dataclass(frozen=True)
class AlertDraft:
    """永続化前のアラート。``fingerprint`` が同一なら同じ問題として扱う。"""

    alert_type: str
    severity: str
    source: str
    title: str
    summary: str
    fingerprint: str
    article_id: int | None = None
    affiliate_program_id: int | None = None
    evidence: dict = field(default_factory=dict)


def fingerprint(*parts: object) -> str:
    """決定的な指紋。同じ問題が続く限り同じ値になる (日付は入れない)。"""

    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


# ==================== 1. import failure =======================================
def evaluate_import_failures(
    *, step_results: list[dict], policy: OperationsPolicy
) -> list[AlertDraft]:
    out: list[AlertDraft] = []
    for step in step_results:
        if step.get("status") != "failed":
            continue
        name = str(step.get("step_name"))
        out.append(
            AlertDraft(
                alert_type=IMPORT_FAILURE,
                severity=policy.severity_for(IMPORT_FAILURE),
                source=name,
                title=f"{name} failed",
                summary=(f"step {name} failed with {step.get('error_category') or 'an error'}"),
                fingerprint=fingerprint(IMPORT_FAILURE, name),
                evidence={
                    "step_name": name,
                    "error_category": step.get("error_category"),
                    "error_message": step.get("error_message"),
                    "attempt_count": step.get("attempt_count"),
                },
            )
        )
    return out


# ==================== 2. data staleness =======================================
def evaluate_data_staleness(
    *,
    source: str,
    data_through: date | None,
    ever_had_data: bool,
    today: date,
    policy: OperationsPolicy,
) -> AlertDraft | None:
    """遅延の範囲内、あるいは一度も届いていない初期状態ではアラートにしない。"""

    config = policy.import_config(source)
    stale_after = config.get("stale_after_days")
    if stale_after is None:
        return None

    if data_through is None:
        # 一度も届いていない = 初期状態。壊れた証拠ではないので黙る。
        if not ever_had_data:
            return None
        age = None
    else:
        age = (today - data_through).days
        expected = int(config.get("expected_lag_days", 0))
        if age <= max(int(stale_after), expected):
            return None

    return AlertDraft(
        alert_type=DATA_STALE,
        severity=policy.severity_for(DATA_STALE),
        source=source,
        title=f"{source} data is stale",
        summary=(
            f"{source} previously delivered data but the most recent row is "
            f"{data_through} ({age} day(s) old; gate {stale_after})"
            if data_through
            else f"{source} previously delivered data but now has none"
        ),
        fingerprint=fingerprint(DATA_STALE, source),
        evidence={
            "source": source,
            "data_through": data_through.isoformat() if data_through else None,
            "age_days": age,
            "gate_stale_after_days": stale_after,
            "ever_had_data": ever_had_data,
        },
    )


# ==================== 3./4. article health ====================================
def evaluate_article_health(*, rows: list[dict], policy: OperationsPolicy) -> list[AlertDraft]:
    """live の観測から、記事ページの明確な退行だけを拾う。

    公開直後のインデックス未登録は **退行ではない** ので扱わない (C6 の領域)。
    """

    out: list[AlertDraft] = []
    for row in rows:
        article_id = row.get("article_id")
        state = row.get("live_state")
        if state in (None, "LIVE_HEALTHY"):
            continue
        severity_type = (
            ARTICLE_HTTP_HEALTH
            if state in ("LIVE_HTTP_ERROR", "LIVE_UNREACHABLE", "LIVE_REDIRECTED")
            else INDEXABILITY_REGRESSION
        )
        out.append(
            AlertDraft(
                alert_type=severity_type,
                severity=policy.severity_for(severity_type),
                source="article_health",
                title=f"article {article_id} is {state}",
                summary=f"{row.get('url')} reported {state}: {row.get('issues') or ''}".strip(),
                fingerprint=fingerprint(severity_type, article_id, state),
                article_id=article_id,
                evidence={
                    "article_id": article_id,
                    "url": row.get("url"),
                    "live_state": state,
                    "sitemap_state": row.get("sitemap_state"),
                    "issues": row.get("issues"),
                },
            )
        )
    return out


# ==================== 5. monetization structure ===============================
def evaluate_monetization_regression(
    *, previous: dict[int, dict], current: dict[int, dict], policy: OperationsPolicy
) -> list[AlertDraft]:
    """収益化の構造が **後退した** ときだけ。``/go/`` は叩かない。"""

    out: list[AlertDraft] = []
    for article_id, before in previous.items():
        after = current.get(article_id)
        if after is None:
            continue
        lost_target = before.get("active_target_count", 0) > 0 and (
            after.get("active_target_count", 0) == 0
        )
        lost_mapping = before.get("active_mapping_count", 0) > 0 and (
            after.get("active_mapping_count", 0) == 0
        )
        if not (lost_target or lost_mapping):
            continue
        what = "affiliate target" if lost_target else "substitution mapping"
        out.append(
            AlertDraft(
                alert_type=MONETIZATION_STRUCTURE_REGRESSION,
                severity=policy.severity_for(MONETIZATION_STRUCTURE_REGRESSION),
                source="monetization",
                title=f"article {article_id} lost its active {what}",
                summary=(
                    f"article {article_id} had {before.get('active_target_count')} target(s) / "
                    f"{before.get('active_mapping_count')} mapping(s) and now has "
                    f"{after.get('active_target_count')} / {after.get('active_mapping_count')}"
                ),
                fingerprint=fingerprint(MONETIZATION_STRUCTURE_REGRESSION, article_id, what),
                article_id=article_id,
                evidence={"article_id": article_id, "before": before, "after": after},
            )
        )
    return out


# ==================== 6. candidate change =====================================
@dataclass(frozen=True)
class CandidateChange:
    change: str
    engine: str
    candidate_type: str
    dedupe_key: str
    priority: str
    previous_priority: str | None
    article_id: int | None


def compare_candidates(
    *, engine: str, previous: list[dict], current: list[dict]
) -> list[CandidateChange]:
    """append-only な候補履歴から、run 間の差分を中立な言葉で分類する。

    消えた候補を「解決した」とは呼ばない -- データ・期間・ポリシーの変化でも消える。
    """

    before = {c["dedupe_key"]: c for c in previous}
    after = {c["dedupe_key"]: c for c in current}
    changes: list[CandidateChange] = []

    for key, candidate in sorted(after.items()):
        old = before.get(key)
        if old is None:
            change = CHANGE_NEW
        elif PRIORITY_ORDER.get(candidate["priority"], 0) > PRIORITY_ORDER.get(old["priority"], 0):
            change = CHANGE_PRIORITY_INCREASED
        elif PRIORITY_ORDER.get(candidate["priority"], 0) < PRIORITY_ORDER.get(old["priority"], 0):
            change = CHANGE_PRIORITY_DECREASED
        else:
            change = CHANGE_PERSISTED
        changes.append(
            CandidateChange(
                change=change,
                engine=engine,
                candidate_type=candidate["candidate_type"],
                dedupe_key=key,
                priority=candidate["priority"],
                previous_priority=old["priority"] if old else None,
                article_id=candidate.get("article_id"),
            )
        )

    for key, candidate in sorted(before.items()):
        if key in after:
            continue
        changes.append(
            CandidateChange(
                change=CHANGE_NO_LONGER_PRESENT,
                engine=engine,
                candidate_type=candidate["candidate_type"],
                dedupe_key=key,
                priority=candidate["priority"],
                previous_priority=candidate["priority"],
                article_id=candidate.get("article_id"),
            )
        )
    return changes


def evaluate_candidate_changes(
    *, changes: list[CandidateChange], policy: OperationsPolicy
) -> list[AlertDraft]:
    """通知に値する変化だけ。低優先度の構造的候補を毎日通知しない。"""

    minimum = str(policy.gate("alerts", "notify_candidate_min_priority", "medium"))
    threshold = PRIORITY_ORDER.get(minimum, 1)
    out: list[AlertDraft] = []
    for change in changes:
        if change.change not in (CHANGE_NEW, CHANGE_PRIORITY_INCREASED):
            continue
        if PRIORITY_ORDER.get(change.priority, 0) < threshold:
            continue
        out.append(
            AlertDraft(
                alert_type=CANDIDATE_CHANGE,
                severity=policy.severity_for(CANDIDATE_CHANGE),
                source=change.engine,
                title=f"{change.change}: {change.candidate_type}",
                summary=(
                    f"{change.engine} reported {change.candidate_type} "
                    f"({change.priority}) for article {change.article_id}"
                ),
                fingerprint=fingerprint(CANDIDATE_CHANGE, change.engine, change.dedupe_key),
                article_id=change.article_id,
                evidence={
                    "engine": change.engine,
                    "change": change.change,
                    "candidate_type": change.candidate_type,
                    "priority": change.priority,
                    "previous_priority": change.previous_priority,
                    "dedupe_key": change.dedupe_key,
                    "gate_min_priority": minimum,
                },
            )
        )
    return out


# ==================== 7. automation health ====================================
def evaluate_automation_health(
    *,
    run_status: str,
    consecutive_failures: int,
    lock_conflict: bool,
    blocking_owner_run_id: int | None,
    policy: OperationsPolicy,
) -> list[AlertDraft]:
    out: list[AlertDraft] = []
    if lock_conflict:
        out.append(
            AlertDraft(
                alert_type=AUTOMATION_HEALTH,
                severity=policy.severity_for(AUTOMATION_HEALTH),
                source="operations",
                title="operations run skipped: another run holds the lock",
                summary=(
                    f"a scheduled run could not start because run {blocking_owner_run_id} "
                    "still holds the pipeline lock"
                ),
                fingerprint=fingerprint(AUTOMATION_HEALTH, "lock_conflict"),
                evidence={"blocking_owner_run_id": blocking_owner_run_id},
            )
        )
    limit = int(policy.gate("alerts", "max_consecutive_failures", 3))
    if consecutive_failures >= limit:
        out.append(
            AlertDraft(
                alert_type=AUTOMATION_HEALTH,
                severity=policy.severity_for(AUTOMATION_HEALTH),
                source="operations",
                title=f"{consecutive_failures} consecutive operations failures",
                summary=(
                    f"the last {consecutive_failures} operations runs did not succeed "
                    f"(gate {limit}); latest status is {run_status}"
                ),
                fingerprint=fingerprint(AUTOMATION_HEALTH, "consecutive_failures"),
                evidence={
                    "consecutive_failures": consecutive_failures,
                    "gate_max_consecutive_failures": limit,
                    "latest_status": run_status,
                },
            )
        )
    return out
