"""Growth Action の効果の観測 (C9 Batch 3、``growth-action-outcome/1``、pure)。

「いつ何を変えたか」(``MeasurementAnchor``) を固定し、実際に変わった時刻 (``effective_at``) から
同じ長さの窓で、変わる前と後に何を観測したかを並べる。**因果を言わない・1 つの成功の点数を作らない・
勝ち負けを判定しない。**

- ``effective_at`` は実際の変化の時刻だけ: 変更の依頼なら WordPress への適用が成功した時刻
  (``change_applications.finished_at``)、記事なら公開の時刻、Threads なら公開の時刻。承認・変換の
  時刻は使わない。まだ変わっていなければ ``None`` (観測は ``waiting``)。
- 窓は 24h / 72h / 7d / 14d / 28d。SEO と GA4 は日ごとの取り込みなので 1 / 3 / 7 / 14 / 28 日。
  取り込みの遅れ (GSC は 2〜3 日) で窓の後ろがまだ届いていなければ ``waiting``。
- 欠測は ``None`` のまま。アフィリエイトのクリックは信頼できる計測開始より後だけ。
- Threads は T6.5 のチェックポイント (24h / 72h) をそのまま使う (7d 以降は Threads に無い)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime

OUTCOME_SCHEMA = "growth-action-outcome/1"

WAITING = "waiting"  # まだ変わっていない、または窓・取り込みがまだ届いていない
INSUFFICIENT = "insufficient"  # 届いたが、比べる量・前の窓の観測が足りない
OBSERVABLE = "observable"  # 値は観測できる (比べてよいかの判定は出所の規則が無い)
STALE_DATA = "stale_data"  # 出所の取り込みが古い
COMPLETED_WINDOW = "completed_window"  # 窓が終わり、取り込みが届き、量も門を越えた
NOT_APPLICABLE = "not_applicable"  # この出所にはこの窓が無い
OUTCOME_STATES = (WAITING, INSUFFICIENT, OBSERVABLE, STALE_DATA, COMPLETED_WINDOW,
                  NOT_APPLICABLE)  # fmt: skip

#: (名前, 日) — SEO / GA4 / アフィリエイトの窓。
CHECKPOINTS: tuple[tuple[str, int], ...] = (("24h", 1), ("72h", 3), ("7d", 7), ("14d", 14),
                                            ("28d", 28))  # fmt: skip
#: Threads の窓 (T6.5 の CHECKPOINT_HOURS のうち、ここで使うもの)。
THREADS_CHECKPOINTS = ("24h", "72h")

#: ChangeEffect の成熟の理由 → この観測の状態。
_EFFECT_REASON_STATE = {
    "POST_WINDOW_NOT_ELAPSED": WAITING,
    "POST_WINDOW_NOT_COVERED": WAITING,
    "NO_PRE_DATA": INSUFFICIENT,
    "BELOW_MINIMUM_VOLUME": INSUFFICIENT,
}
#: 観測の言葉 (因果・評価の言葉を使わない)。
_FORBIDDEN = ("により", "のおかげ", "効果があった", "改善した", "悪化した", "成功", "失敗")


@dataclass(frozen=True)
class MeasurementAnchor:
    growth_action_id: int
    action_type: str
    subject_id: str
    article_id: int | None
    downstream_type: str
    downstream_id: int
    downstream_state: str
    #: 実際の変化の時刻 (まだなら ``None``)。
    effective_at: str | None
    effective_source: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Checkpoint:
    name: str
    days: int | None
    state: str
    reasons: tuple[str, ...] = ()
    before: Mapping = field(default_factory=dict)
    after: Mapping = field(default_factory=dict)
    differences: Mapping = field(default_factory=dict)
    observations: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {**asdict(self), "reasons": list(self.reasons),
                "observations": list(self.observations)}  # fmt: skip


def effective_at_for(downstream_type: str, *, applications: Sequence[Mapping] = (),
                     published_at: datetime | None = None
                     ) -> tuple[str | None, str | None]:  # fmt: skip
    """実際の変化の時刻。返り値: (時刻, どこから)。承認・変換の時刻は使わない。"""

    if downstream_type == "change_request":
        applied = [a for a in applications if a.get("outcome") in ("succeeded", "reconciled")
                   and (a.get("finished_at") or a.get("attempted_at"))]  # fmt: skip
        if not applied:
            return None, None
        first = min(applied, key=lambda a: str(a.get("finished_at") or a.get("attempted_at")))
        return (str(first.get("finished_at") or first.get("attempted_at")),
                "change_applications.finished_at")
    if downstream_type in ("article", "threads_publication"):
        if published_at is None:
            return None, None
        return published_at.isoformat(), f"{downstream_type}.published_at"
    return None, None


def checkpoint_from_effect(name: str, days: int, effect: Mapping | None, *,
                           source_stale: bool = False) -> Checkpoint:  # fmt: skip
    """``ChangeEffectService`` の 1 つの窓の結果 → 観測の状態と、比べる前後の値。"""

    if effect is None:
        return Checkpoint(name, days, WAITING, ("not applied yet",))
    maturity = effect.get("maturity") or {}
    reasons = tuple(maturity.get("reasons") or ())
    if source_stale:
        state = STALE_DATA
    elif maturity.get("status") == "observed":
        state = COMPLETED_WINDOW
    else:
        states = {_EFFECT_REASON_STATE.get(r, INSUFFICIENT) for r in reasons} or {INSUFFICIENT}
        state = WAITING if WAITING in states else INSUFFICIENT
    before, after = dict(effect.get("pre") or {}), dict(effect.get("post") or {})
    differences = dict(effect.get("deltas") or {})
    return Checkpoint(name, days, state, reasons, before, after, differences,
                      observations=_observations(name, before, after, differences, state))


def threads_checkpoint(name: str, post: Mapping | None) -> Checkpoint:
    """T6.5 の 1 本の投稿のチェックポイント → 観測 (cohort との比べはしない)。

    達したかは、T6.5 の投稿の行の ``age_at_as_of_hours`` (観測の時点の経過時間) で決める
    (行に ``reached`` があればそれを使う)。未来の観測は T6.5 の側で除かれている。
    """

    if name not in THREADS_CHECKPOINTS:
        return Checkpoint(name, None, NOT_APPLICABLE, ("Threads checkpoints are 24h and 72h",))
    if post is None:
        return Checkpoint(name, None, WAITING, ("not published yet",))
    cp = (post.get("checkpoints") or {}).get(name) or {}
    reached = cp.get("reached")
    if reached is None:
        age = post.get("age_at_as_of_hours")
        reached = age is not None and age >= int(name.rstrip("h"))
    if not reached:
        return Checkpoint(name, None, WAITING, ("checkpoint not reached",))
    if not cp.get("comparable"):
        return Checkpoint(name, None, INSUFFICIENT, (str(cp.get("reason") or "no matched "
                                                         "observation"),))  # fmt: skip
    after = dict(cp.get("metrics") or {})
    return Checkpoint(name, None, OBSERVABLE, (), {}, after, {},
                      (f"公開から {name} の時点で views={after.get('views')} を観測",))


def _observations(name, before, after, differences, state) -> tuple[str, ...]:
    if state == WAITING:
        return ()
    out = []
    for metric in ("impressions", "clicks", "ga4_sessions", "ga4_organic_sessions",
                   "affiliate_clicks"):
        if after.get(metric) is not None:
            out.append(f"変更後の {name} の窓では {metric}={after[metric]} を観測")
        if differences.get(metric) is not None:
            out.append(f"変更前の {name} の窓との差は {metric} {differences[metric]:+g}")
    if after.get("affiliate_clicks_trusted") is False:
        out.append("アフィリエイトのクリックは信頼できる計測開始より前の期間を含むため数えない")
    return tuple(neutral(line) for line in out)


def neutral(line: str) -> str:
    """観測の文に、因果・評価の言葉が入っていないことを確かめる (入っていれば例外)。"""

    for word in _FORBIDDEN:
        if word in line:
            raise ValueError(f"observation wording must stay descriptive: {line!r}")
    return line


def measurement_state(checkpoints: Sequence[Checkpoint]) -> str:
    """全体の状態 (一番進んだ窓で決める。成功・失敗の判定ではない)。"""

    states = [c.state for c in checkpoints if c.state != NOT_APPLICABLE]
    if not states:
        return WAITING
    for state in (STALE_DATA, COMPLETED_WINDOW, OBSERVABLE, INSUFFICIENT):
        if state in states:
            return state
    return WAITING


@dataclass(frozen=True)
class GrowthActionOutcome:
    anchor: MeasurementAnchor
    checkpoints: tuple[Checkpoint, ...]
    source_freshness: Mapping = field(default_factory=dict)
    data_quality: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def measurement_state(self) -> str:
        return measurement_state(self.checkpoints)

    def as_dict(self) -> dict:
        return {"schema": OUTCOME_SCHEMA, "anchor": self.anchor.as_dict(),
                "measurement_state": self.measurement_state,
                "checkpoints": [c.as_dict() for c in self.checkpoints],
                "source_freshness": dict(self.source_freshness),
                "data_quality": list(self.data_quality), "notes": list(self.notes),
                "causal_claim": "none", "score": None}  # fmt: skip


NOTES = (
    "descriptive only: what was observed before and after the change; no cause is claimed",
    "no single success score and no winner/loser judgement",
    "effective_at is the real change time (application or publication), never the approval",
)


__all__ = ["CHECKPOINTS", "COMPLETED_WINDOW", "Checkpoint", "GrowthActionOutcome",
           "INSUFFICIENT", "MeasurementAnchor", "NOTES", "NOT_APPLICABLE", "OBSERVABLE",
           "OUTCOME_SCHEMA", "OUTCOME_STATES", "STALE_DATA", "THREADS_CHECKPOINTS", "WAITING",
           "checkpoint_from_effect", "effective_at_for", "measurement_state", "neutral",
           "threads_checkpoint"]
