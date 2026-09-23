"""Threads の成熟度と観測の読み方 (T4、pure)。

**公開直後の 0 を「成績が悪い」と読まない。** これがこのモジュールの存在理由で
ある。0 が意味を持つのは、配信が一巡したあとだけである。

境界はコードに埋めず ``app/config/threads_measurement_policy.json`` に置く
(C6/C7/T2 と同じ方針)。閾値を議論・改訂しても、過去の判断は
``policy_version`` で再現できる。

**不透明な総合スコアは作らない。** 名前の付いた観察を個別に立てるだけにする。
比率を出すのは、分母の意味がはっきりしていて、母数が足りているときだけ。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# -- maturity ------------------------------------------------------------------
MATURITY_JUST_PUBLISHED = "just_published"
MATURITY_EARLY_OBSERVATION = "early_observation"
MATURITY_INITIAL_SAMPLE = "initial_sample"
MATURITY_MATURE = "mature_enough_for_comparison"
MATURITIES = (
    MATURITY_JUST_PUBLISHED,
    MATURITY_EARLY_OBSERVATION,
    MATURITY_INITIAL_SAMPLE,
    MATURITY_MATURE,
)

# -- observations (事実のみ。良し悪しを言わない) --------------------------------
OBS_VIEWS_OBSERVED = "views_observed"
OBS_INTERACTION_OBSERVED = "interaction_observed"
OBS_NO_INTERACTION_YET = "no_interaction_yet"
OBS_REPLY_ACTIVITY = "reply_activity"
OBS_REPOST_ACTIVITY = "repost_activity"
OBS_SHARE_ACTIVITY = "share_activity"
OBS_INSUFFICIENT_AGE = "insufficient_age"
OBS_INSUFFICIENT_SAMPLE = "insufficient_sample"
OBS_NOT_OBSERVED_YET = "not_observed_yet"

#: 相互作用として数える指標 (公式に存在するものだけ)。
INTERACTION_METRICS = ("likes", "replies", "reposts", "quotes", "shares")


@dataclass
class MaturityVerdict:
    stage: str
    age_hours: float | None
    comparable: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "age_hours": self.age_hours,
            "comparable": self.comparable,
            "notes": list(self.notes),
        }


def classify_maturity(age_hours: float | None, policy) -> MaturityVerdict:
    """公開からの経過時間だけで段階を決める (指標の値は見ない)。"""

    if age_hours is None:
        return MaturityVerdict(
            stage=MATURITY_JUST_PUBLISHED,
            age_hours=None,
            comparable=False,
            notes=["the publication time is unknown; treat it as brand new"],
        )
    bounds = policy.maturity_hours
    if age_hours < bounds["just_published"]:
        stage = MATURITY_JUST_PUBLISHED
    elif age_hours < bounds["early_observation"]:
        stage = MATURITY_EARLY_OBSERVATION
    elif age_hours < bounds["initial_sample"]:
        stage = MATURITY_INITIAL_SAMPLE
    else:
        stage = MATURITY_MATURE
    return MaturityVerdict(
        stage=stage,
        age_hours=round(age_hours, 2),
        comparable=stage == MATURITY_MATURE,
        notes=[policy.maturity_note(stage)],
    )


def observe(snapshot: dict, maturity: MaturityVerdict, policy) -> list[str]:
    """1 件の観測から、事実としての観察だけを取り出す。

    ``snapshot`` の値は「観測できなかった = None」「本当に 0 = 0」を区別している
    前提で読む。ここで欠測を 0 に丸めない。
    """

    observations: list[str] = []
    views = snapshot.get("views")
    interactions = {m: snapshot.get(m) for m in INTERACTION_METRICS}
    observed = [v for v in interactions.values() if v is not None]

    if views is None and not observed:
        observations.append(OBS_NOT_OBSERVED_YET)
    if views is not None and views > 0:
        observations.append(OBS_VIEWS_OBSERVED)

    total = sum(v for v in observed if v)
    if observed:
        observations.append(OBS_INTERACTION_OBSERVED if total > 0 else OBS_NO_INTERACTION_YET)
    if (interactions.get("replies") or 0) > 0:
        observations.append(OBS_REPLY_ACTIVITY)
    if (interactions.get("reposts") or 0) > 0:
        observations.append(OBS_REPOST_ACTIVITY)
    if (interactions.get("shares") or 0) > 0:
        observations.append(OBS_SHARE_ACTIVITY)

    if not maturity.comparable:
        # 若い投稿の 0 は「成績」ではない。
        observations.append(OBS_INSUFFICIENT_AGE)
    if views is not None and views < policy.minimum_views_for_ratio:
        observations.append(OBS_INSUFFICIENT_SAMPLE)
    return observations


def interaction_total(snapshot: dict) -> int | None:
    """観測できた相互作用の合計。1 つも観測できていなければ ``None``。"""

    values = [snapshot.get(m) for m in INTERACTION_METRICS]
    observed = [v for v in values if v is not None]
    return sum(observed) if observed else None


def interactions_per_view(snapshot: dict, policy, maturity: MaturityVerdict) -> float | None:
    """**観測された相互作用 ÷ 観測された views**。

    名前のとおりの意味しか持たない。「エンゲージメント率」のような一般名を付け
    ないのは、Threads の views の定義が公式に "in development" であり、他所の
    指標と同じものだと言えないためである。

    ``None`` を返す条件 (どれも **0.0 を返さない**):

    - 分母が不明・0・母数不足
    - 投稿がまだ若い

    最後の条件のために ``maturity`` を必須引数にしてある。公開 28 分後に
    ``0 / 83`` を 0.0 として出すと、配信が一巡してもいないのに「反応ゼロ」という
    成績に読まれる。呼び出し側が渡し忘れられないよう、構造で防ぐ。
    """

    if not maturity.comparable:
        return None
    views = snapshot.get("views")
    total = interaction_total(snapshot)
    if views is None or total is None:
        return None
    if views < policy.minimum_views_for_ratio or views <= 0:
        return None
    return round(total / views, 4)


def length_bucket(character_count: int, policy) -> str:
    for bucket in policy.length_buckets:
        if character_count <= int(bucket["max_characters"]):
            return str(bucket["name"])
    return "long"
