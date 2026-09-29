"""システムの健康の点検 (C10-3 / C10-F、``system-health/1``、pure)。

「正常」は知らせない。行動が要る問題 (warning / error) だけを警告にし、同じ問題は同じ指紋
(``fingerprint("C10_HEALTH", check, subject)``、日付を含めない) で 1 つにまとめる。info は
要約に出すだけ。数字は ``app/config/system_health_policy.json`` にだけ置く。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from app.operations.monitoring import AlertDraft, fingerprint

SCHEMA = "system-health/1"
SOURCE = "c10_health"
ALERT_TYPE = "SYSTEM_HEALTH"
POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "system_health_policy.json"
ACTIONABLE = frozenset({"warning", "error"})


@lru_cache(maxsize=1)
def load_health_policy(path: str | None = None) -> dict:
    return json.loads(Path(path or POLICY_PATH).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Finding:
    check: str
    category: str  # nightly / worker / data / workflow / db
    severity: str
    title: str
    summary: str
    subject: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.severity in ACTIONABLE

    @property
    def fingerprint(self) -> str:
        return fingerprint("C10_HEALTH", self.check, self.subject)

    def as_draft(self) -> AlertDraft:
        return AlertDraft(alert_type=ALERT_TYPE, severity=self.severity, source=SOURCE,
                          title=self.title, summary=self.summary, fingerprint=self.fingerprint,
                          evidence={"check": self.check, "category": self.category,
                                    **self.evidence})

    def as_dict(self) -> dict:
        return {**asdict(self), "actionable": self.actionable, "fingerprint": self.fingerprint}


def _severity(policy: dict, check: str) -> str:
    return (policy.get("severity") or {}).get(check, "info")


def finding(policy: dict, check: str, category: str, title: str, summary: str,
            subject: str = "", **evidence) -> Finding:  # fmt: skip
    return Finding(check, category, _severity(policy, check), title, summary, subject, evidence)


# == nightly =======================================================================================
def nightly_findings(policy: dict, *, now_local: datetime, schedule_time: str,
                     registered: bool, last_run: dict | None, last_success_at: datetime | None,
                     eligible: int | None, budget: int) -> list[Finding]:
    """夜の分析の点検 (``now_local`` は運用のタイムゾーンの今)。"""

    cfg = policy.get("nightly") or {}
    out = []
    hh, mm = (int(x) for x in schedule_time.split(":"))
    due = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    grace = timedelta(hours=float(cfg.get("grace_hours_after_schedule", 3)))
    today_key = f"nightly:{now_local.date().isoformat()}"
    if last_run is None:
        out.append(finding(policy, "nightly_never_run", "nightly", "Nightly analysis never ran",
                           "no nightly_analysis_runs row yet"))
        return out
    if last_run.get("status") == "failed":
        out.append(finding(policy, "nightly_failed", "nightly", "Nightly analysis failed",
                           f"{last_run.get('run_key')}: {last_run.get('failure_reason')}",
                           run_key=last_run.get("run_key")))
    if registered and now_local >= due + grace and not (
            last_run.get("run_key") == today_key and last_run.get("status") == "succeeded"):
        out.append(finding(policy, "nightly_missed", "nightly", "Nightly analysis missed",
                           f"no successful run for {today_key} by {(due + grace):%H:%M}",
                           expected=today_key))
    stale = timedelta(hours=float(cfg.get("stale_after_hours", 36)))
    if last_success_at is not None and now_local - last_success_at > stale:
        out.append(finding(policy, "nightly_stale", "nightly", "Nightly analysis is stale",
                           f"last success {last_success_at.isoformat()}"))
    factor = float(cfg.get("explosion_factor", 3))
    if eligible is not None and eligible > budget * factor:
        out.append(finding(policy, "nightly_candidate_explosion", "nightly",
                           "Nightly candidate explosion",
                           f"{eligible} eligible candidates for a budget of {budget}"))
    return out


# == worker ========================================================================================
def worker_findings(policy: dict, *, now: datetime, lock: dict | None,
                    log_errors: int) -> list[Finding]:
    cfg = policy.get("worker") or {}
    out = []
    limit = timedelta(minutes=float(cfg.get("stopped_after_minutes", 45)))
    if lock is None:
        out.append(finding(policy, "worker_stopped", "worker", "Threads worker has no lock",
                           "no threads_worker lock row"))
    else:
        beat = lock.get("heartbeat_at")
        if lock.get("released_at") is not None and (beat is None or now - beat > limit):
            out.append(finding(policy, "worker_stopped", "worker", "Threads worker stopped",
                               f"lock released at {lock['released_at']}"))
        elif beat is not None and now - beat > limit:
            out.append(finding(policy, "worker_stopped", "worker", "Threads worker stopped",
                               f"last heartbeat {beat.isoformat()} (> {limit})"))
    if log_errors:
        out.append(finding(policy, "worker_log_errors", "worker", "Threads worker errors",
                           f"{log_errors} ERROR line(s) in the last "
                           f"{cfg.get('log_error_window_hours', 24)} hours"))
    return out


__all__ = ["ACTIONABLE", "ALERT_TYPE", "Finding", "SCHEMA", "SOURCE", "finding",
           "load_health_policy", "nightly_findings", "worker_findings"]
