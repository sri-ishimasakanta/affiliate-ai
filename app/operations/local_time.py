"""保存時刻 (UTC) を、運用タイムゾーンの壁時計に直す (T4.1、pure)。

**保存は UTC のまま。** 変えるのは「人の暮らしの時刻で意味を持つ派生値」だけである
(公開した時刻・曜日・時間帯、公開してよい時間帯かどうか)。

- tz 付きの値はそのまま運用タイムゾーンへ変換する。
- SQLite は tzinfo を落とすので、**naive な値は UTC とみなしてから** 変換する
  (:func:`app.article.fact_freshness.ensure_aware` と同じ前提)。
- 手で +9 時間足すことはしない。DST のあるタイムゾーンでも正しく動く必要がある。

運用タイムゾーンは ``app/config/operations_policy.json`` の ``timezone`` を使う
(C8 の ``OperationsRunner._effective_date`` と C9.5 の効果測定と同じ)。
Threads のために別の設定は作らない。

経過時間 (成熟度・公開間隔) は **絶対時間** なので、ここを通さずに UTC 同士で
引き算する。タイムゾーン変換で成熟度が変わってはいけない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def to_local(moment: datetime, tz: ZoneInfo) -> datetime:
    """UTC で保存された時刻を、運用タイムゾーンの aware な時刻にする。"""

    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(tz)


def local_hour(moment: datetime, tz: ZoneInfo) -> int:
    return to_local(moment, tz).hour


def local_weekday(moment: datetime, tz: ZoneInfo) -> str:
    """曜日の略称 (Mon..Sun)。ロケールに依存しないよう固定表から引く。"""

    return _WEEKDAYS[to_local(moment, tz).weekday()]


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

__all__ = ["local_hour", "local_weekday", "to_local"]
