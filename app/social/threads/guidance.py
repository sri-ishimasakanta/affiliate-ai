"""Threads 投稿案の生成への、学習からの **弱い参考** (T5.5、pure)。

T5 の学習結果 (``threads-learning/1``) を読んで、「成熟した過去の証拠からすると、
生成で何を **少しだけ** 多め・控えめに考えてよいか」を作る。**何を強制するか** は
作らない。

ここでは学習を計算し直さない。成熟・代表の観測・証拠の閾値・統計・ヒステリシスは
すべて T5 (:mod:`app.social.threads.learning`) の結果をそのまま使う。読むのは
``findings`` (証拠の足りた値どうしの比較) と ``dimensions`` (中立の理由) だけである。

規則 (決定的・説明できるものだけ。スコアは作らない):

- 使うのは ``observed_difference`` の所見だけ。``no_clear_difference`` や証拠不足の
  次元は **中立**。所見が 1 つも無ければ全体が中立 (``mode = "neutral"``)。
- 強さは ``weak`` だけ (V1)。
- ある値が、どれかの所見で上で、どの所見でも下でなければ ``prefer``。
  差が縮まりつつある (``weakening``) 所見だけに支えられていれば ``tentative``。
- ``de-emphasize`` はさらに慎重に: 下だった所見が ``weakening`` でなく、1 本を抜いても
  向きが変わらない (``leave_one_out_consistent``) ときだけ。禁止にはしない。
- 同じ値が上でも下でもあれば中立 (矛盾する証拠からは何も言わない)。
- ``trigger`` は診断用で使わない。``hour`` は V1 では細かすぎるので使わない。
  ``source_article`` は記事の中身そのものが交絡するので使わない (``topic`` を使う)。
- 生成器が作れない値 (将来の link_mode など) の参考、またはそうした値と比べて得た参考は
  ``actionable = False`` で、prompt には入れない (壊れない・根拠なく別の値へ寄せない)。
- ``topic`` / ``daypart`` / ``weekday`` は文面を変えない **文脈の情報** で、prompt には
  入れない。公開の時刻は worker が決め、T5.5 は変えない。

この参考は、承認・公開・既存の提案・承認済みの並びには一切触れない。影響するのは
これから作る提案の prompt (と、その来歴) だけである。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.social.threads.learning import (
    FINDING_OBSERVED_DIFFERENCE,
)
from app.social.threads.learning import (
    SCHEMA_VERSION as LEARNING_SCHEMA,
)

GUIDANCE_SCHEMA = "threads-generation-guidance/1"

MODE_NEUTRAL = "neutral"
MODE_WEAK = "weak"

DIRECTION_PREFER = "prefer"
DIRECTION_DEEMPHASIZE = "de-emphasize"
STRENGTH_WEAK = "weak"

#: 文面に効く次元 (prompt に入れる)。
PROMPT_DIMENSIONS = ("angle", "link_mode", "length_band")
#: 文面を変えない文脈の次元 (PLAN と来歴にだけ出す)。
CONTEXT_DIMENSIONS = ("topic", "daypart", "weekday")
GUIDANCE_DIMENSIONS = PROMPT_DIMENSIONS + CONTEXT_DIMENSIONS
#: 使わない次元と、その理由。
IGNORED_DIMENSIONS = {
    "trigger": "diagnostic only; manual vs automatic never influences generation",
    "hour": "too granular for generation guidance in V1 (daypart is used instead)",
    "source_article": "one article's own content confounds the comparison (topic is used)",
}

SAFEGUARDS = (
    "guidance is weak and advisory; it never forces, removes or rejects a value",
    "every requested angle is still written",
    "article links remain allowed; posts without links remain allowed",
    "short, medium and long posts all remain valid",
    "each batch keeps at least one proposal outside any preferred value (exploration)",
    "the factual boundary, style rules and validators always take precedence",
    "trigger (manual/automatic) never influences generation",
    "publication timing, queue order and approvals are unchanged",
)


@dataclass(frozen=True)
class Preference:
    dimension: str
    value: str
    direction: str
    strength: str = STRENGTH_WEAK
    #: 差が縮まりつつある所見だけに支えられている (参考程度)。
    tentative: bool = False
    #: 生成器が作れる値か (作れない値の参考は prompt に入れない)。
    actionable: bool = True
    evidence: tuple[Mapping, ...] = ()

    def as_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "direction": self.direction,
            "strength": self.strength,
            "tentative": self.tentative,
            "actionable": self.actionable,
            "evidence": [dict(e) for e in self.evidence],
        }


@dataclass(frozen=True)
class ThreadsGenerationGuidance:
    as_of: str
    as_of_local: str
    source_schema: str
    source_policy_version: str
    evidence_status: str
    preferences: tuple[Preference, ...] = ()
    #: 中立の次元と理由。
    neutral: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()
    length_bands: Mapping[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        return MODE_WEAK if any(p.actionable for p in self.prompt_preferences) else MODE_NEUTRAL

    @property
    def prompt_preferences(self) -> tuple[Preference, ...]:
        return tuple(p for p in self.preferences if p.dimension in PROMPT_DIMENSIONS)

    @property
    def context_preferences(self) -> tuple[Preference, ...]:
        return tuple(p for p in self.preferences if p.dimension in CONTEXT_DIMENSIONS)

    def content(self) -> dict:
        """指紋の対象 (時刻を含まない。同じ証拠からは同じ参考)。"""

        return {
            "schema": GUIDANCE_SCHEMA,
            "source_schema": self.source_schema,
            "source_policy_version": self.source_policy_version,
            "evidence_status": self.evidence_status,
            "mode": self.mode,
            "preferences": [p.as_dict() for p in self.preferences],
            "neutral_dimensions": [{"dimension": d, "reason": r} for d, r in self.neutral],
            "ignored_dimensions": [
                {"dimension": d, "reason": r} for d, r in IGNORED_DIMENSIONS.items()
            ],
            "notes": list(self.notes),
            "length_bands": {k: list(v) for k, v in self.length_bands.items()},
            "safeguards": list(SAFEGUARDS),
        }

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.content(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {
            **self.content(),
            "as_of": self.as_of,
            "as_of_local": self.as_of_local,
            "fingerprint": self.fingerprint,
        }

    def provenance(self, *, verified_against_prompt: bool) -> dict:
        """提案 1 件に残す小さな来歴。分析の中身は複写しない (as_of から作り直せる)。"""

        return {
            "schema": GUIDANCE_SCHEMA,
            "applied": self.mode == MODE_WEAK,
            "mode": self.mode,
            "fingerprint": self.fingerprint,
            "as_of": self.as_of,
            "learning_schema": self.source_schema,
            "learning_policy_version": self.source_policy_version,
            "evidence_status": self.evidence_status,
            "preferences": [
                {"dimension": p.dimension, "value": p.value, "direction": p.direction}
                for p in self.preferences
                if p.actionable
            ],
            # --guidance-fingerprint で、prompt を作ったときの参考と同じだと確かめたか。
            "verified_against_prompt": verified_against_prompt,
        }


def build_guidance(
    report: Mapping,
    *,
    supported_values: Mapping[str, Sequence[str]],
    length_bands: Mapping[str, tuple[int, int]] | None = None,
) -> ThreadsGenerationGuidance:
    """T5 の学習結果から参考を作る (決定的)。"""

    base = {
        "as_of": str(report.get("generated_at", "")),
        "as_of_local": str(report.get("generated_at_local", "")),
        "source_schema": str(report.get("schema_version", "")),
        "source_policy_version": str(report.get("policy_version", "")),
        "evidence_status": str(report.get("evidence_status", "")),
        "length_bands": dict(length_bands or {}),
    }
    if report.get("schema_version") != LEARNING_SCHEMA:
        # 読めない形の学習結果からは何も言わない (安全側で中立)。
        return ThreadsGenerationGuidance(
            **base,
            neutral=tuple(
                (d, f"unsupported learning schema {report.get('schema_version')!r}")
                for d in GUIDANCE_DIMENSIONS
            ),
            notes=("the learning report could not be read; guidance is neutral",),
        )

    findings = [f for f in report.get("findings", ()) if f.get("dimension") in GUIDANCE_DIMENSIONS]
    preferences: list[Preference] = []
    neutral: list[tuple[str, str]] = []
    for dimension in GUIDANCE_DIMENSIONS:
        found, reason = _dimension_preferences(
            dimension,
            [f for f in findings if f["dimension"] == dimension],
            report.get("dimensions", {}).get(dimension, {}),
            supported_values.get(dimension),
        )
        preferences.extend(found)
        if not found:
            neutral.append((dimension, reason))

    notes = []
    if not preferences:
        notes.append(
            "No evidence-sufficient Threads learning is available. Preserve normal diversity "
            "and editorial defaults."
        )
    return ThreadsGenerationGuidance(
        **base, preferences=tuple(preferences), neutral=tuple(neutral), notes=tuple(notes)
    )


def _dimension_preferences(dimension, findings, dimension_report, supported):
    if not findings:
        values = dimension_report.get("values", ())
        sufficient = sum(1 for v in values if v.get("status") == "sufficient_evidence")
        return [], (
            f"insufficient evidence ({sufficient} value(s) with sufficient mature evidence; "
            "2 are needed to compare)"
        )
    observed = [f for f in findings if f.get("kind") == FINDING_OBSERVED_DIFFERENCE]
    if not observed:
        return [], "evidence is sufficient, but no clear difference is visible"

    higher: dict[str, list[Mapping]] = {}
    lower: dict[str, list[Mapping]] = {}
    for finding in observed:
        higher.setdefault(finding["higher"], []).append(finding)
        lower.setdefault(finding["lower"], []).append(finding)

    order = [v.get("value") for v in dimension_report.get("values", ())]
    values = sorted(set(higher) | set(lower), key=lambda v: (_index(order, v), v))
    actionable_values = set(supported) if supported is not None else None
    found: list[Preference] = []
    for value in values:
        if value in higher and value in lower:
            continue  # 矛盾する証拠からは何も言わない
        if value in higher:
            support = higher[value]
            direction = DIRECTION_PREFER
            tentative = all(f.get("weakening") for f in support)
        else:
            # 控えめにするのは、安定した (縮まっていない・1 本に依存しない) 所見だけ。
            support = [
                f
                for f in lower[value]
                if not f.get("weakening") and f.get("leave_one_out_consistent")
            ]
            if not support:
                continue
            direction = DIRECTION_DEEMPHASIZE
            tentative = False
        # 比べた相手も生成器が作れる値のときだけ、prompt に入れてよい。作れない値
        # (将来の link_mode など) との比較で、作れる値を控えめにすると、根拠の無い
        # 別の値へ寄せることになるからである。
        actionable = actionable_values is None or (
            value in actionable_values
            and all(_other(f, value) in actionable_values for f in support)
        )
        found.append(
            Preference(
                dimension=dimension,
                value=value,
                direction=direction,
                tentative=tentative,
                actionable=actionable,
                evidence=tuple(_evidence(f, value) for f in support),
            )
        )
    if not found:
        return [], "differences conflict or are narrowing; no guidance is drawn"
    return found, ""


def _other(finding: Mapping, value: str) -> str:
    return finding["lower"] if finding["higher"] == value else finding["higher"]


def _evidence(finding: Mapping, value: str) -> dict:
    sides = {s["value"]: s for s in finding.get("values", ())}
    other = _other(finding, value)
    mine, theirs = sides.get(value, {}), sides.get(other, {})
    return {
        "finding_id": finding.get("id"),
        "metric": finding.get("metric"),
        "compared_with": other,
        "mature_posts": mine.get("posts"),
        "other_mature_posts": theirs.get("posts"),
        "median": mine.get("median_interaction_rate"),
        "other_median": theirs.get("median_interaction_rate"),
        "relative_difference": finding.get("relative_difference"),
        "weakening": bool(finding.get("weakening")),
        "since": finding.get("since"),
    }


def _index(order: Sequence, value) -> int:
    return order.index(value) if value in order else len(order)


# == prompt =====================================================================
_DIMENSION_LABELS = {"angle": "切り口", "link_mode": "リンク", "length_band": "長さ"}


def render_prompt_sections(guidance: ThreadsGenerationGuidance) -> list[str]:
    """prompt に入れる「多様性」と「学習からの弱い参考」の 2 節。

    事実の境界・文体の節とは別の節にし、学習の節は **いちばん弱い** と明記する。
    内部の ID や生の分析の数字は入れない。
    """

    lines = [
        "## 多様性 (必ず守る。下の「学習からの弱い参考」より優先する)",
        "- 依頼した切り口はすべて、それぞれ 1 本ずつ書く。どれかに寄せたり省いたりしない。",
        "- リンクの有無 (none / article) と長さは、記事と切り口に合わせて編集判断で決める。"
        "全部を同じにそろえない。",
        "- 同じ書き出し・同じ構成を繰り返さない。",
        "",
        "## 学習からの弱い参考",
        "この節は、事実の境界・文体・多様性の規則よりも **弱い**。矛盾したら、この節を無視する。",
    ]
    usable = [p for p in guidance.prompt_preferences if p.actionable]
    if not usable:
        lines.append(
            "- 十分な証拠のある学習はまだ無い。通常の多様性と編集の既定に従う。"
            "過去の数本の投稿から傾向を推測しない。"
        )
        return lines

    lines.append(
        "- 成熟した過去の投稿の比較で、弱い傾向が見えている (標本は小さく、確定ではない。"
        "相関であって原因ではない):"
    )
    for preference in usable:
        lines.append(f"  - {_render_preference(preference, guidance)}")
    neutral = [d for d, _r in guidance.neutral if d in PROMPT_DIMENSIONS]
    if neutral:
        labels = "・".join(_DIMENSION_LABELS[d] for d in neutral)
        lines.append(f"- {labels}: 十分な証拠が無い。通常どおり編集判断に従う。")
    lines.append("- 優先した値以外の案も、この回の中に少なくとも 1 本は含める (探索を残す)。")
    return lines


def _render_preference(preference: Preference, guidance: ThreadsGenerationGuidance) -> str:
    value = preference.value
    if preference.dimension == "length_band":
        band = guidance.length_bands.get(value)
        subject = f"長さ {value}" + (f" ({band[0]}〜{band[1]} 文字)" if band else "")
    elif preference.dimension == "link_mode":
        subject = f"link_mode={value}"
    else:
        subject = f"切り口 {value}"
    if preference.direction == DIRECTION_PREFER:
        text = f"{subject} を、ほんの少しだけ優先してよい。"
    else:
        text = f"{subject} は、ほんの少しだけ控えめにしてよい (禁止ではない。書いてよい)。"
    if preference.dimension == "length_band":
        text += " どの長さの投稿も引き続き有効。"
    elif preference.dimension == "link_mode":
        text += " リンクあり・なしのどちらの投稿も引き続き有効。"
    else:
        text += " 依頼した切り口はすべて書く。"
    if preference.tentative:
        text += " (差は縮まりつつある。参考程度に)"
    return text


__all__ = [
    "CONTEXT_DIMENSIONS",
    "GUIDANCE_DIMENSIONS",
    "GUIDANCE_SCHEMA",
    "IGNORED_DIMENSIONS",
    "MODE_NEUTRAL",
    "MODE_WEAK",
    "PROMPT_DIMENSIONS",
    "Preference",
    "ThreadsGenerationGuidance",
    "build_guidance",
    "render_prompt_sections",
]
