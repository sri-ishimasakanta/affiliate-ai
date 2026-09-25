"""どの出どころを信じるか (source precedence) と、事実の書き方 (pure)。

既定の順 (上ほど強い)。領域ごとに、もっとふさわしい出どころがあればそれを使う
(``AUTHORITY_BY_FACT``):

1. ``live_observed``: 本番の読み取り (WordPress の REST・DB の revision・スケジューラ)
2. ``runtime_config``: 本番のコードが実際に読む設定 (policy の JSON・launcher の flag)
3. ``runtime_record``: システムが書いた記録 (DB の行・worker の起動のログ・配備の記録)
4. ``committed_manifest``: コミットした機械可読の manifest・計画
5. ``operations_doc``: コミットした運用のドキュメント
6. ``historical_note``: 過去の記録の文章
7. ``prompt_expectation``: 指示や記憶にある想定 (事実としては使わない)

食い違いの分類:

- ``stale_doc``: ドキュメントが今の状態を述べているが、強い出どころと違う (ドキュメントを直す)
- ``stale_runtime_record``: 記録が古い (新しい記録で置き換わる)
- ``live_drift``: 本番の状態が、あるべき状態 (計画・規則) と違う
- ``configuration_drift``: 実行の設定が、決めたこと・契約と違う
- ``unresolved``: どちらが正しいか決められない (人が確かめる)
- ``expected_difference``: 違って見えるが意図どおり (説明を付けて info)
"""

from __future__ import annotations

AUTHORITY_LEVELS = (
    "live_observed",
    "runtime_config",
    "runtime_record",
    "committed_manifest",
    "operations_doc",
    "historical_note",
    "prompt_expectation",
)
RANK = {name: i + 1 for i, name in enumerate(AUTHORITY_LEVELS)}
CLASSIFICATIONS = (
    "stale_doc",
    "stale_runtime_record",
    "live_drift",
    "configuration_drift",
    "unresolved",
    "expected_difference",
)
# 重さ (blocking は重さと別に決める)。
SEVERITY = {
    "critical": (
        "本番の安全の約束が破れた / 書ける設定が想定外に変わった / "
        "安全な運用を止めるスキーマの食い違い"
    ),
    "high": "本番の状態が、守るべき運用の規則と違う",
    "medium": "運用の劣化や、気にかけるべき古い状態",
    "low": "観測の欠けや、急がない片付け",
    "info": "意図した設計の状態 / 情報",
}

# 事実ごとの一番強い出どころ (領域に合わせた例外を含む)。
AUTHORITY_BY_FACT = {
    "current_phase": ("committed_manifest", "docs/project-roadmap.json (evidence-checked)"),
    "git": ("live_observed", "git (local repository)"),
    "db_revision": ("live_observed", "alembic_version in the application database"),
    "wordpress": ("live_observed", "WordPress REST (read-only GET)"),
    "monetization": ("live_observed", "application database (affiliate tables)"),
    "threads_automatic_publication": (
        "runtime_config",
        "app/config/threads_operations_policy.json automatic_publication.enabled",
    ),
    "threads_worker_capabilities": (
        "runtime_record",
        "worker start-up event in the worker log (for the pid that holds the lock)",
    ),
    "threads_worker_profile": ("runtime_config", "scheduled task arguments + launcher flags"),
    "stock_maintenance": ("runtime_config", "scripts/run_threads_worker_task.cmd publish flags"),
    "approval_relay_deployment": (
        "runtime_record",
        "deployment record in the relay README (hashes, read-only verification)",
    ),
    "scheduler": ("live_observed", "Get-ScheduledTask / Get-ScheduledTaskInfo"),
    "analytics": ("runtime_record", "operations_runs / import runs written by the C8 pipeline"),
}


def fact(value, name: str, *, observed_at, confidence: str = "high", conflicts=(), source=None):
    """重要な値を、出どころ・強さ・観測時刻・確かさ・食い違いと一緒に持つ。"""

    authority, default_source = AUTHORITY_BY_FACT[name]
    return {
        "value": value,
        "authority": authority,
        "authority_rank": RANK[authority],
        "source": source or default_source,
        "observed_at": observed_at,
        "confidence": confidence,
        "conflicts": list(conflicts),
    }


def outranks(a: str, b: str) -> bool:
    """``a`` の出どころは ``b`` より強いか。"""

    return RANK[a] < RANK[b]


def finding(
    fid,
    area,
    field,
    *,
    authoritative_value,
    conflicting_value,
    authoritative_source,
    conflicting_source,
    classification,
    severity,
    blocking=False,
    recommended_resolution,
) -> dict:
    if classification not in CLASSIFICATIONS:
        raise ValueError(classification)
    if severity not in SEVERITY:
        raise ValueError(severity)
    return {
        "id": fid,
        "area": area,
        "field": field,
        "authoritative_value": authoritative_value,
        "conflicting_value": conflicting_value,
        "authoritative_source": authoritative_source,
        "conflicting_source": conflicting_source,
        "classification": classification,
        "severity": severity,
        "blocking": blocking,
        "recommended_resolution": recommended_resolution,
    }
