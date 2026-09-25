"""各セクションの出どころ・観測時刻・鮮度・状態 (pure)。

報告を読む人 (や将来のセッション) が、どこまで信じてよいかを判断できるように、主要な
セクションには必ず次を付ける:

- ``source``: 何から得たか (``live_rest`` / ``local_db`` / ``repository`` / ``runtime_report`` /
  ``local_system`` / ``local_command`` / ``derived``)
- ``kind``: 観測 (``observed``) / リポジトリの宣言 (``declared``) / 導いたもの (``derived``) /
  過去の記録 (``historical``)
- ``observed_at``: 観測した時刻 (UTC)。取れなかったら ``None``
- ``freshness``: ``fresh`` (今観測した) / ``recorded`` (記録から読んだ。記録の時刻を
  ``recorded_at`` に) / ``stale`` / ``unverified`` (読んでいない)
- ``status``: ``ok`` / ``degraded`` / ``unavailable`` / ``skipped`` (offline など) / ``error``
"""

from __future__ import annotations

from datetime import datetime

SOURCES = (
    "live_rest",
    "local_db",
    "repository",
    "runtime_report",
    "local_system",
    "local_command",
    "derived",
)
KINDS = ("observed", "declared", "derived", "historical")
FRESHNESS = ("fresh", "recorded", "stale", "unverified")
STATUSES = ("ok", "degraded", "unavailable", "skipped", "error")


def provenance(
    source: str,
    *,
    kind: str,
    status: str,
    observed_at: datetime | None,
    freshness: str,
    reason: str | None = None,
    recorded_at: str | None = None,
    detail: str | None = None,
) -> dict:
    if source not in SOURCES or kind not in KINDS or freshness not in FRESHNESS:
        raise ValueError(f"bad provenance {source!r} {kind!r} {freshness!r}")
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    out = {
        "source": source,
        "kind": kind,
        "status": status,
        "observed_at": observed_at.isoformat(timespec="seconds") if observed_at else None,
        "freshness": freshness,
    }
    if reason:
        out["reason"] = reason
    if recorded_at:
        out["recorded_at"] = recorded_at
    if detail:
        out["detail"] = detail
    return out


def unavailable(source: str, reason: str, *, kind: str = "observed") -> dict:
    return provenance(
        source,
        kind=kind,
        status="unavailable",
        observed_at=None,
        freshness="unverified",
        reason=reason,
    )


def skipped(source: str, reason: str = "offline mode: not read") -> dict:
    return provenance(
        source,
        kind="observed",
        status="skipped",
        observed_at=None,
        freshness="unverified",
        reason=reason,
    )
