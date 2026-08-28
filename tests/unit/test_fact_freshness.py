"""app/article/fact_freshness.py の境界検証 (tz-aware / 決定論)。"""

from datetime import UTC, datetime, timedelta, timezone

from app.article.fact_freshness import (
    FEATURE_MAX_AGE,
    PRICING_MAX_AGE,
    STATIC_MAX_AGE,
    is_fresh,
    max_age_for,
    to_storage_utc,
)
from app.article.fact_keys import FactKey

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
JST = timezone(timedelta(hours=9))
EST = timezone(timedelta(hours=-5))


def test_policy_constants() -> None:
    assert PRICING_MAX_AGE == timedelta(days=30)
    assert FEATURE_MAX_AGE == timedelta(days=90)
    assert STATIC_MAX_AGE == timedelta(days=180)


def test_max_age_groups() -> None:
    assert max_age_for(FactKey.PRICING_SUMMARY) == PRICING_MAX_AGE
    assert max_age_for(FactKey.FREE_PLAN_AVAILABLE) == PRICING_MAX_AGE
    assert max_age_for(FactKey.KEY_FEATURES) == FEATURE_MAX_AGE
    assert max_age_for(FactKey.AI_FEATURES) == FEATURE_MAX_AGE
    assert max_age_for(FactKey.OFFICIAL_URL) == STATIC_MAX_AGE
    assert max_age_for(FactKey.JAPANESE_LANGUAGE_SUPPORT) == STATIC_MAX_AGE


def test_boundary_exactly_max_age_is_fresh() -> None:
    # age == max_age -> fresh (<=)
    assert is_fresh(FactKey.PRICING_SUMMARY, NOW - timedelta(days=30), now=NOW) is True
    # age > max_age -> stale
    assert (
        is_fresh(
            FactKey.PRICING_SUMMARY,
            NOW - timedelta(days=30, seconds=1),
            now=NOW,
        )
        is False
    )


def test_feature_and_static_boundaries() -> None:
    assert is_fresh(FactKey.KEY_FEATURES, NOW - timedelta(days=90), now=NOW) is True
    assert is_fresh(FactKey.KEY_FEATURES, NOW - timedelta(days=91), now=NOW) is False
    assert is_fresh(FactKey.OFFICIAL_URL, NOW - timedelta(days=180), now=NOW) is True
    assert is_fresh(FactKey.OFFICIAL_URL, NOW - timedelta(days=181), now=NOW) is False


def test_future_checked_at_is_not_fresh() -> None:
    # age がマイナス (未来) は fresh 扱いしない (silent-fresh 防止)
    assert is_fresh(FactKey.PRICING_SUMMARY, NOW + timedelta(seconds=1), now=NOW) is False
    assert is_fresh(FactKey.PRICING_SUMMARY, NOW + timedelta(hours=9), now=NOW) is False
    assert is_fresh(FactKey.OFFICIAL_URL, NOW + timedelta(days=1), now=NOW) is False
    # ちょうど now は fresh
    assert is_fresh(FactKey.PRICING_SUMMARY, NOW, now=NOW) is True


def test_to_storage_utc_preserves_instant_across_offsets() -> None:
    # +09:00 14:12 -> UTC naive 05:12 (同一 instant)
    jst = datetime(2026, 8, 28, 14, 12, tzinfo=JST)
    stored = to_storage_utc(jst)
    assert stored == datetime(2026, 8, 28, 5, 12)
    assert stored.tzinfo is None
    assert stored.replace(tzinfo=UTC) == jst.astimezone(UTC)

    # -05:00 09:00 -> UTC naive 14:00
    est = datetime(2026, 8, 28, 9, 0, tzinfo=EST)
    assert to_storage_utc(est) == datetime(2026, 8, 28, 14, 0)

    # 既に UTC aware -> naive UTC (値そのまま)
    assert to_storage_utc(datetime(2026, 8, 28, 6, 0, tzinfo=UTC)) == datetime(2026, 8, 28, 6, 0)

    # naive 入力は UTC とみなしそのまま
    assert to_storage_utc(datetime(2026, 8, 28, 6, 0)) == datetime(2026, 8, 28, 6, 0)


def test_same_local_wallclock_different_offset_is_different_instant() -> None:
    a = to_storage_utc(datetime(2026, 8, 28, 12, 0, tzinfo=JST))
    b = to_storage_utc(datetime(2026, 8, 28, 12, 0, tzinfo=EST))
    assert a != b
    assert (b.replace(tzinfo=UTC) - a.replace(tzinfo=UTC)) == timedelta(hours=14)
