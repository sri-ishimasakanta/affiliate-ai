"""Threads の時間の扱い (T4.1、pure)。

ここにあるのは「**いつなら** してよいか」の判定だけで、「いつ **する** か」は
決めない。07:00 に公開窓が開くことは「07:00 から公開の資格が生まれうる」ことで
あって、「07:00 に投稿する」ことではない。

原則:

- 窓は運用タイムゾーンの壁時計で ``[start, end)``。
- 公開間隔 (soft gap) は **絶対時間** で測る (UTC 同士の差)。
- 間隔が明けた時刻が窓の外なら、次に窓が開く時刻まで繰り越す。夜中には出さない。
- 繰り越しても **まとめ出しはしない**。最後の実際の公開から間隔を測るので、
  朝に 1 本出せば次はまた間隔ぶん先になる (ダウンタイムの取り返しをしない)。
- 固定の時刻表 (09:00 / 11:00 / …) を持たない。時刻は状態から決まる。
- ランダムな遅延で「人間らしく」見せない。時刻は説明できなければならない。
- 1 日の本数の目安は **助言** にすぎない。ノルマでも上限でも健全性の条件でもない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.operations.local_time import to_local
from app.social.threads.policy import DailyWindowSpec, ThreadsOperationsPolicy


def _aware_utc(moment: datetime) -> datetime:
    return (moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)).astimezone(UTC)


# -- windows -------------------------------------------------------------------
def window_is_open(window: DailyWindowSpec, moment: datetime, tz: ZoneInfo) -> bool:
    local = to_local(moment, tz).time()
    return window.start <= local < window.end


def next_window_open_at(window: DailyWindowSpec, moment: datetime, tz: ZoneInfo) -> datetime:
    """``moment`` 以降で窓が開いている最初の時刻 (UTC)。開いていれば ``moment`` 自身。"""

    local = to_local(moment, tz)
    if window.start <= local.time() < window.end:
        return _aware_utc(moment)
    day = local.date() if local.time() < window.start else local.date() + timedelta(days=1)
    opening = datetime.combine(day, window.start, tzinfo=tz)
    return opening.astimezone(UTC)


def window_closes_at(window: DailyWindowSpec, moment: datetime, tz: ZoneInfo) -> datetime | None:
    """窓が開いているなら、今回閉じる時刻 (UTC)。閉じていれば ``None``。"""

    local = to_local(moment, tz)
    if not window.start <= local.time() < window.end:
        return None
    return datetime.combine(local.date(), window.end, tzinfo=tz).astimezone(UTC)


# -- publication timing ----------------------------------------------------------
REASON_OUTSIDE_PUBLICATION_WINDOW = "outside_publication_window"
REASON_GAP_NOT_ELAPSED = "gap_not_elapsed"


@dataclass(frozen=True)
class PublicationTiming:
    """次に「公開してよい」状態になる最初の時刻と、それまで待つ理由。"""

    now: datetime
    earliest_at: datetime
    gap_elapsed_at: datetime | None
    window_open_now: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def eligible_now(self) -> bool:
        return not self.reasons

    def as_dict(self, tz: ZoneInfo) -> dict:
        return {
            "eligible_now": self.eligible_now,
            "earliest_at": self.earliest_at.isoformat(),
            "earliest_local": to_local(self.earliest_at, tz).isoformat(),
            "gap_elapsed_at": self.gap_elapsed_at.isoformat() if self.gap_elapsed_at else None,
            "window_open_now": self.window_open_now,
            "reasons": list(self.reasons),
        }


def publication_timing(
    *,
    now: datetime,
    last_published_at: datetime | None,
    policy: ThreadsOperationsPolicy,
    tz: ZoneInfo,
) -> PublicationTiming:
    """公開の資格が生まれる最初の時刻。

    例: 最後の公開 09:17、間隔 120 分 → 11:17 が最初の資格時刻。
    worker がその前に起きても、公開の判定は 11:17 まで「待ち」を返す。

    例: 最後の公開 21:45、間隔 120 分 → 23:45 は窓の外なので、翌 07:00 に繰り越す。
    """

    now = _aware_utc(now)
    gap_elapsed_at = None
    candidate = now
    reasons: list[str] = []
    if last_published_at is not None:
        gap_elapsed_at = _aware_utc(last_published_at) + timedelta(
            minutes=policy.soft_min_gap_minutes
        )
        if gap_elapsed_at > now:
            reasons.append(REASON_GAP_NOT_ELAPSED)
            candidate = gap_elapsed_at

    window_open_now = window_is_open(policy.publication_window, now, tz)
    if not window_open_now:
        reasons.append(REASON_OUTSIDE_PUBLICATION_WINDOW)
    earliest = next_window_open_at(policy.publication_window, candidate, tz)
    return PublicationTiming(
        now=now,
        earliest_at=earliest,
        gap_elapsed_at=gap_elapsed_at,
        window_open_now=window_open_now,
        reasons=tuple(reasons),
    )


# -- daily activity (advisory) ---------------------------------------------------
ACTIVITY_BELOW_TARGET = "below_target"
ACTIVITY_WITHIN_TARGET = "within_target"
ACTIVITY_ABOVE_TARGET = "above_target"


def daily_activity(count: int, policy: ThreadsOperationsPolicy) -> dict:
    """今日の本数を目安と比べる。**助言であって、健全性の判定でも上限でもない。**

    3 本未満でも障害ではない。5 本を超えても誤りではない。編集上の理由があり、
    中身が十分に違い、間隔も自然なら 10 本前後の日があってよい。
    """

    if count < policy.daily_target_low:
        band = ACTIVITY_BELOW_TARGET
    elif count > policy.daily_target_high:
        band = ACTIVITY_ABOVE_TARGET
    else:
        band = ACTIVITY_WITHIN_TARGET
    return {
        "count": count,
        "target_low": policy.daily_target_low,
        "target_high": policy.daily_target_high,
        "band": band,
        "advisory": True,
        # 目安から外れても、ブロッカーにも警告にもならない。
        "is_blocker": False,
        "is_health_condition": False,
    }


def local_day_bounds(moment: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """運用タイムゾーンでの「今日」の [始まり, 終わり) を UTC で返す。"""

    local = to_local(moment, tz)
    start = datetime.combine(local.date(), datetime.min.time(), tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


__all__ = [
    "ACTIVITY_ABOVE_TARGET",
    "ACTIVITY_BELOW_TARGET",
    "ACTIVITY_WITHIN_TARGET",
    "REASON_GAP_NOT_ELAPSED",
    "REASON_OUTSIDE_PUBLICATION_WINDOW",
    "PublicationTiming",
    "daily_activity",
    "local_day_bounds",
    "next_window_open_at",
    "publication_timing",
    "window_closes_at",
    "window_is_open",
]
