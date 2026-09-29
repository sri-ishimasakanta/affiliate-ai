"""傾向の記述 (T6.5A-B、pure・読むだけ)。**記述であって、原因の主張でも学習でもない。**

- 中央値で要約する (平均は 1 本の大当たりに引っ張られる)。
- まとまりの本数が少なければ ``small_sample`` を付ける。少ない数から勝ち負けを決めない。
- 外の投稿の「抜け具合」(breakout) は、**同じ投稿者のほかの投稿** の中央値と比べる
  (その投稿自身は基準に入れない)。ほかの投稿が足りなければ基準を作らない。
- 見えない指標 (外の表示回数など) を作らない。欠けた値は数に入れない (0 にしない)。
- 生成・公開には何も戻さない (Luna への反映は後のフェーズ、人の判断のあと)。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from statistics import median

#: 記述の証拠の目安 (T6.5B.2、``threads_observation_policy.json`` の evidence_thresholds と同じ)。
#: どれも **原因の証拠ではない**。
#: - 外の投稿の使える数が 30 未満 → 傾向を判断できない (insufficient evidence)
#: - まとまりの本数が 10 未満 → 形として述べない (insufficient sample)
#: - 10 以上 → 候補の形 (candidate pattern) だけ
#: - 30 以上 → より強い記述ができる (それでも原因ではない)
EXTERNAL_MIN_USABLE_POSTS = 30
GROUP_CANDIDATE_MIN = 10
GROUP_STRONGER_MIN = 30
EVIDENCE_INSUFFICIENT_SAMPLE = "insufficient_sample"
EVIDENCE_CANDIDATE_PATTERN = "candidate_pattern"
EVIDENCE_STRONGER_DESCRIPTIVE = "stronger_descriptive"
#: 1 つのまとまりの中央値を「参考」と言える最小の本数。これ未満は small_sample。
MIN_GROUP_SAMPLE = GROUP_CANDIDATE_MIN
#: 投稿者の基準を作る最小の本数 (その投稿を除いた、ほかの投稿の数)。
MIN_AUTHOR_SAMPLE = 5
#: 基準のこの倍以上 → 「伸びた候補」。
BREAKOUT_CANDIDATE_RATIO = 3.0
#: 基準のこの倍以上 → 「ふだんより上」。
ABOVE_BASELINE_RATIO = 1.5

BREAKOUT_CANDIDATE = "candidate_breakout"
BREAKOUT_ABOVE = "above_baseline"
BREAKOUT_TYPICAL = "typical"
BREAKOUT_NO_BASELINE = "insufficient_author_baseline"
BREAKOUT_NO_METRIC = "metric_not_visible"
BREAKOUT_ZERO_BASELINE = "baseline_median_zero"

DESCRIPTIVE_NOTE = (
    "descriptive only: medians of what was observed. not a cause, not a ranking, "
    "and nothing here changes generation or publishing"
)


def group_evidence(n: int) -> str:
    """まとまりの本数 → 記述の証拠の段階 (原因ではない)。"""

    if n >= GROUP_STRONGER_MIN:
        return EVIDENCE_STRONGER_DESCRIPTIVE
    if n >= GROUP_CANDIDATE_MIN:
        return EVIDENCE_CANDIDATE_PATTERN
    return EVIDENCE_INSUFFICIENT_SAMPLE


def median_or_none(values: Iterable[int | float | None]) -> float | None:
    seen = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    return float(median(seen)) if seen else None


def group_summary(
    rows: Sequence[Mapping],
    key: str,
    metrics: Sequence[str],
    *,
    min_sample: int = MIN_GROUP_SAMPLE,
) -> dict[str, dict]:
    """``key`` の値ごとの本数と、指標ごとの中央値 (観測できた本数つき)。"""

    groups: dict[str, list[Mapping]] = defaultdict(list)
    for row in rows:
        value = row.get(key)
        groups["unknown" if value is None else str(value)].append(row)
    out: dict[str, dict] = {}
    for value, members in sorted(groups.items()):
        medians = {}
        observed = {}
        for metric in metrics:
            values = [m.get(metric) for m in members]
            medians[metric] = median_or_none(values)
            observed[metric] = sum(1 for v in values if v is not None)
        out[value] = {
            "n": len(members),
            "median": medians,
            "observed": observed,
            "small_sample": len(members) < min_sample,
            "evidence": group_evidence(len(members)),
        }
    return out


OBSERVED_HIGHER = "observed higher median"
OBSERVED_LOWER = "observed lower median"
INSUFFICIENT_SAMPLE = "insufficient sample"


def observed_extremes(groups: Mapping[str, Mapping], metric: str) -> dict:
    """十分な本数のまとまりの中で、中央値が最も高い / 低いと **観測された** まとまり。

    比べられるまとまり (本数が足り、中央値がある) が 2 つ未満なら ``insufficient sample``。
    "unknown" のまとまりは比べない。
    """

    eligible = [
        (name, group) for name, group in groups.items()
        if name != "unknown" and not group["small_sample"]
        and group["median"].get(metric) is not None
    ]  # fmt: skip
    if len(eligible) < 2:
        return {"status": INSUFFICIENT_SAMPLE, "comparable_groups": len(eligible)}
    ranked = sorted(eligible, key=lambda item: (-item[1]["median"][metric], item[0]))
    high, low = ranked[0], ranked[-1]
    if high[1]["median"][metric] == low[1]["median"][metric]:
        return {"status": "no observed difference", "comparable_groups": len(eligible)}
    return {
        "status": "compared",
        "comparable_groups": len(eligible),
        OBSERVED_HIGHER: {"group": high[0], "n": high[1]["n"], "median": high[1]["median"][metric]},
        OBSERVED_LOWER: {"group": low[0], "n": low[1]["n"], "median": low[1]["median"][metric]},
    }


def breakout_ratio(value: int | None, baseline_median: float | None) -> float | None:
    """その投稿の値 ÷ 投稿者の中央値 (小数 2 桁)。どちらかが無い・基準が 0 なら ``None``。"""

    if value is None or baseline_median is None or baseline_median <= 0:
        return None
    return round(value / baseline_median, 2)


def classify_breakout(value: int | None, baseline: Mapping) -> str:
    if value is None:
        return BREAKOUT_NO_METRIC
    if not baseline.get("sufficient"):
        return BREAKOUT_NO_BASELINE
    if not baseline.get("median"):
        return BREAKOUT_ZERO_BASELINE
    ratio = breakout_ratio(value, baseline["median"])
    if ratio is not None and ratio >= BREAKOUT_CANDIDATE_RATIO:
        return BREAKOUT_CANDIDATE
    if ratio is not None and ratio >= ABOVE_BASELINE_RATIO:
        return BREAKOUT_ABOVE
    return BREAKOUT_TYPICAL


def author_baseline(
    others: Iterable[int | None], *, min_sample: int = MIN_AUTHOR_SAMPLE
) -> dict:
    """同じ投稿者の **ほかの** 投稿の値から作る基準。足りなければ ``sufficient=False``。"""

    values = [v for v in others if v is not None]
    return {
        "n": len(values),
        "median": median_or_none(values),
        "sufficient": len(values) >= min_sample,
        "min_sample": min_sample,
    }


def author_breakouts(
    posts: Sequence[Mapping],
    *,
    metrics: Sequence[str] = ("likes", "replies"),
    min_sample: int = MIN_AUTHOR_SAMPLE,
) -> list[dict]:
    """投稿ごとの、投稿者の基準との比 (leave-one-out)。``posts`` は最新の観測を持つ行。

    ``breakouts`` (T6.5B.4): 伸びた候補になった指標ごとの結果 (``breakout_entry``)。表示は
    これだけを使う (どの指標で伸びたかを、表示の側で決め直さない)。
    """

    by_author: dict[str, list[Mapping]] = defaultdict(list)
    for post in posts:
        if post.get("author_handle"):
            by_author[post["author_handle"]].append(post)
    out = []
    for post in posts:
        author = post.get("author_handle")
        row = {"external_post_key": post.get("external_post_key"), "author_handle": author}
        for metric in metrics:
            others = [p.get(metric) for p in by_author.get(author, []) if p is not post]
            baseline = author_baseline(others, min_sample=min_sample)
            value = post.get(metric)
            row[metric] = value
            row[f"author_{metric}_baseline"] = baseline
            row[f"{metric}_breakout_ratio"] = (
                breakout_ratio(value, baseline["median"]) if baseline["sufficient"] else None
            )
            row[f"{metric}_breakout"] = classify_breakout(value, baseline)
        row["breakouts"] = [breakout_entry(row, metric) for metric in metrics
                            if row[f"{metric}_breakout"] == BREAKOUT_CANDIDATE]  # fmt: skip
        out.append(row)
    return out


def breakout_entry(row: Mapping, metric: str) -> dict:
    """1 つの指標の伸びた候補: 指標・その投稿の値・投稿者の中央値・比・比べた投稿の数。"""

    baseline = row[f"author_{metric}_baseline"]
    return {
        "metric": metric,
        "value": row[metric],
        "baseline_median": baseline["median"],
        "ratio": row[f"{metric}_breakout_ratio"],
        "baseline_n": baseline["n"],
        "min_sample": baseline["min_sample"],
    }


__all__ = [
    "BREAKOUT_ABOVE", "BREAKOUT_CANDIDATE", "BREAKOUT_NO_BASELINE", "BREAKOUT_NO_METRIC",
    "BREAKOUT_TYPICAL", "BREAKOUT_ZERO_BASELINE", "DESCRIPTIVE_NOTE", "MIN_AUTHOR_SAMPLE",
    "MIN_GROUP_SAMPLE", "EXTERNAL_MIN_USABLE_POSTS", "GROUP_CANDIDATE_MIN",
    "GROUP_STRONGER_MIN", "EVIDENCE_CANDIDATE_PATTERN", "EVIDENCE_INSUFFICIENT_SAMPLE",
    "EVIDENCE_STRONGER_DESCRIPTIVE", "group_evidence",
    "author_baseline", "author_breakouts", "breakout_entry", "breakout_ratio",
    "INSUFFICIENT_SAMPLE", "OBSERVED_HIGHER", "OBSERVED_LOWER", "classify_breakout",
    "group_summary", "median_or_none", "observed_extremes",
]  # fmt: skip
