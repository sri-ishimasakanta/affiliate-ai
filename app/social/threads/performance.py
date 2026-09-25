"""Threads の投稿の成績を **同じ経過時間で** 比べる診断 (pure、読むだけ)。

「最近の投稿は views が落ちているか」を、生の現在値ではなく **公開からの経過時間を
そろえて** 答える。公開から 40 時間経った投稿の 146 views と、1.5 時間の投稿の 41 views
は比べられない。

これは記述であって学習ではない。T5 / T5.5 の成熟・閾値・参考には一切つながない。

**経過時間の起点** は実際の公開時刻 (``ThreadsPublicationService.gap_basis``: Threads の
``remote_timestamp`` → 公開 API の成功 → 照合 → 記録の順)。保存済みの snapshot の
``age_hours`` は手元の ``published_at`` から測っているので使わず、観測時刻から測り直す。

**観測とチェックポイントの対応** (:func:`match_observation_at_age`):

- 公開前の観測は使わない。``observed`` 以外 (失敗・空) の観測も使わない。
- チェックポイントに最も近い本物の観測を 1 つ選ぶ。同じ距離なら早い方 (累積値が小さい
  方 = 控えめな方)。補間はしない。累積値は累積値のまま。
- 許容幅 = min(max(15 分, その経過時間での収集間隔), チェックポイントの 50%)。収集間隔は
  ``threads_operations_policy.json`` の ``insights_refresh.interval_minutes_by_maturity``
  (T4 の成熟の段階ごと)。許容幅の外なら「比較できない」として値を使わない。

**判定** (:func:`classify_checkpoint`、スコアは作らない):

- 比較できる投稿が 3 本未満のチェックポイント → ``insufficient_data``。
- 直近 3 本が狭義に減少し、最新が「それより前の中央値」の 70% 以下 →
  ``directional_decline_signal`` (3 本だけなら確度はとても低いと明記する)。
- 最新が「それより前の中央値」の 90% 以上 → ``no_clear_decline``。
- それ以外 → ``mixed``。
- 全体: 3 本以上そろったチェックポイントが無ければ ``insufficient_data``。そろった
  チェックポイントの判定がすべて同じならそれ、違えば ``mixed``。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import combinations
from statistics import median
from zoneinfo import ZoneInfo

from app.social.threads.learning import daypart_of
from app.social.threads.measurement import INTERACTION_METRICS, classify_maturity, length_bucket

SCHEMA_VERSION = "threads-performance-diagnostic/1"
CHECKPOINT_HOURS = (1, 3, 6, 12, 24, 72)
VELOCITY_INTERVALS = ((0, 1), (1, 3), (3, 6))
METRICS = ("views", *INTERACTION_METRICS)

STATUS_INSUFFICIENT = "insufficient_data"
STATUS_NO_CLEAR_DECLINE = "no_clear_decline"
STATUS_MIXED = "mixed"
STATUS_DECLINE = "directional_decline_signal"

#: 最新が、それより前の中央値のこの割合以下なら「はっきり弱い」。
DECLINE_RATIO = 0.7
#: 最新が、それより前の中央値のこの割合以上なら「落ちていない」。
NO_DECLINE_RATIO = 0.9
MINIMUM_COMPARABLE = 3
MIN_TOLERANCE_MINUTES = 15
_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class Observation:
    observed_at: datetime
    outcome: str
    metrics: Mapping[str, int | None] = field(default_factory=dict)
    snapshot_id: int | None = None


@dataclass(frozen=True)
class PublicationRecord:
    publication_id: int
    published_at: datetime  # 実際の公開時刻 (age 0)
    basis_source: str
    proposal_id: int | None = None
    media_id: str | None = None
    trigger: str | None = None
    angle: str | None = None
    link_mode: str | None = None
    character_count: int | None = None
    article_id: int | None = None
    article_slug: str | None = None
    topic: str | None = None
    text: str = ""
    observations: tuple[Observation, ...] = ()
    last_error_category: str | None = None


@dataclass(frozen=True)
class Match:
    checkpoint_hours: float
    tolerance_minutes: float
    observation: Observation | None
    age_hours: float | None
    delta_minutes: float | None
    comparable: bool
    reason: str

    def value(self, metric: str) -> int | None:
        if not self.comparable or self.observation is None:
            return None
        return self.observation.metrics.get(metric)


# == matching ===================================================================
def tolerance_minutes(checkpoint_hours: float, measurement_policy, operations_policy) -> float:
    """その経過時間での収集間隔から許容幅を決める (上の docstring の式)。"""

    stage = classify_maturity(checkpoint_hours, measurement_policy).stage
    intervals = operations_policy.subsystem("insights_refresh").get(
        "interval_minutes_by_maturity", {}
    )
    interval = float(intervals.get(stage, 30))
    return min(max(MIN_TOLERANCE_MINUTES, interval), 0.5 * checkpoint_hours * 60)


def match_observation_at_age(
    observations: Iterable[Observation],
    *,
    published_at: datetime,
    checkpoint_hours: float,
    tolerance: float,
    as_of: datetime | None = None,
) -> Match:
    """チェックポイントに最も近い本物の観測を選ぶ (補間しない)。"""

    published_at = _aware(published_at)
    target = published_at + timedelta(hours=checkpoint_hours)
    usable = [
        o
        for o in observations
        if o.outcome == "observed"
        and _aware(o.observed_at) >= published_at
        and (as_of is None or _aware(o.observed_at) <= _aware(as_of))
    ]
    if not usable:
        return Match(checkpoint_hours, tolerance, None, None, None, False, "no observation")
    best = min(
        usable,
        key=lambda o: (
            abs((_aware(o.observed_at) - target).total_seconds()),
            _aware(o.observed_at),
        ),
    )
    age = (_aware(best.observed_at) - published_at).total_seconds() / 3600
    delta = (_aware(best.observed_at) - target).total_seconds() / 60
    if abs(delta) > tolerance:
        return Match(
            checkpoint_hours,
            tolerance,
            best,
            round(age, 3),
            round(delta, 1),
            False,
            f"nearest observation is {abs(delta):.0f} min away (tolerance {tolerance:.0f} min)",
        )
    if best.metrics.get("views") is None:
        return Match(
            checkpoint_hours,
            tolerance,
            best,
            round(age, 3),
            round(delta, 1),
            False,
            "views missing",
        )
    return Match(checkpoint_hours, tolerance, best, round(age, 3), round(delta, 1), True, "ok")


# == derived values =============================================================
def engagement_total(metrics: Mapping[str, int | None]) -> int | None:
    values = [metrics.get(m) for m in INTERACTION_METRICS]
    return None if any(v is None for v in values) else int(sum(values))


def ratio(numerator: int | None, views: int | None, minimum_views: int) -> float | None:
    """views が欠測・0・下限未満なら出さない (0 で割らない)。"""

    if numerator is None or views is None or views <= 0 or views < minimum_views:
        return None
    return round(numerator / views, 4)


def velocity(matches: Mapping[float, Match], start: float, end: float) -> dict:
    """本物の累積値の差だけで区間の伸びを出す。publication → 1h は 0 から数える。"""

    end_match = matches.get(end)
    end_views = end_match.value("views") if end_match else None
    if start == 0:
        start_views, start_age = (0, 0.0)
    else:
        start_match = matches.get(start)
        start_views = start_match.value("views") if start_match else None
        start_age = start_match.age_hours if start_match else None
    if end_views is None or start_views is None or start_age is None:
        return {
            "interval": f"{start}h-{end}h",
            "available": False,
            "views_gained": None,
            "views_per_hour": None,
        }
    hours = end_match.age_hours - start_age
    gained = end_views - start_views
    return {
        "interval": f"{start}h-{end}h",
        "available": hours > 0,
        "views_gained": gained,
        "views_per_hour": round(gained / hours, 2) if hours > 0 else None,
        "actual_hours": round(hours, 3),
    }


# == classification =============================================================
def classify_checkpoint(values: Sequence[tuple[int, int]]) -> dict:
    """(publication_id, views) を公開順に受け取り、落ち方を判定する (根拠つき)。"""

    seq = [v for _pid, v in values]
    evidence = {"sequence": [{"publication_id": p, "views": v} for p, v in values]}
    if len(seq) < MINIMUM_COMPARABLE:
        return {
            "status": STATUS_INSUFFICIENT,
            "confidence": "none",
            "reason": f"{len(seq)} comparable publication(s); {MINIMUM_COMPARABLE} needed",
            **evidence,
        }
    latest, earlier = seq[-1], seq[:-1]
    earlier_median = median(earlier)
    last_three = seq[-3:]
    strictly_down = last_three[0] > last_three[1] > last_three[2]
    relative = round(latest / earlier_median, 3) if earlier_median > 0 else None
    confidence = (
        "very_low" if len(seq) == MINIMUM_COMPARABLE else "low" if len(seq) < 6 else "moderate"
    )
    base = {
        "latest_views": latest,
        "earlier_median": earlier_median,
        "latest_to_earlier_median": relative,
        "last_three_strictly_decreasing": strictly_down,
        "confidence": confidence,
        **evidence,
    }
    if earlier_median == 0:
        return {**base, "status": STATUS_MIXED, "reason": "earlier median is 0"}
    if strictly_down and latest <= DECLINE_RATIO * earlier_median:
        return {
            **base,
            "status": STATUS_DECLINE,
            "reason": (
                f"the last three decrease and the latest is {relative:.0%} of the earlier median"
            ),
        }
    if latest >= NO_DECLINE_RATIO * earlier_median:
        return {
            **base,
            "status": STATUS_NO_CLEAR_DECLINE,
            "reason": f"the latest is {relative:.0%} of the earlier median",
        }
    return {
        **base,
        "status": STATUS_MIXED,
        "reason": "weaker than before but not a repeated decline",
    }


def overall_status(per_checkpoint: Mapping[str, dict]) -> str:
    decided = [c["status"] for c in per_checkpoint.values() if c["status"] != STATUS_INSUFFICIENT]
    if not decided:
        return STATUS_INSUFFICIENT
    return decided[0] if len(set(decided)) == 1 else STATUS_MIXED


# == similarity ================================================================
def _prose(text: str) -> str:
    return re.sub(r"\s+", "", _URL_RE.sub(" ", text or ""))


def bigram_jaccard(a: str, b: str) -> float | None:
    """URL を除いた本文の文字 2-gram の Jaccard 係数 (決定的・外部サービスなし)。"""

    x, y = _prose(a), _prose(b)
    grams_a = {x[i : i + 2] for i in range(len(x) - 1)}
    grams_b = {y[i : i + 2] for i in range(len(y) - 1)}
    if not grams_a or not grams_b:
        return None
    return round(len(grams_a & grams_b) / len(grams_a | grams_b), 3)


def shared_phrases(texts: Mapping[int, str], *, length: int = 10, limit: int = 10) -> list[dict]:
    """2 本以上の投稿に出てくる同じ言い回し (URL を除く、``length`` 文字以上)。

    重なり合う ``length`` 文字の断片は、同じ投稿の組に共通する最長のひと続きにまとめる
    (「KrispとFireflies.ai」を 10 個の断片として数えない)。
    """

    proses = {pid: _prose(text) for pid, text in texts.items()}
    owners: dict[str, set[int]] = {}
    for pid, prose in proses.items():
        for i in range(len(prose) - length + 1):
            owners.setdefault(prose[i : i + length], set()).add(pid)
    found: dict[tuple[str, tuple[int, ...]], None] = {}
    for prose in proses.values():
        cover: list[tuple[int, ...] | None] = [None] * len(prose)
        for i in range(len(prose) - length + 1):
            shared = owners.get(prose[i : i + length], set())
            if len(shared) >= 2:
                for j in range(i, i + length):
                    cover[j] = tuple(sorted(shared))
        i = 0
        while i < len(prose):
            if cover[i] is None:
                i += 1
                continue
            j = i
            while j < len(prose) and cover[j] == cover[i]:
                j += 1
            found[(prose[i:j], cover[i])] = None
            i = j
    # 同じ投稿の組で、より長い言い回しに含まれるものは出さない。
    maximal = [
        (phrase, ids)
        for phrase, ids in found
        if not any(
            ids == other_ids and phrase != other and phrase in other for other, other_ids in found
        )
    ]
    ranked = sorted(maximal, key=lambda item: (-len(item[1]), -len(item[0]), item[0]))
    return [{"phrase": phrase, "publication_ids": list(ids)} for phrase, ids in ranked[:limit]]


# == untracked posts ============================================================
def compare_media_ids(internal: Iterable[str], remote: Iterable[str]) -> dict:
    """手元の公開と、アカウントの投稿一覧の media id を突き合わせる (読むだけ)。"""

    internal_set, remote_set = set(internal), set(remote)
    return {
        "tracked": sorted(internal_set & remote_set),
        "remote_not_tracked": sorted(remote_set - internal_set),
        "internal_not_remote": sorted(internal_set - remote_set),
    }


# == report =====================================================================
def build_report(
    records: Sequence[PublicationRecord],
    *,
    as_of: datetime,
    measurement_policy,
    operations_policy,
    tz: ZoneInfo,
    untracked: Mapping | None = None,
) -> dict:
    as_of = _aware(as_of)
    visible = sorted(
        (r for r in records if _aware(r.published_at) <= as_of),
        key=lambda r: (_aware(r.published_at), r.publication_id),
    )
    minimum_views = measurement_policy.minimum_views_for_ratio
    tolerances = {
        h: round(tolerance_minutes(h, measurement_policy, operations_policy), 1)
        for h in CHECKPOINT_HOURS
    }
    cutoff = max(
        (
            _aware(o.observed_at)
            for r in visible
            for o in r.observations
            if _aware(o.observed_at) <= as_of
        ),
        default=None,
    )

    publications = []
    matches_by_pub: dict[int, dict[float, Match]] = {}
    for record in visible:
        matches = {
            h: match_observation_at_age(
                record.observations,
                published_at=record.published_at,
                checkpoint_hours=h,
                tolerance=tolerances[h],
                as_of=as_of,
            )
            for h in CHECKPOINT_HOURS
        }
        matches_by_pub[record.publication_id] = matches
        publications.append(_publication_row(record, matches, as_of, tz, measurement_policy))

    checkpoint_summary = {}
    for h in CHECKPOINT_HOURS:
        values = [
            (r.publication_id, matches_by_pub[r.publication_id][h].value("views"))
            for r in visible
            if matches_by_pub[r.publication_id][h].comparable
        ]
        checkpoint_summary[f"{h}h"] = {
            "tolerance_minutes": tolerances[h],
            "comparable_count": len(values),
            "eligible_count": sum(
                1 for r in visible if _aware(r.published_at) + timedelta(hours=h) <= as_of
            ),
            "classification": classify_checkpoint(values),
            "median_views": median([v for _p, v in values]) if values else None,
        }
    _attach_relative(publications, matches_by_pub, visible)

    per_checkpoint = {k: v["classification"] for k, v in checkpoint_summary.items()}
    status = overall_status(per_checkpoint)
    decided = [k for k, v in per_checkpoint.items() if v["status"] != STATUS_INSUFFICIENT]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(as_of),
        "as_of": _iso(as_of),
        "as_of_local": _local(as_of, tz),
        "data_cutoff": _iso(cutoff) if cutoff else None,
        "data_cutoff_local": _local(cutoff, tz) if cutoff else None,
        "local_timezone": tz.key,
        "publication_count": len(visible),
        "checkpoint_policy": {
            "checkpoints_hours": list(CHECKPOINT_HOURS),
            "age_zero": "authoritative publication time (remote timestamp first)",
            "matching": "nearest real observed snapshot, earlier on ties, no interpolation, "
            "never before publication",
            "tolerance_rule": "min(max(15 min, refresh interval at that age), 50% of the "
            "checkpoint); refresh interval from insights_refresh.interval_minutes_by_maturity",
            "tolerance_minutes": {f"{h}h": tolerances[h] for h in CHECKPOINT_HOURS},
            "ratio_minimum_views": minimum_views,
            "classification": {
                "minimum_comparable": MINIMUM_COMPARABLE,
                "decline_ratio": DECLINE_RATIO,
                "no_decline_ratio": NO_DECLINE_RATIO,
            },
        },
        "publications": publications,
        "raw_latest": [
            {
                "publication_id": p["publication_id"],
                "latest_age_hours": p["latest"]["age_hours"] if p["latest"] else None,
                "latest_views": p["latest"]["metrics"]["views"] if p["latest"] else None,
            }
            for p in publications
        ],
        "checkpoint_summary": checkpoint_summary,
        "velocity_summary": _velocity_summary(publications),
        "dimension_summary": _dimensions(publications, visible, matches_by_pub),
        "similarity": _similarity(visible),
        "untracked_remote_posts": dict(untracked or {}),
        "diagnostic": {
            "status": status,
            "decided_checkpoints": decided,
            "evidence": [
                {
                    "checkpoint": k,
                    **{x: v[x] for x in v if x != "sequence"},
                    "sequence": v["sequence"],
                }
                for k, v in per_checkpoint.items()
            ],
            "limitations": _limitations(visible, checkpoint_summary),
        },
        "side_effects": {"database_writes": 0, "threads_calls": 0, "threads_writes": 0},
    }


def _publication_row(record, matches, as_of, tz, measurement_policy) -> dict:
    minimum_views = measurement_policy.minimum_views_for_ratio
    usable = [
        o
        for o in record.observations
        if o.outcome == "observed" and _aware(record.published_at) <= _aware(o.observed_at) <= as_of
    ]
    latest = max(usable, key=lambda o: _aware(o.observed_at), default=None)
    checkpoints = {}
    for h, m in matches.items():
        metrics = dict(m.observation.metrics) if (m.comparable and m.observation) else None
        views = metrics.get("views") if metrics else None
        total = engagement_total(metrics) if metrics else None
        checkpoints[f"{h}h"] = {
            "comparable": m.comparable,
            "reason": m.reason,
            "tolerance_minutes": m.tolerance_minutes,
            "matched_observed_at_local": (
                _local(m.observation.observed_at, tz) if m.observation else None
            ),
            "actual_age_hours": m.age_hours,
            "delta_minutes": m.delta_minutes,
            "metrics": metrics,
            "engagements": total,
            "views_per_hour": (
                round(views / m.age_hours, 2) if views is not None and m.age_hours else None
            ),
            "ratios": (
                {
                    **{
                        f"{k}_per_view": ratio(metrics.get(k), views, minimum_views)
                        for k in INTERACTION_METRICS
                    },
                    "engagements_per_view": ratio(total, views, minimum_views),
                }
                if metrics
                else None
            ),
        }
    latest_age = (
        (_aware(latest.observed_at) - _aware(record.published_at)).total_seconds() / 3600
        if latest
        else None
    )
    return {
        "publication_id": record.publication_id,
        "proposal_id": record.proposal_id,
        "media_id": record.media_id,
        "trigger": record.trigger,
        "published_at_local": _local(record.published_at, tz),
        "age_basis": record.basis_source,
        "age_at_as_of_hours": round(
            (as_of - _aware(record.published_at)).total_seconds() / 3600, 2
        ),
        "angle": record.angle,
        "link_mode": record.link_mode,
        "character_count": record.character_count,
        "length_band": (
            length_bucket(record.character_count, measurement_policy)
            if record.character_count is not None
            else None
        ),
        "article_id": record.article_id,
        "article_slug": record.article_slug,
        "topic": record.topic,
        "time_bucket": time_bucket(record.published_at, tz, measurement_policy.dayparts),
        "observation_count": len(usable),
        "last_error_category": record.last_error_category,
        "latest": (
            {
                "observed_at_local": _local(latest.observed_at, tz),
                "age_hours": round(latest_age, 2),
                "metrics": dict(latest.metrics),
                "engagements": engagement_total(latest.metrics),
                "engagements_per_view": ratio(
                    engagement_total(latest.metrics), latest.metrics.get("views"), minimum_views
                ),
            }
            if latest
            else None
        ),
        "checkpoints": checkpoints,
        "velocity": [velocity(matches, s, e) for s, e in VELOCITY_INTERVALS],
    }


def _attach_relative(publications, matches_by_pub, visible) -> None:
    """同じチェックポイントで、直前の比較できる投稿と、それより前の中央値に対する比。"""

    for h in CHECKPOINT_HOURS:
        key = f"{h}h"
        history: list[int] = []
        for record, row in zip(visible, publications, strict=True):
            views = matches_by_pub[record.publication_id][h].value("views")
            cell = row["checkpoints"][key]
            if views is None:
                cell["vs_previous_pct"] = None
                cell["vs_earlier_median"] = None
                continue
            previous = history[-1] if history else None
            cell["vs_previous_pct"] = (
                round((views - previous) / previous * 100, 1) if previous else None
            )
            earlier = median(history) if history else None
            cell["vs_earlier_median"] = round(views / earlier, 3) if earlier else None
            history.append(views)


def _velocity_summary(publications) -> dict:
    out = {}
    for s, e in VELOCITY_INTERVALS:
        label = f"{s}h-{e}h"
        values = [
            {"publication_id": p["publication_id"], **v}
            for p in publications
            for v in p["velocity"]
            if v["interval"] == label and v["available"]
        ]
        out[label] = {"available_count": len(values), "values": values}
    return out


def time_bucket(moment: datetime, tz: ZoneInfo, dayparts) -> str:
    """公開時刻の時間帯。T5 と同じ既存の区切り (測定ポリシーの ``learning.dayparts``)。"""

    return daypart_of(_aware(moment).astimezone(tz).hour, dayparts)


def _dimensions(publications, visible, matches_by_pub) -> dict:
    dims = {
        "angle": "angle",
        "link_mode": "link_mode",
        "topic": "topic",
        "length_band": "length_band",
        "time_bucket": "time_bucket",
    }
    out = {}
    for name, key in dims.items():
        groups: dict[str, list[dict]] = {}
        for row in publications:
            groups.setdefault(str(row[key]), []).append(row)
        values = {}
        for value, rows in sorted(groups.items()):
            per_checkpoint = {}
            for h in (1, 3, 6):
                views = [
                    r["checkpoints"][f"{h}h"]["metrics"]["views"]
                    for r in rows
                    if r["checkpoints"][f"{h}h"]["comparable"]
                ]
                per_checkpoint[f"{h}h"] = {
                    "comparable": len(views),
                    "median_views": median(views) if views else None,
                    "views": views,
                }
            values[value] = {
                "publications": [r["publication_id"] for r in rows],
                "small_sample": len(rows) < MINIMUM_COMPARABLE,
                "checkpoints": per_checkpoint,
            }
        out[name] = values
    return out


def _similarity(visible) -> dict:
    pairs = []
    for a, b in zip(visible, visible[1:], strict=False):
        pairs.append(
            {
                "publication_ids": [a.publication_id, b.publication_id],
                "bigram_jaccard": bigram_jaccard(a.text, b.text),
                "same_angle": a.angle == b.angle,
                "same_topic": a.topic is not None and a.topic == b.topic,
                "same_article": a.article_id is not None and a.article_id == b.article_id,
            }
        )
    most_similar = max(
        (
            {
                "publication_ids": [a.publication_id, b.publication_id],
                "bigram_jaccard": bigram_jaccard(a.text, b.text),
            }
            for a, b in combinations(visible, 2)
        ),
        key=lambda x: (x["bigram_jaccard"] or 0, x["publication_ids"]),
        default=None,
    )
    return {
        "consecutive": pairs,
        "most_similar_pair": most_similar,
        "angle_sequence": [r.angle for r in visible],
        "shared_phrases": shared_phrases({r.publication_id: r.text for r in visible}),
        "method": "character bigram Jaccard on text without URLs; shared 10-character phrases",
    }


def _limitations(visible, checkpoint_summary) -> list[str]:
    notes = []
    for key, summary in checkpoint_summary.items():
        if summary["comparable_count"] < MINIMUM_COMPARABLE:
            notes.append(
                f"{key}: {summary['comparable_count']} comparable publication(s) "
                f"({summary['eligible_count']} old enough) - no trend is drawn"
            )
    if len(visible) < 10:
        notes.append(
            f"only {len(visible)} publications in total; any pattern is a small-sample description"
        )
    notes.append("views are cumulative counts reported by Threads; no interpolation is used")
    notes.append("correlation with angle/topic/time/length/link is descriptive, not causal")
    return notes


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return _aware(moment).astimezone(UTC).isoformat()


def _local(moment: datetime, tz: ZoneInfo) -> str:
    return _aware(moment).astimezone(tz).isoformat(timespec="minutes")


__all__ = [
    "CHECKPOINT_HOURS",
    "Observation",
    "PublicationRecord",
    "build_report",
    "classify_checkpoint",
    "compare_media_ids",
    "match_observation_at_age",
    "tolerance_minutes",
    "velocity",
]
