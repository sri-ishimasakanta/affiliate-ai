"""Threads の学習 (T5、pure)。**控えめで、説明できることだけを言う。**

実際の Threads の結果から「どの切り口・長さ・時間帯が、これまでの成熟した投稿で
どう見えたか」を記述する。**言えるだけの証拠がなければ、そう言う。** それがこの
モジュールの第一の仕事である。

T4 の計測の規則をそのまま使う (別の成熟の仕組みを作らない):

- 成熟の判定は :func:`app.social.threads.measurement.classify_maturity`。
  比較に使うのは ``mature_enough_for_comparison`` (72 時間以降) の投稿だけ。
- 値ごとの本数の下限は ``comparison.minimum_mature_posts_per_dimension`` (3)。
- 比率を出す views の下限は ``comparison.minimum_views_for_ratio`` (30)。
- 長さの帯は :func:`app.social.threads.measurement.length_bucket`。
- 母数が足りないうちは中央値も比率も出さない。件数だけを示す。

**代表の観測 (canonical snapshot)** — 1 投稿につき 1 つだけ:

    成熟の境界 (72h) から ``comparable_window_hours`` (24h) 以内に観測できた
    最初の ``observed`` の snapshot。その窓の中に views の取れた snapshot があれば、
    その最初のものを優先する。窓が閉じても見つからなければ、その投稿は
    「比較できる観測が無い」として数えるだけで、例には入れない。

こうすると、1 時間後の値と 7 日後の値を同じものとして並べることがなく、同じ投稿を
2 回数えることもない。窓が閉じたあとの観測は代表を変えないので、後から数字が
揺れても所見は動かない (append-only の snapshot を書き換えないことと同じ理由)。

**欠測は 0 ではない。** views が取れていなければ「欠測」、views=0 なら「0」。
相互作用 (likes/replies/reposts/quotes/shares) は 5 つとも観測できたときだけ合計する。
1 つでも欠けていれば合計は ``None`` (欠けた分を 0 とみなさない)。

**所見 (finding)** は、同じ次元の中で証拠が足りた値どうしを、成熟した投稿の
``interaction_rate`` の **中央値** で比べた記述である:

- 中央値の差が ``minimum_relative_difference`` 以上で、かつ **どの 1 本を抜いても
  向きが変わらない** ときだけ ``observed_difference`` とする (外れ値 1 本で所見を
  作らない)。
- 一度立った所見は、差が ``retain_relative_difference`` を下回るか向きが変わるまで
  保つ (``weakening`` と明示する)。新しい証拠が入るたびに所見が点滅しないため。
- それ以外は ``no_clear_difference``。

所見は相関の記述であって、その次元が結果を生んだとは言わない。「最適」「勝ち」
「常に良い」とは書かない。不透明な総合スコアも作らない。

この結果はまだ提案の生成・並び順・公開には **一切つながない** (T5.5 以降)。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from zoneinfo import ZoneInfo

from app.models.threads_insight_snapshot import SNAPSHOT_OBSERVED
from app.operations.local_time import local_hour, local_weekday, to_local
from app.social.threads.measurement import INTERACTION_METRICS, classify_maturity, length_bucket
from app.social.threads.proposal import LINK_MODES
from app.social.threads.style import POST_ANGLES

#: 機械可読な出力の形の版。T5.5 はこの版を見て読み方を決める。
SCHEMA_VERSION = "threads-learning/1"

METRICS = ("views", *INTERACTION_METRICS)
UNKNOWN = "unknown"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# -- evidence status (値ごと・全体) ---------------------------------------------
STATUS_INSUFFICIENT_SAMPLE = "insufficient_sample"
STATUS_INSUFFICIENT_COMPARABLE = "insufficient_comparable_data"
STATUS_INSUFFICIENT_VIEWS = "insufficient_views"
STATUS_SUFFICIENT = "sufficient_evidence"
EVIDENCE_STATUSES = (
    STATUS_INSUFFICIENT_SAMPLE,
    STATUS_INSUFFICIENT_COMPARABLE,
    STATUS_INSUFFICIENT_VIEWS,
    STATUS_SUFFICIENT,
)

# -- 1 投稿の扱い ---------------------------------------------------------------
EVIDENCE_IMMATURE = "immature"
EVIDENCE_AWAITING = "awaiting_mature_observation"
EVIDENCE_MISSED = "no_comparable_observation"
EVIDENCE_COMPARABLE = "comparable"

# -- findings -----------------------------------------------------------------
FINDING_OBSERVED_DIFFERENCE = "observed_difference"
FINDING_NO_CLEAR_DIFFERENCE = "no_clear_difference"
COMPARISON_METRIC = "interaction_rate"

# -- dimensions ---------------------------------------------------------------
DIM_ANGLE = "angle"
DIM_SOURCE_ARTICLE = "source_article"
DIM_TOPIC = "topic"
DIM_LINK_MODE = "link_mode"
DIM_LENGTH = "length_band"
DIM_HOUR = "hour"
DIM_DAYPART = "daypart"
DIM_WEEKDAY = "weekday"
DIM_TRIGGER = "trigger"
DIMENSIONS = (
    DIM_ANGLE,
    DIM_SOURCE_ARTICLE,
    DIM_TOPIC,
    DIM_LINK_MODE,
    DIM_LENGTH,
    DIM_HOUR,
    DIM_DAYPART,
    DIM_WEEKDAY,
    DIM_TRIGGER,
)
TRIGGERS = ("manual", "automatic")


# == facts (DB から作る。ここでは読むだけ) =======================================
@dataclass(frozen=True)
class SnapshotFact:
    snapshot_id: int
    observed_at: datetime
    outcome: str
    #: 観測時に記録した経過時間。無ければ観測時刻と公開時刻から計算する。
    age_hours: float | None = None
    #: 観測できた指標だけ。**欠測は None** (0 ではない)。
    metrics: Mapping[str, int | None] = field(default_factory=dict)

    def metric(self, name: str) -> int | None:
        return self.metrics.get(name)


@dataclass(frozen=True)
class PublicationFact:
    publication_id: int
    published_at: datetime | None
    angle: str | None = None
    link_mode: str | None = None
    character_count: int | None = None
    trigger: str | None = None
    article_id: int | None = None
    article_title: str | None = None
    topic: str | None = None
    proposal_id: int | None = None
    snapshots: tuple[SnapshotFact, ...] = ()


@dataclass(frozen=True)
class Canonical:
    """ある時点での、1 投稿の学習上の扱い。"""

    state: str
    maturity_stage: str
    age_hours: float | None
    snapshot: SnapshotFact | None = None
    snapshot_age_hours: float | None = None


@dataclass(frozen=True)
class Example:
    """学習に使う 1 本 (成熟していて、代表の観測がある投稿)。"""

    publication: PublicationFact
    snapshot: SnapshotFact
    snapshot_age_hours: float
    views: int | None
    interactions: int | None
    interaction_rate: float | None


# == canonical snapshot =========================================================
def snapshot_age_hours(snapshot: SnapshotFact, publication: PublicationFact) -> float | None:
    if snapshot.age_hours is not None:
        return float(snapshot.age_hours)
    if publication.published_at is None:
        return None
    return _hours(_aware(snapshot.observed_at) - _aware(publication.published_at))


def _window(policy) -> tuple[float, float]:
    start = policy.mature_after_hours
    return start, start + policy.comparable_window_hours


def _candidates(publication: PublicationFact, as_of: datetime, policy) -> list[SnapshotFact]:
    start, end = _window(policy)
    found = []
    for snapshot in publication.snapshots:
        if snapshot.outcome != SNAPSHOT_OBSERVED:
            continue
        if _aware(snapshot.observed_at) > as_of:
            continue
        age = snapshot_age_hours(snapshot, publication)
        if age is not None and start <= age < end:
            found.append(snapshot)
    return sorted(found, key=lambda s: (_aware(s.observed_at), s.snapshot_id))


def _choose(candidates: Sequence[SnapshotFact]) -> SnapshotFact | None:
    for snapshot in candidates:
        if snapshot.metric("views") is not None:
            return snapshot
    return candidates[0] if candidates else None


def select_canonical(publication: PublicationFact, as_of: datetime, policy) -> Canonical:
    """``as_of`` の時点で、この投稿を学習にどう使うかを決める (決定的)。"""

    as_of = _aware(as_of)
    age = (
        _hours(as_of - _aware(publication.published_at))
        if publication.published_at is not None
        else None
    )
    verdict = classify_maturity(age, policy)
    if not verdict.comparable:
        return Canonical(EVIDENCE_IMMATURE, verdict.stage, verdict.age_hours)
    chosen = _choose(_candidates(publication, as_of, policy))
    if chosen is not None:
        return Canonical(
            EVIDENCE_COMPARABLE,
            verdict.stage,
            verdict.age_hours,
            snapshot=chosen,
            snapshot_age_hours=round(snapshot_age_hours(chosen, publication), 2),
        )
    _start, end = _window(policy)
    state = EVIDENCE_AWAITING if age is not None and age < end else EVIDENCE_MISSED
    return Canonical(state, verdict.stage, verdict.age_hours)


def change_points(publication: PublicationFact, as_of: datetime, policy) -> list[datetime]:
    """この投稿の代表の観測が変わる時刻 (高々 2 つ)。所見の再生に使う。"""

    candidates = _candidates(publication, _aware(as_of), policy)
    if not candidates:
        return []
    points = [_aware(candidates[0].observed_at)]
    with_views = next((s for s in candidates if s.metric("views") is not None), None)
    if with_views is not None and with_views is not candidates[0]:
        points.append(_aware(with_views.observed_at))
    return points


# == metrics ====================================================================
def interactions_of(snapshot: SnapshotFact) -> int | None:
    """相互作用の合計。**5 つとも観測できたときだけ。** 欠けた分を 0 とみなさない。"""

    values = [snapshot.metric(m) for m in INTERACTION_METRICS]
    if any(v is None for v in values):
        return None
    return int(sum(values))


def interaction_rate(views: int | None, interactions: int | None, policy) -> float | None:
    """interactions ÷ views。views が欠測・0・下限未満なら出さない (0.0 にしない)。"""

    if views is None or interactions is None:
        return None
    if views <= 0 or views < policy.minimum_views_for_ratio:
        return None
    return interactions / views


def build_example(publication: PublicationFact, canonical: Canonical, policy) -> Example | None:
    if canonical.state != EVIDENCE_COMPARABLE or canonical.snapshot is None:
        return None
    snapshot = canonical.snapshot
    views = snapshot.metric("views")
    interactions = interactions_of(snapshot)
    return Example(
        publication=publication,
        snapshot=snapshot,
        snapshot_age_hours=canonical.snapshot_age_hours,
        views=views,
        interactions=interactions,
        interaction_rate=interaction_rate(views, interactions, policy),
    )


# == dimensions =================================================================
def daypart_of(hour: int, dayparts: Iterable[Mapping]) -> str:
    for part in dayparts:
        start, end = int(part["start_hour"]), int(part["end_hour"])
        inside = start <= hour < end if start < end else hour >= start or hour < end
        if inside:
            return str(part["name"])
    return UNKNOWN


def dimension_values(publication: PublicationFact, policy, tz: ZoneInfo) -> dict[str, str]:
    """1 投稿の次元の値。**未知の値もそのまま文字列で残す** (将来の値で壊れない)。"""

    published = publication.published_at
    hour = local_hour(published, tz) if published is not None else None
    return {
        DIM_ANGLE: _text(publication.angle),
        DIM_SOURCE_ARTICLE: (
            str(publication.article_id) if publication.article_id is not None else UNKNOWN
        ),
        DIM_TOPIC: _text(publication.topic),
        DIM_LINK_MODE: _text(publication.link_mode),
        DIM_LENGTH: (
            length_bucket(publication.character_count, policy)
            if publication.character_count is not None
            else UNKNOWN
        ),
        DIM_HOUR: f"{hour:02d}" if hour is not None else UNKNOWN,
        DIM_DAYPART: daypart_of(hour, policy.dayparts) if hour is not None else UNKNOWN,
        DIM_WEEKDAY: local_weekday(published, tz) if published is not None else UNKNOWN,
        DIM_TRIGGER: _text(publication.trigger),
    }


def _vocabulary(dimension: str, policy) -> tuple[str, ...]:
    """証拠が 0 本でも並べる既知の値 (どこがまだ試されていないかを見せるため)。"""

    if dimension == DIM_ANGLE:
        return POST_ANGLES
    if dimension == DIM_LINK_MODE:
        return LINK_MODES
    if dimension == DIM_LENGTH:
        return tuple(str(b["name"]) for b in policy.length_buckets)
    if dimension == DIM_DAYPART:
        return tuple(str(p["name"]) for p in policy.dayparts)
    if dimension == DIM_WEEKDAY:
        return WEEKDAYS
    if dimension == DIM_TRIGGER:
        return TRIGGERS
    return ()


def _ordered_values(dimension: str, present: Iterable[str], policy) -> list[str]:
    known = list(_vocabulary(dimension, policy))
    extra = sorted({v for v in present if v not in known and v != UNKNOWN}, key=_sort_key)
    tail = [UNKNOWN] if UNKNOWN in set(present) else []
    return known + extra + tail


def _label(dimension: str, value: str, publications: Sequence[PublicationFact], policy) -> str:
    if dimension == DIM_SOURCE_ARTICLE:
        for publication in publications:
            if publication.article_title:
                return publication.article_title
    if dimension == DIM_HOUR and value != UNKNOWN:
        return f"{value}:00-{value}:59"
    if dimension == DIM_DAYPART:
        for part in policy.dayparts:
            if part["name"] == value:
                return f"{int(part['start_hour']):02d}:00-{int(part['end_hour']):02d}:00"
    return value


# == aggregation ================================================================
def evidence_status(*, mature: int, complete: int, eligible: int, policy) -> tuple[str, list[str]]:
    """値ごとの証拠の状態と、その理由 (人が読める文)。"""

    minimum = policy.minimum_mature_posts
    start, end = _window(policy)
    if mature < minimum:
        return STATUS_INSUFFICIENT_SAMPLE, [
            f"{mature} mature post(s) (>= {start:g}h); {minimum} needed before comparing"
        ]
    if complete < minimum:
        return STATUS_INSUFFICIENT_COMPARABLE, [
            f"{complete} mature post(s) have a complete observation at {start:g}-{end:g}h; "
            f"{minimum} needed"
        ]
    if eligible < minimum:
        return STATUS_INSUFFICIENT_VIEWS, [
            f"{eligible} mature post(s) reached {policy.minimum_views_for_ratio} views; "
            f"{minimum} needed before a rate is compared"
        ]
    return STATUS_SUFFICIENT, []


def summarize(
    canonicals: Sequence[tuple[PublicationFact, Canonical]],
    examples: Sequence[Example],
    policy,
    tz: ZoneInfo,
) -> dict:
    """1 つの値 (または全体) の証拠。生の件数は常に、中央値は母数が足りたときだけ出す。"""

    minimum = policy.minimum_mature_posts
    mature = sum(1 for _p, c in canonicals if c.state != EVIDENCE_IMMATURE)
    views = [e.views for e in examples if e.views is not None]
    complete = [e for e in examples if e.views is not None and e.interactions is not None]
    rated = [e for e in examples if e.interaction_rate is not None]
    status, reasons = evidence_status(
        mature=mature, complete=len(complete), eligible=len(rated), policy=policy
    )

    median_views = _median(views) if len(views) >= minimum else None
    outliers = []
    if median_views:
        outliers = [
            e.publication.publication_id
            for e in examples
            if e.views is not None and e.views > policy.outlier_ratio_to_median * median_views
        ]
    if outliers:
        reasons = [
            *reasons,
            f"publication(s) {', '.join(f'#{i}' for i in outliers)} exceed "
            f"{policy.outlier_ratio_to_median:g}x the median views; medians are used, not totals",
        ]

    rates = [e.interaction_rate for e in rated]
    show_rate = status == STATUS_SUFFICIENT
    published = [_aware(p.published_at) for p, c in canonicals if c.state == EVIDENCE_COMPARABLE]
    ages = [e.snapshot_age_hours for e in examples]
    return {
        "status": status,
        "reasons": reasons,
        "counts": {
            "publications": len(canonicals),
            "immature": sum(1 for _p, c in canonicals if c.state == EVIDENCE_IMMATURE),
            "mature": mature,
            "comparable": len(examples),
            "awaiting_observation": sum(1 for _p, c in canonicals if c.state == EVIDENCE_AWAITING),
            "no_comparable_observation": sum(
                1 for _p, c in canonicals if c.state == EVIDENCE_MISSED
            ),
        },
        "missing": {
            "views": sum(1 for e in examples if e.views is None),
            "interactions": sum(1 for e in examples if e.interactions is None),
        },
        "zero_views": sum(1 for e in examples if e.views == 0),
        "below_minimum_views": sum(
            1 for e in examples if e.views is not None and e.views < policy.minimum_views_for_ratio
        ),
        "views": {
            "observed": len(views),
            "total": sum(views) if views else None,
            "median": median_views,
            "min": min(views) if median_views is not None else None,
            "max": max(views) if median_views is not None else None,
        },
        "interactions": {
            "observed": len(complete),
            "total": sum(e.interactions for e in complete) if complete else None,
            "median": (
                _median([e.interactions for e in complete]) if len(complete) >= minimum else None
            ),
        },
        "metric_totals": {
            metric: _metric_total(examples, metric) for metric in INTERACTION_METRICS
        },
        "interaction_rate": {
            "eligible": len(rated),
            "median": _round(_median(rates)) if show_rate else None,
            "min": _round(min(rates)) if show_rate else None,
            "max": _round(max(rates)) if show_rate else None,
        },
        "outliers": outliers,
        "window": {
            "first_published_local": _local(min(published), tz) if published else None,
            "last_published_local": _local(max(published), tz) if published else None,
            "snapshot_age_hours": [min(ages), max(ages)] if ages else None,
        },
        "publication_ids": [e.publication.publication_id for e in examples],
    }


def _metric_total(examples: Sequence[Example], metric: str) -> dict:
    observed = [e.snapshot.metric(metric) for e in examples]
    observed = [v for v in observed if v is not None]
    return {"observed": len(observed), "total": sum(observed) if observed else None}


# == findings ===================================================================
def _rates_by_value(examples: Sequence[Example], dimension: str, policy, tz) -> dict:
    grouped: dict[str, list[tuple[int, float]]] = {}
    for example in examples:
        if example.interaction_rate is None:
            continue
        value = dimension_values(example.publication, policy, tz)[dimension]
        grouped.setdefault(value, []).append(
            (example.publication.publication_id, example.interaction_rate)
        )
    return grouped


def _leave_one_out_holds(higher: list[float], lower: list[float]) -> bool:
    """どちらの群からどの 1 本を抜いても、中央値の向きが変わらないか。"""

    base_high, base_low = _median(higher), _median(lower)
    for i in range(len(higher)):
        if not _greater(_median(higher[:i] + higher[i + 1 :]), base_low):
            return False
    for i in range(len(lower)):
        if not _greater(base_high, _median(lower[:i] + lower[i + 1 :])):
            return False
    return True


def compute_findings(
    examples: Sequence[Example],
    policy,
    tz: ZoneInfo,
    *,
    previous: Mapping[str, Mapping] | None,
    at: datetime,
) -> tuple[list[dict], dict[str, dict]]:
    """証拠が足りた値どうしの比較。``previous`` は直前の状態 (ヒステリシスのため)。"""

    previous = previous or {}
    minimum = policy.minimum_mature_posts
    findings: list[dict] = []
    state: dict[str, dict] = {}
    for dimension in DIMENSIONS:
        if dimension in policy.diagnostic_dimensions:
            continue
        grouped = _rates_by_value(examples, dimension, policy, tz)
        sufficient = [
            value
            for value in _ordered_values(dimension, grouped, policy)
            if len(grouped.get(value, ())) >= minimum
        ]
        for a, b in combinations(sufficient, 2):
            finding = _compare(dimension, a, b, grouped, policy, previous, at)
            findings.append(finding)
            state[finding["id"]] = {
                "kind": finding["kind"],
                "higher": finding["higher"],
                "since": finding["since"],
            }
    return findings, state


def _compare(dimension, a, b, grouped, policy, previous, at) -> dict:
    finding_id = f"{dimension}:{a}|{b}"
    rates = {v: [r for _i, r in grouped[v]] for v in (a, b)}
    medians = {v: _median(rates[v]) for v in (a, b)}
    higher = lower = None
    relative = 0.0
    consistent = False
    if _greater(medians[a], medians[b]) or _greater(medians[b], medians[a]):
        higher, lower = (a, b) if medians[a] > medians[b] else (b, a)
        # 閾値 (0.2 / 0.1) との比較が浮動小数の誤差で揺れないよう丸めておく。
        relative = round((medians[higher] - medians[lower]) / medians[higher], 9)
        consistent = _leave_one_out_holds(rates[higher], rates[lower])

    prior = previous.get(finding_id) or {}
    held = prior.get("kind") == FINDING_OBSERVED_DIFFERENCE and prior.get("higher") == higher
    weakening = False
    if higher is not None and consistent and relative >= policy.minimum_relative_difference:
        kind = FINDING_OBSERVED_DIFFERENCE
    elif (
        higher is not None and consistent and held and relative >= policy.retain_relative_difference
    ):
        kind = FINDING_OBSERVED_DIFFERENCE
        weakening = True
    else:
        kind = FINDING_NO_CLEAR_DIFFERENCE
    reported_higher = higher if kind == FINDING_OBSERVED_DIFFERENCE else None
    same = prior.get("kind") == kind and prior.get("higher") == reported_higher
    since = prior.get("since") if same and prior.get("since") else _iso(at)

    sides = []
    for value in (a, b):
        ids = [i for i, _r in grouped[value]]
        sides.append(
            {
                "value": value,
                "posts": len(ids),
                "median_interaction_rate": _round(medians[value]),
                "min_interaction_rate": _round(min(rates[value])),
                "max_interaction_rate": _round(max(rates[value])),
                "publication_ids": ids,
            }
        )
    return {
        "id": finding_id,
        "dimension": dimension,
        "metric": COMPARISON_METRIC,
        "kind": kind,
        "higher": reported_higher,
        "lower": lower if kind == FINDING_OBSERVED_DIFFERENCE else None,
        "values": sides,
        "relative_difference": _round(relative),
        "leave_one_out_consistent": consistent,
        "weakening": weakening,
        "since": since,
        "statement": _statement(dimension, kind, higher, lower, sides, relative, weakening, policy),
        "uncertainty": [
            f"n={sides[0]['posts']} vs n={sides[1]['posts']} mature posts",
            "medians are descriptive; no significance test is run",
            "correlation in this sample, not evidence that the dimension caused it",
            (
                "leave-one-out consistent"
                if consistent
                else "not consistent when one post is left out"
            ),
        ],
    }


def _statement(dimension, kind, higher, lower, sides, relative, weakening, policy) -> str:
    by_value = {s["value"]: s for s in sides}
    if kind == FINDING_OBSERVED_DIFFERENCE:
        hi, lo = by_value[higher], by_value[lower]
        text = (
            f"In the current mature sample, {dimension}={higher} has shown a higher median "
            f"interaction rate ({hi['median_interaction_rate']}, n={hi['posts']}) than "
            f"{dimension}={lower} ({lo['median_interaction_rate']}, n={lo['posts']})."
        )
        if weakening:
            text += (
                f" The gap has narrowed below {policy.minimum_relative_difference:g}; it is "
                f"kept until it falls below {policy.retain_relative_difference:g}."
            )
        return text
    a, b = sides[0]["value"], sides[1]["value"]
    return (
        f"{dimension}={a} and {dimension}={b} both have sufficient evidence, but no robust "
        f"difference in median interaction rate is visible (relative difference "
        f"{relative:.2f}; {policy.minimum_relative_difference:g} is needed, and it must hold "
        f"when any single post is left out)."
    )


# == analysis ===================================================================
def examples_as_of(
    publications: Sequence[PublicationFact], as_of: datetime, policy
) -> tuple[list[tuple[PublicationFact, Canonical]], list[Example]]:
    canonicals = [(p, select_canonical(p, as_of, policy)) for p in publications]
    examples = [e for p, c in canonicals if (e := build_example(p, c, policy)) is not None]
    return canonicals, examples


def analyze(
    publications: Sequence[PublicationFact], *, as_of: datetime, policy, tz: ZoneInfo
) -> dict:
    """``as_of`` の時点の学習結果 (決定的)。``as_of`` より後の観測は見ない。"""

    as_of = _aware(as_of)
    visible = [
        p
        for p in sorted(publications, key=lambda p: p.publication_id)
        if p.published_at is not None and _aware(p.published_at) <= as_of
    ]

    # 所見は、代表の観測が変わった時刻ごとに順に作り直す (ヒステリシスの状態を運ぶ)。
    # 代表の観測は窓が閉じたら変わらないので、同じ入力からは常に同じ所見になる。
    points = sorted({t for p in visible for t in change_points(p, as_of, policy)})
    state: dict[str, dict] = {}
    findings: list[dict] = []
    for point in points:
        _c, at_point = examples_as_of(visible, point, policy)
        findings, state = compute_findings(at_point, policy, tz, previous=state, at=point)

    canonicals, examples = examples_as_of(visible, as_of, policy)
    overall = summarize(canonicals, examples, policy, tz)
    dimensions = {
        dimension: _dimension_report(dimension, canonicals, examples, policy, tz)
        for dimension in DIMENSIONS
    }
    comparable = [d for d, report in dimensions.items() if report["comparable"]]
    start, end = _window(policy)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(as_of),
        "generated_at_local": _local(as_of, tz),
        "local_timezone": tz.key,
        "policy_version": policy.policy_version,
        "thresholds": {
            "mature_after_hours": start,
            "comparable_snapshot_window_hours": [start, end],
            "minimum_mature_posts_per_value": policy.minimum_mature_posts,
            "minimum_views_for_rate": policy.minimum_views_for_ratio,
            "minimum_relative_difference": policy.minimum_relative_difference,
            "retain_relative_difference": policy.retain_relative_difference,
            "outlier_ratio_to_median": policy.outlier_ratio_to_median,
        },
        "canonical_snapshot_rule": policy.canonical_snapshot_rule,
        "counts": {
            "total": overall["counts"]["publications"],
            "mature": overall["counts"]["mature"],
            "immature": overall["counts"]["immature"],
            "comparable": overall["counts"]["comparable"],
            "missing_comparable_data": overall["counts"]["awaiting_observation"]
            + overall["counts"]["no_comparable_observation"],
            "awaiting_observation": overall["counts"]["awaiting_observation"],
            "no_comparable_observation": overall["counts"]["no_comparable_observation"],
        },
        "evidence_status": overall["status"],
        "evidence_reasons": overall["reasons"],
        "comparable_dimensions": comparable,
        "overall": overall,
        "dimensions": dimensions,
        "findings": findings,
        "publications": [_publication_report(p, c, policy, tz) for p, c in canonicals],
        "caveats": _caveats(policy),
    }


def _dimension_report(dimension, canonicals, examples, policy, tz) -> dict:
    by_value: dict[str, list[tuple[PublicationFact, Canonical]]] = {}
    for publication, canonical in canonicals:
        value = dimension_values(publication, policy, tz)[dimension]
        by_value.setdefault(value, []).append((publication, canonical))
    by_publication = {e.publication.publication_id: e for e in examples}
    values = []
    for value in _ordered_values(dimension, by_value, policy):
        members = by_value.get(value, [])
        member_examples = [
            by_publication[p.publication_id]
            for p, _c in members
            if p.publication_id in by_publication
        ]
        values.append(
            {
                "value": value,
                "label": _label(dimension, value, [p for p, _c in members], policy),
                **summarize(members, member_examples, policy, tz),
            }
        )
    diagnostic = dimension in policy.diagnostic_dimensions
    sufficient = [v for v in values if v["status"] == STATUS_SUFFICIENT]
    return {
        "diagnostic_only": diagnostic,
        # 比較できる = 証拠の足りた値が 2 つ以上ある (診断用の次元は比較しない)。
        "comparable": not diagnostic and len(sufficient) >= 2,
        "values": values,
    }


def _publication_report(publication, canonical, policy, tz) -> dict:
    example = build_example(publication, canonical, policy)
    snapshot = canonical.snapshot
    return {
        "publication_id": publication.publication_id,
        "proposal_id": publication.proposal_id,
        "article_id": publication.article_id,
        "published_at_local": (
            _local(_aware(publication.published_at), tz) if publication.published_at else None
        ),
        "age_hours": canonical.age_hours,
        "maturity_stage": canonical.maturity_stage,
        "evidence_state": canonical.state,
        "dimensions": dimension_values(publication, policy, tz),
        "character_count": publication.character_count,
        "snapshot": (
            {
                "snapshot_id": snapshot.snapshot_id,
                "observed_at_local": _local(_aware(snapshot.observed_at), tz),
                "age_hours": canonical.snapshot_age_hours,
            }
            if snapshot is not None
            else None
        ),
        # 学習に使わない (若い・代表の観測が無い) 投稿の数字は出さない。T4 の報告を見る。
        "metrics": ({m: snapshot.metric(m) for m in METRICS} if example is not None else None),
        "missing_metrics": (
            [m for m in METRICS if snapshot.metric(m) is None] if example is not None else None
        ),
        "interactions": example.interactions if example else None,
        "interaction_rate": _round(example.interaction_rate) if example else None,
    }


def _caveats(policy) -> list[str]:
    caveats = [
        "Descriptive only: a difference in this sample is a correlation, not evidence that "
        "the dimension caused it.",
        "Only mature posts (>= the T4 comparison age) with a comparable observation are used.",
        "Missing metrics are not zero; a post with missing views is counted as missing.",
        "Trigger (manual/automatic) is diagnostic only and is never compared.",
        "Account growth over time can move views independently of any post attribute.",
        "T5 does not change proposal generation, queue ordering or publication.",
    ]
    return caveats + [f"{k}: {v}" for k, v in sorted(policy.metric_caveats.items())]


# == changes since a baseline ===================================================
def compare_reports(current: Mapping, baseline: Mapping) -> dict:
    """2 つの時点の学習結果の差。**証拠が変わっていなければ「変化なし」と言う。**"""

    def example_ids(report):
        return {
            p["publication_id"]
            for p in report["publications"]
            if p["evidence_state"] == EVIDENCE_COMPARABLE
        }

    def statuses(report):
        return {
            (dimension, value["value"]): value["status"]
            for dimension, data in report["dimensions"].items()
            for value in data["values"]
        }

    new_examples = sorted(example_ids(current) - example_ids(baseline))
    before, after = statuses(baseline), statuses(current)
    status_changes = [
        {"dimension": d, "value": v, "from": before.get((d, v)), "to": after[(d, v)]}
        for (d, v) in after
        if before.get((d, v)) != after[(d, v)]
    ]
    sufficiency_changes = [c for c in status_changes if STATUS_SUFFICIENT in (c["from"], c["to"])]

    old = {f["id"]: f for f in baseline["findings"]}
    new = {f["id"]: f for f in current["findings"]}
    finding_changes = []
    for finding_id, finding in new.items():
        prior = old.get(finding_id)
        if prior is None:
            finding_changes.append({"id": finding_id, "change": "added", "to": finding["kind"]})
        elif (prior["kind"], prior["higher"], prior["weakening"]) != (
            finding["kind"],
            finding["higher"],
            finding["weakening"],
        ):
            change = "weakened" if finding["weakening"] and not prior["weakening"] else "changed"
            finding_changes.append(
                {
                    "id": finding_id,
                    "change": change,
                    "from": {"kind": prior["kind"], "higher": prior["higher"]},
                    "to": {"kind": finding["kind"], "higher": finding["higher"]},
                }
            )
    for finding_id in old.keys() - new.keys():
        finding_changes.append({"id": finding_id, "change": "withdrawn"})
    finding_changes.sort(key=lambda c: c["id"])

    overall = (
        None
        if baseline["evidence_status"] == current["evidence_status"]
        else {"from": baseline["evidence_status"], "to": current["evidence_status"]}
    )
    material = bool(new_examples or finding_changes or sufficiency_changes or overall)
    return {
        "baseline_at": baseline["generated_at"],
        "baseline_at_local": baseline["generated_at_local"],
        "material": material,
        "summary": (
            "no material change"
            if not material
            else f"{len(new_examples)} new mature example(s), "
            f"{len(finding_changes)} finding change(s)"
        ),
        "overall_status_change": overall,
        "new_examples": new_examples,
        "status_changes": status_changes,
        "finding_changes": finding_changes,
    }


# == helpers ====================================================================
def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _greater(a: float, b: float) -> bool:
    """``a > b`` だが、浮動小数の誤差 (0.030000000000000002 > 0.03) は差と見なさない。"""

    return a > b and not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _hours(delta) -> float:
    return delta.total_seconds() / 3600.0


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return _aware(moment).astimezone(UTC).isoformat()


def _local(moment: datetime, tz: ZoneInfo) -> str:
    return to_local(moment, tz).isoformat(timespec="minutes")


def _text(value) -> str:
    return str(value) if value not in (None, "") else UNKNOWN


def _sort_key(value: str):
    return (0, int(value), "") if value.isdigit() else (1, 0, value)


__all__ = [
    "DIMENSIONS",
    "EVIDENCE_STATUSES",
    "FINDING_NO_CLEAR_DIFFERENCE",
    "FINDING_OBSERVED_DIFFERENCE",
    "SCHEMA_VERSION",
    "PublicationFact",
    "SnapshotFact",
    "analyze",
    "compare_reports",
    "select_canonical",
]
