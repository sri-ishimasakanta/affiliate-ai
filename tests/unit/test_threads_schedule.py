"""Threads の時間の扱い (T4.1、pure)。

pin する契約:

- 生の時刻は UTC。派生値 (時刻・曜日・窓) は **運用タイムゾーン** (Asia/Tokyo)。
- naive な値は UTC とみなしてから変換する。
- 成熟度・公開間隔は絶対時間。タイムゾーン変換で変わらない。
- 公開窓 07:00–23:00、承認通知窓 08:00–21:00 (``[start, end)``)。
- 窓が開くことは「その時刻に出す」ことではない。資格が生まれうるだけ。
- 間隔が明けた時刻が夜なら翌朝へ繰り越す。夜中には出さない。
- 1 日の本数の目安は助言。上限でもノルマでも健全性の条件でもない。
- Threads 用に別のタイムゾーン設定を作らない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.operations.local_time import local_hour, local_weekday, to_local
from app.operations.policy import get_policy as get_c8_policy
from app.social.threads.measurement import classify_maturity
from app.social.threads.policy import (
    get_measurement_policy,
    get_operations_policy,
    load_operations_policy,
)
from app.social.threads.schedule import (
    ACTIVITY_ABOVE_TARGET,
    ACTIVITY_BELOW_TARGET,
    ACTIVITY_WITHIN_TARGET,
    REASON_GAP_NOT_ELAPSED,
    REASON_OUTSIDE_PUBLICATION_WINDOW,
    daily_activity,
    local_day_bounds,
    next_window_open_at,
    publication_timing,
    window_is_open,
)

JST = ZoneInfo("Asia/Tokyo")
POLICY = get_operations_policy()


def _jst(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=JST)


# == timezone =================================================================
def test_the_exact_boundary_case_rolls_the_date_and_weekday() -> None:
    """2026-09-23 18:24 UTC は 2026-09-24 03:24 JST。曜日も水→木に変わる。"""

    moment = datetime(2026, 9, 23, 18, 24, tzinfo=UTC)
    local = to_local(moment, JST)

    assert local.isoformat() == "2026-09-24T03:24:00+09:00"
    assert local_hour(moment, JST) == 3
    assert local_weekday(moment, JST) == "Thu"
    # UTC のままなら水曜 18 時になってしまう (T4 の不具合)。
    assert moment.strftime("%a") == "Wed"


def test_naive_timestamps_are_treated_as_utc_first() -> None:
    """SQLite から読んだ naive な値は UTC。JST だと思い込んで +0 にしない。"""

    naive = datetime(2026, 9, 23, 18, 24)
    assert to_local(naive, JST) == to_local(naive.replace(tzinfo=UTC), JST)
    assert local_hour(naive, JST) == 3


def test_local_hour_uses_the_operations_timezone() -> None:
    assert local_hour(datetime(2026, 9, 24, 0, 30, tzinfo=UTC), JST) == 9
    assert local_hour(datetime(2026, 9, 24, 14, 59, tzinfo=UTC), JST) == 23


def test_maturity_is_absolute_elapsed_time_and_ignores_timezone() -> None:
    measurement = get_measurement_policy()
    published = datetime(2026, 9, 23, 18, 24, tzinfo=UTC)
    now = datetime(2026, 9, 24, 18, 24, tzinfo=UTC)
    age_utc = (now - published).total_seconds() / 3600
    age_local = (to_local(now, JST) - to_local(published, JST)).total_seconds() / 3600

    assert age_utc == age_local == 24.0
    assert classify_maturity(age_utc, measurement) == classify_maturity(age_local, measurement)


def test_threads_reuses_the_operations_timezone_setting() -> None:
    """運用タイムゾーンは operations_policy.json の 1 か所だけ。"""

    assert get_c8_policy().timezone.key == "Asia/Tokyo"
    assert "timezone" not in POLICY.raw


def test_a_threads_specific_timezone_is_refused(tmp_path) -> None:
    document = dict(POLICY.raw)
    document["timezone"] = "UTC"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="timezone"):
        load_operations_policy(path)


def test_local_day_bounds_follow_the_jst_calendar() -> None:
    start, end = local_day_bounds(datetime(2026, 9, 23, 18, 24, tzinfo=UTC), JST)
    assert start == datetime(2026, 9, 23, 15, 0, tzinfo=UTC)  # 09-24 00:00 JST
    assert end == datetime(2026, 9, 24, 15, 0, tzinfo=UTC)


# == windows ==================================================================
@pytest.mark.parametrize(
    ("hour", "minute", "open_"),
    [(6, 59, False), (7, 0, True), (22, 59, True), (23, 0, False), (3, 0, False)],
)
def test_publication_window(hour: int, minute: int, open_: bool) -> None:
    assert window_is_open(POLICY.publication_window, _jst(2026, 9, 24, hour, minute), JST) is open_


@pytest.mark.parametrize(
    ("hour", "minute", "open_"),
    [(7, 59, False), (8, 0, True), (20, 59, True), (21, 0, False)],
)
def test_approval_notification_window(hour: int, minute: int, open_: bool) -> None:
    window = POLICY.approval_notification_window
    assert window_is_open(window, _jst(2026, 9, 24, hour, minute), JST) is open_


def test_next_window_open_rolls_to_the_next_morning() -> None:
    opening = next_window_open_at(POLICY.publication_window, _jst(2026, 9, 24, 23, 45), JST)
    assert to_local(opening, JST) == _jst(2026, 9, 25, 7, 0)


def test_next_window_open_before_dawn_is_the_same_day() -> None:
    opening = next_window_open_at(POLICY.publication_window, _jst(2026, 9, 24, 3, 24), JST)
    assert to_local(opening, JST) == _jst(2026, 9, 24, 7, 0)


def test_overnight_windows_are_refused(tmp_path) -> None:
    document = dict(POLICY.raw)
    document["publication_window"] = {"start": "23:00", "end": "07:00"}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="overnight"):
        load_operations_policy(path)


# == publication timing =======================================================
def test_gap_is_measured_from_the_real_publication_not_a_slot() -> None:
    """09:17 に出したら、次の資格は 11:17。11:15 や 11:20 の枠には丸めない。"""

    timing = publication_timing(
        now=_jst(2026, 9, 24, 9, 40),
        last_published_at=_jst(2026, 9, 24, 9, 17),
        policy=POLICY,
        tz=JST,
    )
    assert timing.reasons == (REASON_GAP_NOT_ELAPSED,)
    assert to_local(timing.earliest_at, JST) == _jst(2026, 9, 24, 11, 17)


def test_a_late_gap_rolls_to_the_next_morning_instead_of_posting_at_night() -> None:
    """21:45 + 120 分 = 23:45 は窓の外。翌 07:00 に繰り越す。"""

    timing = publication_timing(
        now=_jst(2026, 9, 24, 22, 0),
        last_published_at=_jst(2026, 9, 24, 21, 45),
        policy=POLICY,
        tz=JST,
    )
    assert to_local(timing.earliest_at, JST) == _jst(2026, 9, 25, 7, 0)
    assert not timing.eligible_now


def test_night_blocks_publication_even_with_no_prior_post() -> None:
    timing = publication_timing(
        now=_jst(2026, 9, 24, 2, 0), last_published_at=None, policy=POLICY, tz=JST
    )
    assert REASON_OUTSIDE_PUBLICATION_WINDOW in timing.reasons
    assert to_local(timing.earliest_at, JST) == _jst(2026, 9, 24, 7, 0)


def test_the_morning_has_no_burst_catch_up() -> None:
    """夜の間に資格が溜まっても、07:00 の 1 本のあとは再び間隔ぶん待つ。"""

    first = publication_timing(
        now=_jst(2026, 9, 25, 7, 0),
        last_published_at=_jst(2026, 9, 24, 21, 45),
        policy=POLICY,
        tz=JST,
    )
    assert first.eligible_now

    after_one = publication_timing(
        now=_jst(2026, 9, 25, 7, 1),
        last_published_at=_jst(2026, 9, 25, 7, 0),
        policy=POLICY,
        tz=JST,
    )
    assert not after_one.eligible_now
    assert to_local(after_one.earliest_at, JST) == _jst(2026, 9, 25, 9, 0)


def test_07_00_means_eligible_from_not_publish_at() -> None:
    """07:00 は資格の始まり。07:00 に投稿する予定という意味は持たない。"""

    timing = publication_timing(
        now=_jst(2026, 9, 24, 10, 33), last_published_at=None, policy=POLICY, tz=JST
    )
    assert timing.eligible_now
    assert timing.earliest_at == _jst(2026, 9, 24, 10, 33).astimezone(UTC)


def test_timing_has_no_randomness() -> None:
    kwargs = {
        "now": _jst(2026, 9, 24, 9, 40),
        "last_published_at": _jst(2026, 9, 24, 9, 17),
        "policy": POLICY,
        "tz": JST,
    }
    assert publication_timing(**kwargs) == publication_timing(**kwargs)


# == daily activity ===========================================================
def test_below_the_target_is_not_an_operational_failure() -> None:
    activity = daily_activity(1, POLICY)
    assert activity["band"] == ACTIVITY_BELOW_TARGET
    assert activity["is_health_condition"] is False
    assert activity["is_blocker"] is False


def test_above_the_target_is_not_an_operational_failure() -> None:
    activity = daily_activity(6, POLICY)
    assert activity["band"] == ACTIVITY_ABOVE_TARGET
    assert activity["is_health_condition"] is False
    assert activity["is_blocker"] is False


def test_around_ten_posts_is_not_automatically_a_failure() -> None:
    activity = daily_activity(10, POLICY)
    assert activity["is_blocker"] is False
    assert activity["is_health_condition"] is False


def test_within_the_target_is_just_a_band() -> None:
    assert daily_activity(4, POLICY)["band"] == ACTIVITY_WITHIN_TARGET


def test_no_hard_daily_cap_exists() -> None:
    """1 日の本数は、どの時刻判定にも入っていない。"""

    # 今日すでに 9 本出していても、時刻の資格は間隔と窓だけで決まる。
    timing = publication_timing(
        now=_jst(2026, 9, 24, 20, 0),
        last_published_at=_jst(2026, 9, 24, 17, 0),
        policy=POLICY,
        tz=JST,
    )
    assert timing.eligible_now
    assert "daily_cap" not in json.dumps(POLICY.raw)
    assert "max_per_day" not in json.dumps(POLICY.raw)


def test_the_target_must_be_declared_advisory(tmp_path) -> None:
    document = dict(POLICY.raw)
    document["daily_activity_target"] = {"low": 3, "high": 5, "advisory": False}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="advisory"):
        load_operations_policy(path)


def test_policy_values_match_the_approved_v1_semantics() -> None:
    assert POLICY.publication_window.start.isoformat("minutes") == "07:00"
    assert POLICY.publication_window.end.isoformat("minutes") == "23:00"
    assert POLICY.approval_notification_window.start.isoformat("minutes") == "08:00"
    assert POLICY.approval_notification_window.end.isoformat("minutes") == "21:00"
    assert POLICY.soft_min_gap_minutes == 120
    assert (POLICY.daily_target_low, POLICY.daily_target_high) == (3, 5)
    assert POLICY.heartbeat_max_seconds <= 300
    assert timedelta(seconds=POLICY.heartbeat_max_seconds) <= timedelta(minutes=5)
