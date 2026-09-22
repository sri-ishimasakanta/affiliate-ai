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

暦日は **運用/レポートのタイムゾーン** (``app/config/operations_policy.json`` の
``timezone``、本番では ``Asia/Tokyo``) で決める。UTC の暦日ではない。保存されている
適用時刻は UTC のままで、ここでは読むときに変換するだけである
(C8 の ``OperationsRunner._effective_date`` と同じ規約)。

    2026-09-22T17:43:41Z  ->  2026-09-23 (Asia/Tokyo) が変更日

provider の日付の意味は揃っていないので、揃っているふりをしない:

- GA4   -- property のタイムゾーンの暦日 (本番では Asia/Tokyo。窓と一致する)
- GSC   -- **Pacific Time** の暦日 (:mod:`app.search_console.date_window`)
- 窓     -- レポートタイムゾーンの暦日

GSC は窓の境界に対して最大 1 日ずれうる。行を書き換えて合わせることはしない
(provider-faithful を壊すため)。境界日付近の差は、この 1 日のずれの範囲内では
読み取らない -- 成熟度判定が既に「窓が経過し取り込みが届くまで読まない」と
しているので、実務上はそこで吸収される。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

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


@dataclass(frozen=True)
class EffectWindows:
    """変更日と、その前後の同じ長さの 2 窓。"""

    #: レポートタイムゾーンでの変更日 (どちらの窓にも含まれない)。
    effective_date: date
    timezone_name: str
    pre: EffectWindow
    post: EffectWindow


def local_effective_date(moment: datetime, tz: ZoneInfo) -> date:
    """UTC で保存された時刻を、レポートタイムゾーンの暦日に直す。

    SQLite は tzinfo を落とすので、naive な値は UTC とみなす
    (:func:`app.article.fact_freshness.ensure_aware` と同じ前提)。手で +9 時間
    足すようなことはしない -- DST のある tz でも正しく動く必要がある。
    """

    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(tz).date()


def build_windows(
    *,
    change_at: datetime,
    reporting_timezone: ZoneInfo,
    today: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
    coverage_through: date | None,
) -> EffectWindows:
    """変更日を挟んだ同じ長さの 2 窓を作る (変更当日はどちらにも入れない)。

    暦日は ``reporting_timezone`` で決める。UTC の暦日で切ると、深夜の適用が
    前日側に落ちて、変更当日を post 窓に含めてしまう。
    """

    window_days = max(1, int(window_days))
    change_date = local_effective_date(change_at, reporting_timezone)
    pre_end = change_date - timedelta(days=1)
    pre_start = pre_end - timedelta(days=window_days - 1)
    post_start = change_date + timedelta(days=1)
    post_end = post_start + timedelta(days=window_days - 1)

    def _covered(end: date) -> bool:
        return coverage_through is not None and coverage_through >= end and today >= end

    return EffectWindows(
        effective_date=change_date,
        timezone_name=str(getattr(reporting_timezone, "key", reporting_timezone)),
        pre=EffectWindow(pre_start, pre_end, _covered(pre_end), coverage_through),
        post=EffectWindow(post_start, post_end, _covered(post_end), coverage_through),
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
