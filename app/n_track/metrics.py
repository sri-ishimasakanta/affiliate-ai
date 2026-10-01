"""手で写す数の目録と検査・要約 (N3。pure)。

目録はこのプロジェクト自身の言葉だけ (provider の書き出しの形を仮定しない)。数は人が画面
(note の管理画面・販売の画面・試行の記録など) から写したもの。

- 対象の種類: ``note_piece`` (N1〜N3)・``channel`` (N3)・``product`` (N4 / N5 と配布)・
  ``pilot`` (N7。仮名の参照 ``pilot-xx`` だけ。個人の情報は入れない)。
- 単位: ``count`` / ``jpy`` (整数)・``hours`` / ``minutes`` (小数可)・``flag`` (0 か 1)・
  ``scale_1_5``。
- 要約は標本の大きさを必ず出し、勝ち負け・因果・推定を出さない。対象が 3 未満、または期間が
  28 日未満なら ``small_sample`` (比べない)。
- 2026-10-01: ``note_piece`` の参照に ``external-n<12>`` (台帳に無い、仕組みの外で公開した記事。
  登録した記事だけ) を足した。観測の時刻は UTC にそろえて同じ時刻を 1 つに数える。累計の指標
  (期間なし) は前の観測との差を出す (``timeline``)。7 日ごとの観測の予定 (``checkpoints``)。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

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
              "willingness_to_pay_jpy": "jpy", "support_contacts": "count",
              # 構造化した声 (N7): 評価と、つまずいた所の印
              "value_rating": "scale_1_5", "would_continue": "flag",
              "friction_setup": "flag", "friction_approvals": "flag",
              "friction_cost": "flag", "friction_trust": "flag"},
}  # fmt: skip
_REF = {
    "note_piece": re.compile(r"^(draft-[0-9a-f]{6,32}|external-n[0-9a-z]{12})$"),
    "channel": re.compile(r"^(note|wordpress|threads|newsletter|membership|marketplace)$"),
    "product": re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$"),
    "pilot": re.compile(r"^pilot-[a-z0-9-]{1,32}$"),
}
_PERSONAL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|(?<!\d)0\d{1,4}-\d{1,4}-\d{3,4}(?!\d)"
                       r"|\+\d{8,15}(?!\d)|https?://")
MIN_SUBJECTS = 3
MIN_SPAN_DAYS = 28
#: 期間なしで記録すると「その時点までの累計」(note の全期間の値・今のフォロワー数) として読む
#: 指標。前の観測との差は分析のときに計算する (記録は累計のまま)。
CUMULATIVE_WITHOUT_PERIOD = frozenset({
    ("note_piece", "views"), ("note_piece", "likes"), ("note_piece", "comments"),
    ("channel", "followers"), ("channel", "subscribers")})  # fmt: skip


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
    if entry.note and _PERSONAL.search(entry.note):
        raise MetricError("the note looks like it contains personal data (email / phone / URL)")
    return unit


def entry_key(entry: MetricInput) -> str:
    payload = json.dumps([entry.subject_kind, entry.subject_ref, entry.metric,
                          entry.period_start and entry.period_start.isoformat(),
                          entry.period_end and entry.period_end.isoformat(),
                          # 同じ時刻を別のオフセットで書いても 1 つ (UTC にそろえる)
                          entry.observed_at.astimezone(UTC).isoformat()],
                         ensure_ascii=False)  # fmt: skip
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


def timeline(rows: Iterable[dict], *, kind: str, ref: str, metric: str,
             published_at: datetime | None = None) -> list[dict]:
    """1 つの対象・指標の観測を時刻の順に並べる。累計の指標 (期間なし) は前の観測との差、
    公開の時刻があれば公開からの日数を足す。推定・補間はしない (観測した点だけ)。"""

    picked = sorted((r for r in rows if (r["subject_kind"], r["subject_ref"], r["metric"])
                     == (kind, ref, metric)), key=lambda r: (r["observed_at"], r["id"]))
    cumulative = (kind, metric) in CUMULATIVE_WITHOUT_PERIOD
    out, previous = [], None
    for r in picked:
        point = {"id": r["id"], "observed_at": r["observed_at"].isoformat(), "value": r["value"],
                 "period": ([r["period_start"].isoformat(), r["period_end"].isoformat()]
                            if r["period_start"] else None)}
        if published_at is not None:
            point["days_since_publication"] = round(
                (r["observed_at"] - published_at).total_seconds() / 86400, 1)
        if cumulative and r["period_start"] is None:
            if previous is not None:
                delta = r["value"] - previous["value"]
                point["delta"] = delta
                point["days_since_previous"] = round(
                    (r["observed_at"] - previous["observed_at"]).total_seconds() / 86400, 1)
                if delta < 0:
                    point["warning"] = "a running total went down: check the entry"
            previous = r
        out.append(point)
    return out


def checkpoints(observed: Iterable[datetime], *, now: datetime, cadence_days: int = 7,
                count: int = 4, window_days: int = 3) -> dict:
    """baseline (最初の観測) から ``cadence_days`` ごとの観測の予定と、それぞれの状態。

    状態: ``observed`` (予定の前後 ``window_days`` 日に観測がある)・``missed`` (窓を過ぎた)・
    ``due`` (いまが窓の中)・``upcoming``。baseline が無ければ ``waiting_for_baseline``。
    """

    times = sorted(observed)
    if not times:
        return {"state": "waiting_for_baseline", "baseline": None, "checkpoints": []}
    base, window = times[0], timedelta(days=window_days)
    out = []
    for k in range(count + 1):
        target = base + timedelta(days=cadence_days * k)
        hits = [t for t in times if abs(t - target) <= window]
        if hits:
            state, at = "observed", min(hits, key=lambda t: abs(t - target)).isoformat()
        elif now > target + window:
            state, at = "missed", None
        elif now >= target - window:
            state, at = "due", None
        else:
            state, at = "upcoming", None
        out.append({"label": "baseline" if k == 0 else f"week {k}",
                    "target": target.date().isoformat(), "state": state, "observed_at": at})
    done = all(c["state"] == "observed" for c in out)
    return {"state": "complete" if done else "in_progress", "baseline": base.isoformat(),
            "checkpoints": out}


__all__ = ["CATALOG", "CUMULATIVE_WITHOUT_PERIOD", "MIN_SPAN_DAYS", "MIN_SUBJECTS",
           "MetricError", "MetricInput", "UNITS", "checkpoints", "entry_key", "summarize",
           "timeline", "unit_of", "validate"]
