"""SaaS の検証の証拠のまとめ (N7。pure)。**決めない。作らない。**

実際の試しの利用者 (仮名 ``pilot-xx``) について手で記録した数 (``manual_metric_entries``、
``subject_kind = pilot``) だけから、go / no-go の条件ごとに「満たす / 満たさない / 証拠が
足りない」を出す。条件の値は ``app/config/pilot_policy.json`` (試しの前に人が決める案)。

- 試しの数が ``min_pilots`` に届かない・その条件の数が無い → ``insufficient``。
- 全体: 証拠が足りない / 証拠は go を支える / 証拠は go に反する / 混ざっている。
  **どれも人の判断の材料であって判断ではない。**
- SaaS / Managed Service / Hybrid の兆し: 始めやすさ・手助けの量・価値の評価から、どちらに
  向くかを書くだけ (決めるのは人)。
- 基準の固定 (2026-10-01): 人が基準を確かめたら、その時の中身の hash を確認の記録
  (``pilot_policy_confirmations.json``、追記だけ) に残す。記録の hash と今の基準の hash が
  同じときだけ ``confirmed``。確認の後に基準が変われば ``changed_after_confirmation`` (黙って
  書き換えられない)。``status`` の欄は表示だけで、確認の記録が正。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from statistics import median

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "pilot_policy.json"
CONFIRMATIONS_PATH = POLICY_PATH.with_name("pilot_policy_confirmations.json")
CONFIRMATIONS_SCHEMA = "pilot-policy-confirmations/1"
#: 基準の中身 (hash に入るもの)。``status`` と ``note`` は説明で、中身ではない。
POLICY_CONTENT_KEYS = ("policy_version", "min_pilots", "criteria", "model_signals")


def policy_hash(policy: dict) -> str:
    content = {k: policy.get(k) for k in POLICY_CONTENT_KEYS}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()  # fmt: skip


def load_confirmations(path: Path | None = None) -> list[dict]:
    path = path or CONFIRMATIONS_PATH
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != CONFIRMATIONS_SCHEMA:
        raise ValueError(f"{path} is not {CONFIRMATIONS_SCHEMA}")
    return list(data.get("records") or [])


def policy_identity(policy: dict, confirmations: list[dict]) -> dict:
    """今の基準の版・hash と、人の確認の状態 (確認の記録が正)。"""

    current = policy_hash(policy)
    match = next((r for r in reversed(confirmations) if r["policy_hash"] == current), None)
    state = ("confirmed" if match else
             "changed_after_confirmation" if confirmations else "proposed")
    return {"policy_version": policy.get("policy_version"), "policy_hash": current,
            "state": state, "file_status": policy.get("status"),
            "confirmed_at": match and match["confirmed_at"],
            "confirmed_by": match and match["confirmed_by"],
            "confirmations": len(confirmations)}  # fmt: skip


def confirm_policy(policy: dict, confirmations: list[dict], *, policy_hash_given: str, by: str,
                   now: datetime, note: str | None = None) -> dict:
    """人の基準の確認の記録を作る (書くのは呼ぶ側)。今の hash と同じ hash を人が渡す。"""

    current = policy_hash(policy)
    if policy_hash_given != current:
        raise ValueError("the confirmation is for a different policy hash (the policy changed)")
    if any(r["policy_hash"] == current for r in confirmations):
        raise ValueError("this policy is already confirmed")
    who = (by or "").strip()
    if not who or "@" in who:
        raise ValueError("by is a short name (no email)")
    if now.tzinfo is None:
        raise ValueError("now needs a timezone")
    return {"policy_version": policy.get("policy_version"), "policy_hash": current,
            "confirmed_at": now.isoformat(timespec="seconds"), "confirmed_by": who[:64],
            "criteria_snapshot": {k: policy.get(k) for k in POLICY_CONTENT_KEYS},
            "note": (note or "").strip()[:300] or None,
            "meaning": "the human confirmed these go / no-go thresholds before reading real "
                       "pilot results; not a go / no-go decision"}  # fmt: skip


def load_policy(path: Path | None = None) -> dict:
    return json.loads((path or POLICY_PATH).read_text(encoding="utf-8"))


def latest_by_pilot(rows: Iterable[dict]) -> dict[str, dict[str, float]]:
    """pilot ごと・指標ごとの最新の値 (上書きされた行は除いてから渡す)。"""

    out: dict[str, dict[str, float]] = {}
    for r in sorted((r for r in rows if r["subject_kind"] == "pilot"),
                    key=lambda r: (r["observed_at"], r["id"])):  # fmt: skip
        out.setdefault(r["subject_ref"], {})[r["metric"]] = r["value"]
    return out


def _rate(values: list[float], predicate) -> float | None:
    return round(sum(1 for v in values if predicate(v)) / len(values), 2) if values else None


def evaluate(rows: Iterable[dict], *, policy: dict) -> dict:
    pilots = latest_by_pilot(rows)
    n = len(pilots)
    minimum = int(policy.get("min_pilots", 3))
    c = policy["criteria"]

    def values(metric: str) -> list[float]:
        return [m[metric] for m in pilots.values() if metric in m]

    def criterion(name, metric, measure, ok, *, threshold):
        got = values(metric)
        if n < minimum or len(got) < minimum:
            return name, {"status": "insufficient", "n": len(got), "threshold": threshold}
        value = measure(got)
        return name, {"status": "met" if ok(value) else "not_met", "value": value,
                      "n": len(got), "threshold": threshold}  # fmt: skip

    results = dict([
        criterion("activation", "activated", lambda v: _rate(v, lambda x: x == 1),
                  lambda r: r >= c["activation_rate_min"], threshold=c["activation_rate_min"]),
        criterion("time_to_first_value", "time_to_first_value_hours", median,
                  lambda m: m <= c["time_to_first_value_hours_median_max"],
                  threshold=c["time_to_first_value_hours_median_max"]),
        criterion("workflow_completion", "workflows_completed",
                  lambda v: _rate(v, lambda x: x >= 1),
                  lambda r: r >= c["workflow_completion_rate_min"],
                  threshold=c["workflow_completion_rate_min"]),
        criterion("repeat_usage", "active_days",
                  lambda v: _rate(v, lambda x: x >= c["repeat_usage_active_days_min"]),
                  lambda r: r >= c["repeat_usage_rate_min"], threshold=c["repeat_usage_rate_min"]),
        criterion("onboarding_difficulty", "onboarding_difficulty", median,
                  lambda m: m <= c["onboarding_difficulty_median_max"],
                  threshold=c["onboarding_difficulty_median_max"]),
        criterion("support_burden", "support_minutes", median,
                  lambda m: m <= c["support_minutes_median_max"],
                  threshold=c["support_minutes_median_max"]),
        criterion("value", "value_rating", median,
                  lambda m: m >= c["value_rating_median_min"],
                  threshold=c["value_rating_median_min"]),
        criterion("would_continue", "would_continue", lambda v: _rate(v, lambda x: x == 1),
                  lambda r: r >= c["would_continue_rate_min"],
                  threshold=c["would_continue_rate_min"]),
    ])  # fmt: skip
    # 費用は 2 つとも記録した利用者だけ (片方が無いのを 0 にしない。2026-10-01)
    costs = [m["operating_cost_jpy"] + m["api_cost_jpy"] for m in pilots.values()
             if "operating_cost_jpy" in m and "api_cost_jpy" in m]
    wtp = values("willingness_to_pay_jpy")
    if n < minimum or len(costs) < minimum or len(wtp) < minimum:
        results["cost_vs_willingness_to_pay"] = {"status": "insufficient",
                                                 "n": min(len(costs), len(wtp))}
    else:
        results["cost_vs_willingness_to_pay"] = {
            "status": "met" if median(costs) <= median(wtp) else "not_met",
            "value": {"cost_median_jpy": median(costs), "wtp_median_jpy": median(wtp)},
            "n": min(len(costs), len(wtp))}  # fmt: skip
    statuses = [r["status"] for r in results.values()]
    if n < minimum or "insufficient" in statuses:
        overall = "insufficient_evidence"
    elif all(s == "met" for s in statuses):
        overall = "evidence_supports_go"
    elif statuses.count("not_met") > len(statuses) / 2:
        overall = "evidence_against_go"
    else:
        overall = "mixed"
    return {"pilots": n, "min_pilots": minimum, "criteria": results, "overall": overall,
            "model_signals": _signals(pilots, policy, n >= minimum),
            "policy_status": policy.get("status"),
            "note": "evidence summary for the human go / no-go and SaaS / Managed / Hybrid "
                    "decision; not a decision; no usage is estimated"}  # fmt: skip


def _signals(pilots: dict, policy: dict, enough: bool) -> dict:
    if not enough:
        return {"state": "insufficient_evidence"}
    s = policy["model_signals"]
    diff = [m["onboarding_difficulty"] for m in pilots.values() if "onboarding_difficulty" in m]
    support = [m["support_minutes"] for m in pilots.values() if "support_minutes" in m]
    value = [m["value_rating"] for m in pilots.values() if "value_rating" in m]
    if not (diff and support and value):
        return {"state": "insufficient_evidence"}
    self_service = (median(diff) <= s["self_service_onboarding_difficulty_max"]
                    and median(support) <= s["self_service_support_minutes_max"])
    managed = median(value) >= s["managed_value_rating_min"] and (
        median(diff) >= s["managed_onboarding_difficulty_min"]
        or median(support) >= s["managed_support_minutes_min"])
    return {"state": "described", "self_service_signal": self_service,
            "managed_signal": managed,
            "reading": ("both: consider Hybrid" if self_service and managed else
                        "SaaS-leaning" if self_service else
                        "Managed-leaning" if managed else "no clear signal")}  # fmt: skip


__all__ = ["CONFIRMATIONS_PATH", "POLICY_CONTENT_KEYS", "confirm_policy", "evaluate",
           "latest_by_pilot", "load_confirmations", "load_policy", "policy_hash",
           "policy_identity"]
