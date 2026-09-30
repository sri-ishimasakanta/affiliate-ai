"""手で写す数の目録と検査・要約 (N3。pure)。

目録はこのプロジェクト自身の言葉だけ (provider の書き出しの形を仮定しない)。数は人が画面
(note の管理画面・販売の画面・試行の記録など) から写したもの。

- 対象の種類: ``note_piece`` (N1〜N3)・``channel`` (N3)・``product`` (N4 / N5 と配布)・
  ``pilot`` (N7。仮名の参照 ``pilot-xx`` だけ。個人の情報は入れない)。
- 単位: ``count`` / ``jpy`` (整数)・``hours`` / ``minutes`` (小数可)・``flag`` (0 か 1)・
  ``scale_1_5``。
- 要約は標本の大きさを必ず出し、勝ち負け・因果・推定を出さない。対象が 3 未満、または期間が
  28 日未満なら ``small_sample`` (比べない)。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

UNITS = ("count", "jpy", "hours", "minutes", "flag", "scale_1_5")
INTEGER_UNITS = ("count", "jpy", "flag", "scale_1_5")

CATALOG: dict[str, dict[str, str]] = {
    "note_piece": {"views": "count", "likes": "count", "comments": "count",
                   "sales_count": "count", "revenue_jpy": "jpy", "refund_count": "count"},
    "channel": {"followers": "count", "referral_sessions": "count", "subscribers": "count"},
    "product": {"units_sold": "count", "revenue_jpy": "jpy", "refund_count": "count",
                "downloads": "count"},
    "pilot": {"activated": "flag", "time_to_first_value_hours": "hours",
              "workflows_completed": "count", "active_days": "count",
              "onboarding_difficulty": "scale_1_5", "support_minutes": "minutes",
              "operating_cost_jpy": "jpy", "api_cost_jpy": "jpy",
              "willingness_to_pay_jpy": "jpy"},
}  # fmt: skip
_REF = {
    "note_piece": re.compile(r"^draft-[0-9a-f]{6,32}$"),
    "channel": re.compile(r"^(note|wordpress|threads|newsletter|membership|marketplace)$"),
    "product": re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$"),
    "pilot": re.compile(r"^pilot-[a-z0-9-]{1,32}$"),
}
MIN_SUBJECTS = 3
MIN_SPAN_DAYS = 28


class MetricError(ValueError):
    pass


@dataclass(frozen=True)
class MetricInput:
    subject_kind: str
    subject_ref: str
    metric: str
    value: float
    observed_at: datetime
    source_description: str
    entered_by: str
    period_start: date | None = None
    period_end: date | None = None
    note: str | None = None


def unit_of(kind: str, metric: str) -> str:
    try:
        return CATALOG[kind][metric]
    except KeyError:
        raise MetricError(f"unknown metric {kind}.{metric}; known: "
                          f"{sorted(CATALOG.get(kind, {}))}") from None  # fmt: skip


def validate(entry: MetricInput) -> str:
    """検査して単位を返す。個人の情報らしい値・推定の数・範囲の外の値を拒む。"""

    if entry.subject_kind not in CATALOG:
        raise MetricError(f"unknown subject kind {entry.subject_kind!r}")
    unit = unit_of(entry.subject_kind, entry.metric)
    if "@" in entry.subject_ref or not _REF[entry.subject_kind].match(entry.subject_ref):
        raise MetricError(f"subject_ref {entry.subject_ref!r} does not match the "
                          f"{entry.subject_kind} reference form (no personal data)")  # fmt: skip
    value = float(entry.value)
    if value != value or value < 0:
        raise MetricError("value must be a number >= 0")
    if unit in INTEGER_UNITS and value != int(value):
        raise MetricError(f"{entry.metric} is counted in whole {unit}")
    if unit == "flag" and value not in (0, 1):
        raise MetricError("a flag is 0 or 1")
    if unit == "scale_1_5" and not 1 <= value <= 5:
        raise MetricError("a 1-5 scale value must be between 1 and 5")
    if entry.observed_at.tzinfo is None:
        raise MetricError("observed_at needs a timezone")
    if (entry.period_start is None) != (entry.period_end is None):
        raise MetricError("give both period_start and period_end, or neither")
    if entry.period_start and entry.period_end and entry.period_end < entry.period_start:
        raise MetricError("period_end is before period_start")
    if entry.period_end and entry.period_end > entry.observed_at.date():
        raise MetricError("the period ends after the observation (no estimates)")
    if len(entry.source_description.strip()) < 3:
        raise MetricError("say where the number was copied from (source_description)")
    if not entry.entered_by.strip() or "@" in entry.entered_by:
        raise MetricError("entered_by is a short name (no email)")
    return unit


def entry_key(entry: MetricInput) -> str:
    payload = json.dumps([entry.subject_kind, entry.subject_ref, entry.metric,
                          entry.period_start and entry.period_start.isoformat(),
                          entry.period_end and entry.period_end.isoformat(),
                          entry.observed_at.isoformat()], ensure_ascii=False)  # fmt: skip
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def summarize(rows: Iterable[dict], *, kind: str, metric: str) -> dict:
    """``rows``: 有効な記録 (上書きされたものを除く) の dict。対象ごとの最新と標本の大きさ。"""

    rows = [r for r in rows if r["subject_kind"] == kind and r["metric"] == metric]
    latest: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: (r["observed_at"], r["id"])):
        latest[r["subject_ref"]] = r
    starts = [r["period_start"] or r["observed_at"].date() for r in rows]
    ends = [r["period_end"] or r["observed_at"].date() for r in rows]
    span = (max(ends) - min(starts)).days + 1 if rows else 0
    small = len(latest) < MIN_SUBJECTS or span < MIN_SPAN_DAYS
    return {
        "subject_kind": kind, "metric": metric, "unit": unit_of(kind, metric),
        "subjects": len(latest), "entries": len(rows), "span_days": span,
        "latest": {ref: r["value"] for ref, r in sorted(latest.items())},
        "small_sample": small,
        "statement": (f"n={len(latest)} subject(s) over {span} day(s): "
                      + ("too few to compare; no conclusion" if small else
                         "descriptive only; no causal claim")),
        "provenance": "manual_entry",
    }  # fmt: skip


__all__ = ["CATALOG", "MIN_SPAN_DAYS", "MIN_SUBJECTS", "MetricError", "MetricInput", "UNITS",
           "entry_key", "summarize", "unit_of", "validate"]
