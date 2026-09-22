"""適用済み変更の before/after 窓と成熟度判定 (C9.3、pure)。

**因果を主張しない。** ここで作るのは「変更の前後で、同じ長さの窓を取って、
同じ指標を並べる」だけの器である。順位変動・季節性・他記事の更新・Google 側の
更新など、観測できない要因はいくらでもある。したがって出力は常に

    観測された差分 + それを信じてよいか (成熟度) + 因果を主張しない旨

の 3 点セットで返す。

成熟度は 4 つの理由で ``insufficient_data`` になりうる:

``POST_WINDOW_NOT_ELAPSED``
    変更後の窓がまだ経過していない (未来の日付は数えない)。
``POST_WINDOW_NOT_COVERED``
    窓は経過したが、取り込みがそこまで届いていない。**活動 (最終データ日)
    ではなく coverage** で判断する (C8.5 と同じ区別)。
``NO_PRE_DATA``
    変更前の窓に観測が無い。差を取る相手がいない。
``BELOW_MINIMUM_VOLUME``
    露出が少なすぎて、差がノイズと区別できない。

窓は変更日そのものを **どちらにも含めない**。適用当日は変更前後が混ざるため。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

#: 成熟度の理由コード。
POST_WINDOW_NOT_ELAPSED = "POST_WINDOW_NOT_ELAPSED"
POST_WINDOW_NOT_COVERED = "POST_WINDOW_NOT_COVERED"
NO_PRE_DATA = "NO_PRE_DATA"
BELOW_MINIMUM_VOLUME = "BELOW_MINIMUM_VOLUME"

#: 成熟度の判定結果。
EFFECT_INSUFFICIENT_DATA = "insufficient_data"
EFFECT_OBSERVED = "observed"

#: 因果の主張は常にこれ。文字列として出力に残し、読み手の誤解を防ぐ。
CAUSAL_CLAIM_NONE = "none"

DEFAULT_WINDOW_DAYS = 28
#: この露出数に満たない窓では差を「観測された」と呼ばない。
DEFAULT_MINIMUM_IMPRESSIONS = 30


@dataclass(frozen=True)
class EffectWindow:
    """比較に使う 1 つの窓。"""

    start: date
    end: date
    #: 取り込みがこの窓を完全に覆えているか。
    covered: bool
    covered_through: date | None

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
            "covered": self.covered,
            "covered_through": self.covered_through.isoformat() if self.covered_through else None,
        }


@dataclass
class MaturityVerdict:
    """この比較を読んでよいか。"""

    status: str
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def sufficient(self) -> bool:
        return self.status == EFFECT_OBSERVED

    def as_dict(self) -> dict:
        return {"status": self.status, "reasons": list(self.reasons), "notes": list(self.notes)}


def build_windows(
    *,
    change_date: date,
    today: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
    coverage_through: date | None,
) -> tuple[EffectWindow, EffectWindow]:
    """変更日を挟んだ同じ長さの 2 窓を作る (変更当日はどちらにも入れない)。"""

    window_days = max(1, int(window_days))
    pre_end = change_date - timedelta(days=1)
    pre_start = pre_end - timedelta(days=window_days - 1)
    post_start = change_date + timedelta(days=1)
    post_end = post_start + timedelta(days=window_days - 1)

    def _covered(end: date) -> bool:
        return coverage_through is not None and coverage_through >= end and today >= end

    return (
        EffectWindow(pre_start, pre_end, _covered(pre_end), coverage_through),
        EffectWindow(post_start, post_end, _covered(post_end), coverage_through),
    )


def assess_maturity(
    *,
    pre: EffectWindow,
    post: EffectWindow,
    today: date,
    pre_impressions: int,
    post_impressions: int,
    pre_rows: int,
    minimum_impressions: int = DEFAULT_MINIMUM_IMPRESSIONS,
) -> MaturityVerdict:
    """差を読んでよいかを判定する。迷ったら ``insufficient_data`` に倒す。"""

    reasons: list[str] = []
    notes: list[str] = []

    if today < post.end:
        reasons.append(POST_WINDOW_NOT_ELAPSED)
        notes.append(
            f"the post-change window ends {post.end.isoformat()}; today is {today.isoformat()}"
        )
    elif not post.covered:
        reasons.append(POST_WINDOW_NOT_COVERED)
        through = post.covered_through.isoformat() if post.covered_through else "unknown"
        notes.append(
            f"imports have not yet covered the post-change window (coverage through {through})"
        )

    if pre_rows == 0:
        reasons.append(NO_PRE_DATA)
        notes.append("there is no measurement in the pre-change window to compare against")
    if not pre.covered:
        notes.append("the pre-change window is not fully covered by imports")

    if not reasons and max(pre_impressions, post_impressions) < minimum_impressions:
        reasons.append(BELOW_MINIMUM_VOLUME)
        notes.append(
            f"peak window impressions {max(pre_impressions, post_impressions)} "
            f"is below the minimum {minimum_impressions}"
        )

    status = EFFECT_OBSERVED if not reasons else EFFECT_INSUFFICIENT_DATA
    return MaturityVerdict(status=status, reasons=reasons, notes=notes)


def delta(before: float | int | None, after: float | int | None) -> float | None:
    """観測が揃っているときだけ差を出す (片側欠損で 0 を作らない)。"""

    if before is None or after is None:
        return None
    return round(float(after) - float(before), 4)
