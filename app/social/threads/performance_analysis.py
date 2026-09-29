"""自分の Threads 投稿の成績の分析と、生成への補助の参考 (T6.5、pure・読むだけ)。

既存の層の上に置く (作り直さない):

- 観測は append-only の ``threads_insight_snapshots`` (T4)。指標は **累積値** (views は時間とともに
  減らない)。欠測は ``None`` (0 ではない)。
- 経過時間の起点は実際の公開時刻 (``ThreadsPerformanceService.records`` と同じ)。
- 同じ経過時間での観測の選び方は ``performance.match_observation_at_age`` (T6.1 の診断と同じ:
  最も近い本物の観測、補間しない、許容幅は収集間隔から)。
- 比率の下限は測定ポリシーの ``comparison.minimum_views_for_ratio``。証拠の本数の目安は
  ``threads_observation_policy.json`` の ``evidence_thresholds`` (T6.5B.2、5 / 10 / 30)。

**生の累積値で、経過時間の違う投稿を直接比べない。** 比べるのは、同じチェックポイント
(公開から N 時間) に本物の観測がある投稿どうしだけ。どのチェックポイントを使うかは、データに
ある本数で決める (``comparison_checkpoint``: 比べられる本数が下限を満たす、いちばん遅い
チェックポイント)。無ければ ``insufficient_data``。

**勝ち・負けの固定の閾値は作らない。** 比べる相手 (cohort = 同じ lane・同じチェックポイント) の
中の **順位 (percentile)** と **中央値との比** だけを出す。reach (views)・engagement (相互作用の
合計)・conversation (返信)・amplification (再投稿 + 引用 + シェア)・engagement rate
(相互作用 / views、views が下限以上のときだけ) を **別々に** 持つ。総合点は作らない。

**参考 (feedback)** は、同じ切り口・会話のきっかけ・長さの帯の投稿の **順位の中央値** が、
cohort の真ん中 (0.5) より上か下かを、1 本ずつ抜いても向きが変わらないときだけ言う。本数が
少なければ ``hypothesis`` / ``preliminary`` と書き、「成功する」とは言わない。本文は写さない
(切り口・きっかけ・長さなどの再利用できる特徴だけ)。Growth と通常の投稿は混ぜない。
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.social.threads.learning import WEEKDAYS, daypart_of
from app.social.threads.measurement import classify_maturity, length_bucket
from app.social.threads.performance import (
    CHECKPOINT_HOURS,
    METRICS,
    Observation,
    PublicationRecord,
    engagement_total,
    match_observation_at_age,
    tolerance_minutes,
)

SCHEMA_VERSION = "threads-performance-analysis/1"
FEEDBACK_SCHEMA = "threads-performance-feedback/1"

LANE_REGULAR = "regular"
LANE_GROWTH = "growth"
LANE_UNKNOWN = "unknown"
ORIGIN_ARTICLE = "article"
ORIGIN_NON_ARTICLE = "non_article"
UNKNOWN = "unknown"
#: トピックを付けずに送った (記録はあるが値が無い)。
NO_TOPIC = "none"

#: 比べる成分 (別々に持つ。総合点は作らない)。
COMPONENTS = ("reach", "engagement", "conversation", "amplification", "engagement_rate")
AMPLIFICATION_METRICS = ("reposts", "quotes", "shares")

EVIDENCE_INSUFFICIENT = "insufficient_data"
EVIDENCE_HYPOTHESIS = "hypothesis"
EVIDENCE_PRELIMINARY = "preliminary"
EVIDENCE_DESCRIPTIVE = "descriptive"

DIRECTION_HIGHER = "higher_than_cohort"
DIRECTION_LOWER = "lower_than_cohort"
DIRECTION_NONE = "no_clear_difference"

MODE_NEUTRAL = "neutral"
MODE_ADVISORY = "advisory"

#: 生成で変えられる特徴 (prompt に書いてよい)。
ACTIONABLE_DIMENSIONS = ("angle", "conversation_hook", "length_band")
#: 文面を変えない文脈の特徴 (記録だけ。prompt には書かない)。
CONTEXT_DIMENSIONS = ("threads_topic", "origin", "daypart", "weekday", "age_stage")
SEGMENT_DIMENSIONS = ("lane", *ACTIONABLE_DIMENSIONS, *CONTEXT_DIMENSIONS)
#: 参考に使う成分 (比率は views の下限を満たす投稿だけ)。
FEEDBACK_COMPONENTS = ("reach", "engagement", "engagement_rate")

SAFEGUARDS = (
    "supplementary signal only: facts, article evidence and style rules take precedence",
    "keep exploring: do not narrow generation to past winners",
    "growth and regular posts are analysed separately and never mixed",
    "the existing soft preference against repeating the last 1-2 topics/angles is unchanged",
    "no post text is copied; only reusable features (angle, hook, length) are described",
    "descriptive association, not causation",
)


@dataclass(frozen=True)
class EvidenceThresholds:
    """証拠の本数の目安 (``threads_observation_policy.json`` の ``evidence_thresholds``)。"""

    minimum: int = 5
    preliminary: int = 10
    descriptive: int = 30

    @classmethod
    def from_policy(cls, policy: Mapping | None) -> EvidenceThresholds:
        raw = (policy or {}).get("evidence_thresholds") or {}
        return cls(
            minimum=int(raw.get("author_baseline_min_other_posts", 5)),
            preliminary=int(raw.get("group_candidate_pattern_min", 10)),
            descriptive=int(raw.get("group_stronger_descriptive_min", 30)),
        )


def evidence_status(n: int, thresholds: EvidenceThresholds) -> str:
    if n < thresholds.minimum:
        return EVIDENCE_INSUFFICIENT
    if n < thresholds.preliminary:
        return EVIDENCE_HYPOTHESIS
    if n < thresholds.descriptive:
        return EVIDENCE_PRELIMINARY
    return EVIDENCE_DESCRIPTIVE


@dataclass(frozen=True)
class PostInput:
    """1 本の公開の入力 (``PublicationRecord`` + 提案の来歴から読んだ値)。"""

    record: PublicationRecord
    lane: str = LANE_UNKNOWN
    content_kind: str = UNKNOWN
    #: コンテナ作成で実際に送ったトピック (記録が無ければ ``None`` で ``topic_recorded=False``)。
    threads_topic: str | None = None
    topic_recorded: bool = False
    conversation_hook: str | None = None


# -- 1 本の値 -------------------------------------------------------------------------------


def _hours(delta) -> float:
    return round(delta.total_seconds() / 3600, 3)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def derived_metrics(metrics: Mapping[str, int | None], minimum_views: int) -> dict:
    """累積値から作る値。**0 で割らない・欠測を 0 にしない。**

    比率は views が 1 以上のときだけ計算し、views が比率の下限 (``minimum_views``) 未満なら
    ``rate_reliable=False`` (比べるときには使わない)。
    """

    views = metrics.get("views")
    engagement = engagement_total(metrics)
    replies = metrics.get("replies")
    amp_values = [metrics.get(m) for m in AMPLIFICATION_METRICS]
    amplification = None if any(v is None for v in amp_values) else int(sum(amp_values))

    def rate(numerator):
        if numerator is None or views is None or views <= 0:
            return None
        return round(numerator / views, 4)

    if views is None:
        note = "views_missing"
    elif views == 0:
        note = "zero_views"
    elif views < minimum_views:
        note = "views_below_ratio_minimum"
    else:
        note = None
    return {
        "total_engagement": engagement,
        "amplification_count": amplification,
        "engagement_per_view": rate(engagement),
        "conversation_rate": rate(replies),
        "amplification_rate": rate(amplification),
        "rate_reliable": note is None,
        "rate_note": note,
    }


def components_of(metrics: Mapping[str, int | None], minimum_views: int) -> dict:
    """比べる成分 (同じ経過時間の累積値)。比率は下限を満たすときだけ。"""

    derived = derived_metrics(metrics, minimum_views)
    return {
        "reach": metrics.get("views"),
        "engagement": derived["total_engagement"],
        "conversation": metrics.get("replies"),
        "amplification": derived["amplification_count"],
        "engagement_rate": derived["engagement_per_view"] if derived["rate_reliable"] else None,
    }


def _completeness(visible: Sequence[Observation], published_at: datetime) -> dict:
    observed = [o for o in visible if o.outcome == "observed" and o.observed_at >= published_at]
    failed = [o for o in visible if o.outcome != "observed"]
    latest = observed[-1] if observed else None
    missing = sorted(m for m in METRICS if latest is not None and latest.metrics.get(m) is None)
    if latest is None:
        status = "no_insight"
    elif missing:
        status = "partial"
    else:
        status = "complete"
    return {"status": status, "observed_snapshots": len(observed),
            "failed_snapshots": len(failed), "latest_missing_metrics": missing}  # fmt: skip


def analyze_post(post: PostInput, *, as_of: datetime, tz: ZoneInfo, measurement_policy,
                 operations_policy) -> dict:  # fmt: skip
    """1 本の成績 (その時点で観測済みのものだけ)。"""

    record = post.record
    published = _aware(record.published_at)
    visible = sorted((o for o in record.observations if _aware(o.observed_at) <= as_of),
                     key=lambda o: (_aware(o.observed_at), o.snapshot_id or 0))  # fmt: skip
    visible = [Observation(_aware(o.observed_at), o.outcome, o.metrics, o.snapshot_id)
               for o in visible]  # fmt: skip
    completeness = _completeness(visible, published)
    observed = [o for o in visible if o.outcome == "observed" and o.observed_at >= published]
    minimum_views = measurement_policy.minimum_views_for_ratio
    age = _hours(as_of - published)
    first = observed[0] if observed else None
    latest = observed[-1] if observed else None
    checkpoints = {}
    for hours in CHECKPOINT_HOURS:
        if age < hours:
            checkpoints[f"{hours}h"] = {"reached": False, "comparable": False}
            continue
        match = match_observation_at_age(
            visible, published_at=published, checkpoint_hours=hours,
            tolerance=tolerance_minutes(hours, measurement_policy, operations_policy),
            as_of=as_of,
        )  # fmt: skip
        metrics = dict(match.observation.metrics) if match.comparable else None
        checkpoints[f"{hours}h"] = {
            "reached": True,
            "comparable": match.comparable,
            "observation_age_hours": match.age_hours,
            "delta_minutes": match.delta_minutes,
            "reason": match.reason,
            "metrics": metrics,
            "components": components_of(metrics, minimum_views) if metrics else None,
        }
    local = published.astimezone(tz)
    characters = record.character_count
    growth_delta = None
    if first is not None and latest is not None and latest is not first:
        span = _hours(latest.observed_at - first.observed_at)
        gained = {m: (latest.metrics.get(m) - first.metrics.get(m))
                  if latest.metrics.get(m) is not None and first.metrics.get(m) is not None
                  else None for m in METRICS}  # fmt: skip
        growth_delta = {"hours": span, "gained": gained,
                        "views_per_hour": round(gained["views"] / span, 3)
                        if gained["views"] is not None and span > 0 else None}  # fmt: skip
    return {
        "publication_id": record.publication_id,
        "proposal_id": record.proposal_id,
        "lane": post.lane,
        "content_kind": post.content_kind,
        "origin": ORIGIN_ARTICLE if record.article_id is not None else ORIGIN_NON_ARTICLE,
        "article_id": record.article_id,
        "article_keyword": record.topic,
        # 記録があって値が無い = トピックを付けずに送った (``none``)。記録が無い = ``unknown``。
        "threads_topic": ((post.threads_topic or NO_TOPIC) if post.topic_recorded
                          else UNKNOWN),  # fmt: skip
        "topic_recorded": post.topic_recorded,
        "angle": record.angle or UNKNOWN,
        "conversation_hook": post.conversation_hook or UNKNOWN,
        "length_band": (length_bucket(characters, measurement_policy)
                        if characters is not None else UNKNOWN),  # fmt: skip
        "character_count": characters,
        "published_at": published.isoformat(),
        "published_at_local": local.isoformat(timespec="minutes"),
        "daypart": daypart_of(local.hour, measurement_policy.dayparts),
        "weekday": WEEKDAYS[local.weekday()],
        "age_hours": age,
        "age_stage": classify_maturity(age, measurement_policy).stage,
        "completeness": completeness,
        "first_observation": _obs(first, published),
        "latest_observation": {**_obs(latest, published),
                               "insight_age_hours": _hours(as_of - latest.observed_at)}
        if latest else None,  # fmt: skip
        "latest_metrics": dict(latest.metrics) if latest else None,
        "latest_derived": derived_metrics(latest.metrics, minimum_views) if latest else None,
        "growth_delta": growth_delta,
        "checkpoints": checkpoints,
        "note": "latest values are cumulative at different ages and are not compared across "
                "posts; comparisons use equal-age checkpoints",
    }


def _obs(observation: Observation | None, published: datetime) -> dict | None:
    if observation is None:
        return None
    return {"observed_at": observation.observed_at.isoformat(),
            "age_hours": _hours(observation.observed_at - published),
            "metrics": dict(observation.metrics)}  # fmt: skip


# -- 比べる相手 (cohort) ----------------------------------------------------------------------


def checkpoint_counts(posts: Sequence[dict], lane: str) -> dict[str, int]:
    return {f"{h}h": sum(1 for p in posts if p["lane"] == lane
                         and p["checkpoints"][f"{h}h"].get("comparable"))
            for h in CHECKPOINT_HOURS}  # fmt: skip


def comparison_checkpoint(counts: Mapping[str, int], thresholds: EvidenceThresholds
                          ) -> str | None:  # fmt: skip
    """比べるチェックポイント (データで決める): 比べられる本数で届く **いちばん強い証拠の段階**
    のうち、いちばん遅いチェックポイント。下限に届くものが無ければ ``None``。"""

    best, chosen = None, None
    for hours in CHECKPOINT_HOURS:
        key = f"{hours}h"
        tier = evidence_status(counts.get(key, 0), thresholds)
        if tier == EVIDENCE_INSUFFICIENT:
            continue
        rank = _EVIDENCE_ORDER.index(tier)
        if best is None or rank >= best:
            best, chosen = rank, key
    return chosen


def percentile_rank(value: float, others: Sequence[float]) -> float | None:
    """ほかの投稿の中での位置 (0〜1、同じ値は半分として数える midrank)。"""

    if not others:
        return None
    below = sum(1 for v in others if v < value)
    equal = sum(1 for v in others if v == value)
    return round((below + 0.5 * equal) / len(others), 4)


def position(rank: float | None) -> str | None:
    """分布の中の位置の名前 (四分位。固定の成績の閾値ではない)。"""

    if rank is None:
        return None
    if rank >= 0.75:
        return "upper_quartile"
    if rank <= 0.25:
        return "lower_quartile"
    return "middle_half"


def evaluate(posts: list[dict], *, lane: str, checkpoint: str | None,
             thresholds: EvidenceThresholds) -> None:  # fmt: skip
    """同じ lane・同じチェックポイントの投稿の中での、成分ごとの位置を各投稿に書く。"""

    members = [p for p in posts if p["lane"] == lane]
    for post in members:
        post["evaluation"] = {"lane": lane, "checkpoint": checkpoint, "components": {}}
        if checkpoint is None:
            post["evaluation"]["status"] = EVIDENCE_INSUFFICIENT
            post["evaluation"]["reason"] = "no checkpoint has enough comparable posts in this lane"
            continue
        mine = (post["checkpoints"][checkpoint].get("components") or {})
        status = []
        for component in COMPONENTS:
            value = mine.get(component)
            others = [(p["checkpoints"][checkpoint].get("components") or {}).get(component)
                      for p in members if p is not post]  # fmt: skip
            others = [v for v in others if v is not None]
            cohort_n = len(others) + (1 if value is not None else 0)
            ev = evidence_status(cohort_n, thresholds)
            if value is None:
                post["evaluation"]["components"][component] = {
                    "value": None, "cohort_n": cohort_n, "evidence": ev,
                    "reason": "not comparable at this checkpoint (no matched observation, "
                              "missing metric or views below the ratio minimum)"}  # fmt: skip
                continue
            median = statistics.median(others) if others else None
            rank = percentile_rank(value, others) if ev != EVIDENCE_INSUFFICIENT else None
            post["evaluation"]["components"][component] = {
                "value": value, "cohort_n": cohort_n, "evidence": ev,
                "cohort_median_others": median,
                "relative_to_median": round(value / median, 3) if median else None,
                "percentile_rank": rank, "position": position(rank)}  # fmt: skip
            status.append(ev)
        # 比べられた成分のうち、いちばん強い証拠 (成分ごとの証拠は components に残す。
        # 件数の少ない engagement_rate が、views の比較まで弱く見せないように)。
        post["evaluation"]["status"] = (max(status, key=_EVIDENCE_ORDER.index)
                                        if status else EVIDENCE_INSUFFICIENT)  # fmt: skip


_EVIDENCE_ORDER = [EVIDENCE_INSUFFICIENT, EVIDENCE_HYPOTHESIS, EVIDENCE_PRELIMINARY,
                   EVIDENCE_DESCRIPTIVE]  # fmt: skip


# -- まとまり (segment) ------------------------------------------------------------------------


def _ranks(posts: Iterable[dict], component: str) -> list[float]:
    out = []
    for p in posts:
        item = ((p.get("evaluation") or {}).get("components") or {}).get(component) or {}
        if item.get("percentile_rank") is not None:
            out.append(item["percentile_rank"])
    return out


def _direction(ranks: Sequence[float], rest: Sequence[float]) -> tuple[str, bool]:
    """まとまりの順位の中央値が、同じ cohort の **ほかの投稿** の順位の中央値より上 / 下か。

    同じ値が多い (0 が多い) と順位が真ん中からずれるので、固定の 0.5 ではなく、ほかの投稿と
    比べる。1 本ずつ抜いても向きが変わらないときだけ向きを言う。
    """

    if not ranks or not rest:
        return DIRECTION_NONE, False
    baseline = statistics.median(rest)
    median = statistics.median(ranks)
    if median == baseline:
        return DIRECTION_NONE, False
    side = median > baseline
    stable = len(ranks) > 1 and all(
        (statistics.median(ranks[:i] + ranks[i + 1:]) > baseline) == side
        and statistics.median(ranks[:i] + ranks[i + 1:]) != baseline
        for i in range(len(ranks))
    )
    if not stable:
        return DIRECTION_NONE, False
    return (DIRECTION_HIGHER if side else DIRECTION_LOWER), True


def segments(posts: list[dict], *, thresholds: EvidenceThresholds,
             checkpoints: Mapping[str, str | None]) -> dict:  # fmt: skip
    """次元 → 値 → 本数・比べられた本数・成分ごとの中央値と順位の中央値。"""

    out: dict[str, dict] = {}
    for dimension in SEGMENT_DIMENSIONS:
        groups: dict[str, list[dict]] = {}
        for post in posts:
            groups.setdefault(str(post.get(dimension) or UNKNOWN), []).append(post)
        out[dimension] = {}
        for value in sorted(groups):
            members = groups[value]
            entry = {"posts": len(members), "lanes": sorted({p["lane"] for p in members}),
                     "components": {}}  # fmt: skip
            outside = [p for p in posts if p not in members]
            for component in COMPONENTS:
                ranks = _ranks(members, component)
                rest = _ranks(outside, component)
                values = []
                for p in members:
                    cp = checkpoints.get(p["lane"])
                    comps = (p["checkpoints"].get(cp) or {}).get("components") if cp else None
                    if comps and comps.get(component) is not None:
                        values.append(comps[component])
                direction, stable = _direction(ranks, rest)
                enough = len(ranks) >= thresholds.minimum and len(rest) >= thresholds.minimum
                entry["components"][component] = {
                    "n": len(ranks),
                    "evidence": evidence_status(len(ranks), thresholds),
                    "median_value": statistics.median(values) if values else None,
                    "median_percentile_rank": round(statistics.median(ranks), 4)
                    if ranks else None,
                    "rest_n": len(rest),
                    "rest_median_percentile_rank": round(statistics.median(rest), 4)
                    if rest else None,
                    "direction": direction if enough else DIRECTION_NONE,
                    "leave_one_out_stable": stable and enough,
                }  # fmt: skip
            out[dimension][value] = entry
    return out


# -- 参考 (feedback) ---------------------------------------------------------------------------

_DIMENSION_LABELS = {"angle": "切り口", "conversation_hook": "会話のきっかけ",
                     "length_band": "長さ", "threads_topic": "トピック", "origin": "元",
                     "daypart": "時間帯", "weekday": "曜日", "age_stage": "経過"}  # fmt: skip
_COMPONENT_LABELS = {"reach": "views", "engagement": "相互作用の数",
                     "engagement_rate": "相互作用 / views"}  # fmt: skip
_EVIDENCE_LABELS = {EVIDENCE_HYPOTHESIS: "仮説", EVIDENCE_PRELIMINARY: "暫定",
                    EVIDENCE_DESCRIPTIVE: "記述"}  # fmt: skip


@dataclass(frozen=True)
class PerformanceFeedback:
    """生成への補助の参考 (``threads-performance-feedback/1``)。"""

    as_of: str
    checkpoint: str | None
    cohort_n: int
    supported: tuple[dict, ...] = ()
    weak: tuple[dict, ...] = ()
    insufficient: tuple[dict, ...] = ()
    context: tuple[dict, ...] = ()
    growth: Mapping = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def mode(self) -> str:
        return MODE_ADVISORY if (self.supported or self.weak) else MODE_NEUTRAL

    def content(self) -> dict:
        return {
            "schema": FEEDBACK_SCHEMA,
            "mode": self.mode,
            "lane": LANE_REGULAR,
            "checkpoint": self.checkpoint,
            "cohort_n": self.cohort_n,
            "currently_supported_patterns": list(self.supported),
            "weak_patterns": list(self.weak),
            "insufficient_evidence_patterns": list(self.insufficient),
            "context_observations": list(self.context),
            "growth_observations": dict(self.growth),
            "notes": list(self.notes),
            "safeguards": list(SAFEGUARDS),
        }

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(self.content(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)  # fmt: skip
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {**self.content(), "as_of": self.as_of, "fingerprint": self.fingerprint}

    def provenance(self) -> dict:
        return {"schema": FEEDBACK_SCHEMA, "mode": self.mode, "fingerprint": self.fingerprint,
                "as_of": self.as_of, "checkpoint": self.checkpoint,
                "patterns": [{"dimension": p["dimension"], "value": p["value"],
                              "component": p["component"], "direction": p["direction"],
                              "evidence": p["evidence"]}
                             for p in (*self.supported, *self.weak)]}  # fmt: skip


def supported_values_for(style_policy, measurement_policy) -> dict[str, tuple[str, ...]]:
    """生成器が作れる値 (これ以外の値の参考は prompt に入れない)。"""

    from app.social.threads.conversation import HOOKS

    return {"angle": tuple(style_policy.angles), "conversation_hook": tuple(HOOKS),
            "length_band": tuple(b["name"] for b in measurement_policy.length_buckets)}


def neutral_feedback(as_of: str, reason: str) -> PerformanceFeedback:
    return PerformanceFeedback(as_of=as_of, checkpoint=None, cohort_n=0, notes=(reason,))


def build_feedback(report: Mapping, *, supported_values: Mapping[str, Sequence[str]] | None = None
                   ) -> PerformanceFeedback:  # fmt: skip
    """分析の結果から参考を作る (決定的)。読めない・証拠が無ければ中立。"""

    as_of = str(report.get("as_of", ""))
    if report.get("schema_version") != SCHEMA_VERSION:
        return neutral_feedback(as_of, "the performance analysis could not be read; neutral")
    regular = (report.get("cohorts") or {}).get(LANE_REGULAR) or {}
    checkpoint = regular.get("checkpoint")
    if checkpoint is None:
        return neutral_feedback(as_of, "no equal-age checkpoint has enough regular posts; "
                                       "keep normal diversity and editorial defaults")
    supported, weak, insufficient, context = [], [], [], []
    regular_segments = (report.get("regular_segments") or {})
    for dimension in (*ACTIONABLE_DIMENSIONS, *CONTEXT_DIMENSIONS):
        for value, entry in (regular_segments.get(dimension) or {}).items():
            if value == UNKNOWN:
                continue
            for component in FEEDBACK_COMPONENTS:
                item = entry["components"][component]
                pattern = {
                    "dimension": dimension, "value": value, "component": component,
                    "checkpoint": checkpoint, "n": item["n"], "evidence": item["evidence"],
                    "median_percentile_rank": item["median_percentile_rank"],
                    "rest_n": item["rest_n"],
                    "rest_median_percentile_rank": item["rest_median_percentile_rank"],
                    "median_value": item["median_value"], "direction": item["direction"],
                }  # fmt: skip
                if item["evidence"] == EVIDENCE_INSUFFICIENT:
                    insufficient.append(pattern)
                    continue
                if item["direction"] == DIRECTION_NONE:
                    continue
                pattern["statement_ja"] = _statement(pattern)
                allowed = (supported_values or {}).get(dimension)
                pattern["actionable"] = dimension in ACTIONABLE_DIMENSIONS and (
                    allowed is None or value in allowed)
                if dimension in CONTEXT_DIMENSIONS or not pattern["actionable"]:
                    context.append(pattern)
                elif item["direction"] == DIRECTION_HIGHER:
                    supported.append(pattern)
                else:
                    weak.append(pattern)
    # Growth は数と証拠の段階だけ (最新の値は観測のたびに変わり、参考の指紋を揺らすので入れない)。
    growth = {k: v for k, v in (report.get("growth_summary") or {}).items()
              if k in ("posts", "evidence", "note")}  # fmt: skip
    notes = []
    if not supported and not weak:
        notes.append("no stable equal-age difference among regular posts; neutral feedback")
    return PerformanceFeedback(
        as_of=as_of, checkpoint=checkpoint, cohort_n=int(regular.get("comparable", 0)),
        supported=tuple(supported), weak=tuple(weak), insufficient=tuple(insufficient),
        context=tuple(context), growth=growth, notes=tuple(notes),
    )


def _statement(pattern: Mapping) -> str:
    evidence = _EVIDENCE_LABELS.get(pattern["evidence"], pattern["evidence"])
    side = "上" if pattern["direction"] == DIRECTION_HIGHER else "下"
    dimension = _DIMENSION_LABELS.get(pattern["dimension"], pattern["dimension"])
    component = _COMPONENT_LABELS.get(pattern["component"], pattern["component"])
    rank = pattern["median_percentile_rank"]
    rest = pattern["rest_median_percentile_rank"]
    return (f"{dimension} 「{pattern['value']}」は、公開から {pattern['checkpoint']} の "
            f"{component} で、同じ経過時間の通常の投稿の中の順位の中央値が {rank} "
            f"(ほかの投稿の {rest} より{side}、n={pattern['n']}、{evidence})。"
            "原因ではなく、観測された傾向。")


def render_feedback_sections(feedback: PerformanceFeedback | None) -> list[str]:
    """prompt に足す節。中立・参考なしなら空 (prompt は変わらない)。"""

    if feedback is None or feedback.mode == MODE_NEUTRAL:
        return []
    usable = [p for p in (*feedback.supported, *feedback.weak) if p.get("actionable")]
    if not usable:
        return []
    lines = ["## 過去の成績からの補助の参考 (観測された傾向。仮説)",
             "事実・記事の根拠・文体の規則が優先。",
             "これは補助の参考で、決まりではない。"]  # fmt: skip
    for p in usable:
        lines.append(f"- {p['statement_ja']}")
    lines += [
        "- いつもの多様さを保つ (参考の値だけに寄せない。新しい書き方も試す)。",
        "- 直近 1〜2 本と同じ切り口・話題を避ける既存の方針はそのまま。",
    ]
    return lines


# -- まとめ --------------------------------------------------------------------------------


def build_analysis(inputs: Sequence[PostInput], *, as_of: datetime, tz: ZoneInfo,
                   measurement_policy, operations_policy,
                   thresholds: EvidenceThresholds) -> dict:  # fmt: skip
    """全体の分析 (決定的。``as_of`` までの観測だけ)。"""

    as_of = _aware(as_of)
    posts = [analyze_post(i, as_of=as_of, tz=tz, measurement_policy=measurement_policy,
                          operations_policy=operations_policy)
             for i in sorted(inputs, key=lambda i: i.record.publication_id)
             if _aware(i.record.published_at) <= as_of]  # fmt: skip
    cohorts = {}
    chosen = {}
    for lane in (LANE_REGULAR, LANE_GROWTH):
        counts = checkpoint_counts(posts, lane)
        checkpoint = comparison_checkpoint(counts, thresholds)
        chosen[lane] = checkpoint
        evaluate(posts, lane=lane, checkpoint=checkpoint, thresholds=thresholds)
        cohorts[lane] = {
            "posts": sum(1 for p in posts if p["lane"] == lane),
            "comparable_by_checkpoint": counts,
            "checkpoint": checkpoint,
            "comparable": counts.get(checkpoint, 0) if checkpoint else 0,
            "evidence": evidence_status(counts.get(checkpoint, 0) if checkpoint else 0,
                                        thresholds),
        }  # fmt: skip
    for post in posts:
        post.setdefault("evaluation", {"status": EVIDENCE_INSUFFICIENT,
                                       "reason": f"lane {post['lane']!r} is not compared"})
    usable = [p for p in posts if p["completeness"]["status"] != "no_insight"]
    regular = [p for p in posts if p["lane"] == LANE_REGULAR]
    growth = [p for p in posts if p["lane"] == LANE_GROWTH]
    warnings = []
    for lane, cohort in cohorts.items():
        if cohort["checkpoint"] is None and cohort["posts"]:
            warnings.append(f"{lane}: no equal-age checkpoint has {thresholds.minimum}+ "
                            "comparable posts; evaluations are insufficient_data")
        elif cohort["evidence"] in (EVIDENCE_HYPOTHESIS, EVIDENCE_PRELIMINARY):
            warnings.append(f"{lane}: comparisons at {cohort['checkpoint']} use "
                            f"{cohort['comparable']} posts ({cohort['evidence']})")
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "as_of_local": as_of.astimezone(tz).isoformat(timespec="minutes"),
        "read_only": True,
        "definitions": {
            "reach": "views at the equal-age checkpoint (cumulative)",
            "engagement": "likes + replies + reposts + quotes + shares (all five observed)",
            "conversation": "replies",
            "amplification": "reposts + quotes + shares",
            "engagement_rate": "engagement / views, only when views >= "
                               f"{measurement_policy.minimum_views_for_ratio}",
            "percentile_rank": "midrank among the other comparable posts of the same lane",
            "checkpoint_selection": "the latest checkpoint within the strongest evidence tier "
                                    "reached by the lane's comparable posts (at least "
                                    f"{thresholds.minimum})",
            "evidence": {"insufficient_data": f"< {thresholds.minimum}",
                         "hypothesis": f"{thresholds.minimum}-{thresholds.preliminary - 1}",
                         "preliminary": f"{thresholds.preliminary}-"
                                        f"{thresholds.descriptive - 1}",
                         "descriptive": f">= {thresholds.descriptive}"},
        },  # fmt: skip
        "summary": {
            "total_posts": len(posts),
            "posts_with_usable_insight": len(usable),
            "regular_posts": len(regular),
            "growth_posts": len(growth),
            "distributions": _distributions(posts, chosen),
        },
        "cohorts": cohorts,
        "posts": posts,
        "segments": segments(posts, thresholds=thresholds, checkpoints=chosen),
        "regular_segments": segments(regular, thresholds=thresholds, checkpoints=chosen),
        "growth_summary": {
            "posts": len(growth),
            "evidence": cohorts[LANE_GROWTH]["evidence"],
            "note": "growth posts are compared only with growth posts",
            "latest": [{"publication_id": p["publication_id"], "age_hours": p["age_hours"],
                        "metrics": p["latest_metrics"]} for p in growth],
        },  # fmt: skip
        "warnings": warnings,
        "limitations": [
            "descriptive only: associations between features and outcomes, not causes",
            "views/likes are cumulative; posts of different ages are compared only at "
            "equal-age checkpoints",
            "missing metrics are unknown, never zero; rates need enough views",
        ],
    }


def _distributions(posts: Sequence[dict], chosen: Mapping[str, str | None]) -> dict:
    out = {}
    for lane, checkpoint in chosen.items():
        if checkpoint is None:
            continue
        lane_out = {}
        for component in COMPONENTS:
            values = sorted(
                (p["checkpoints"][checkpoint].get("components") or {}).get(component)
                for p in posts
                if p["lane"] == lane
                and (p["checkpoints"][checkpoint].get("components") or {}).get(component)
                is not None
            )  # fmt: skip
            if not values:
                lane_out[component] = {"n": 0}
                continue
            q = statistics.quantiles(values, n=4, method="inclusive") if len(values) > 1 else [
                values[0]] * 3  # fmt: skip
            lane_out[component] = {"n": len(values), "min": values[0], "p25": q[0],
                                   "median": q[1], "p75": q[2], "max": values[-1]}  # fmt: skip
        out[lane] = {"checkpoint": checkpoint, "components": lane_out}
    return out


def filter_posts(report: Mapping, *, lane: str | None = None, topic: str | None = None,
                 angle: str | None = None, min_age_hours: float | None = None) -> list[dict]:
    """表示の絞り込み (分析そのものは全体で作る)。"""

    out = []
    for post in report.get("posts") or []:
        if lane and post["lane"] != lane:
            continue
        if topic and post["threads_topic"] != topic:
            continue
        if angle and post["angle"] != angle:
            continue
        if min_age_hours is not None and post["age_hours"] < min_age_hours:
            continue
        out.append(post)
    return out


__all__ = [
    "ACTIONABLE_DIMENSIONS", "COMPONENTS", "CONTEXT_DIMENSIONS", "DIRECTION_HIGHER",
    "DIRECTION_LOWER", "DIRECTION_NONE", "EVIDENCE_DESCRIPTIVE", "EVIDENCE_HYPOTHESIS",
    "EVIDENCE_INSUFFICIENT", "EVIDENCE_PRELIMINARY", "EvidenceThresholds", "FEEDBACK_SCHEMA",
    "LANE_GROWTH", "LANE_REGULAR", "LANE_UNKNOWN", "MODE_ADVISORY", "MODE_NEUTRAL", "NO_TOPIC",
    "PerformanceFeedback", "PostInput", "SCHEMA_VERSION", "analyze_post", "build_analysis",
    "build_feedback", "checkpoint_counts", "comparison_checkpoint", "components_of",
    "derived_metrics", "evaluate", "evidence_status", "filter_posts", "neutral_feedback",
    "percentile_rank", "position", "render_feedback_sections", "segments",
    "supported_values_for",
]  # fmt: skip
