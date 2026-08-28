"""ArticleFact の freshness policy (pure)。

V1 定数 (Phase 3B-1 §17):
- 料金系: 30 日
- 機能系: 90 日
- 静的 : 180 日

境界は ``age <= max_age → fresh`` / ``age > max_age → stale`` (tz-aware)。
将来 Settings 化する可能性がある (今回は module 定数)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.article.fact_keys import FactKey


def ensure_aware(value: datetime) -> datetime:
    """SQLite は tz を落とすため、naive datetime は UTC とみなす。

    これが正しい前提として、書き込み側は必ず :func:`to_storage_utc` で UTC へ
    正規化してから永続化すること。
    """

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def to_storage_utc(value: datetime) -> datetime:
    """DB 保存用に datetime を **UTC の naive wall-clock** へ正規化する。

    ``DateTime(timezone=True)`` でも SQLite は tzinfo を落とし wall-clock だけを
    保存するため、書き込み前に aware datetime を UTC へ変換しておく
    (`+09:00` の 14:12 → naive 05:12)。これにより SQLite でも PostgreSQL でも
    「保存値 = UTC instant」で意味が統一される。naive 入力は既に UTC とみなす。
    """

    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(tzinfo=None)

PRICING_MAX_AGE = timedelta(days=30)
FEATURE_MAX_AGE = timedelta(days=90)
STATIC_MAX_AGE = timedelta(days=180)

_ZERO = timedelta(0)

_PRICING_KEYS = frozenset(
    {
        FactKey.PRICING_SUMMARY,
        FactKey.FREE_PLAN_AVAILABLE,
        FactKey.FREE_TRIAL_AVAILABLE,
        FactKey.BUSINESS_PLAN_AVAILABLE,
    }
)
_STATIC_KEYS = frozenset(
    {
        FactKey.OFFICIAL_PRODUCT_NAME,
        FactKey.OFFICIAL_URL,
        FactKey.CATEGORY,
        FactKey.TARGET_USERS,
        FactKey.JAPANESE_LANGUAGE_SUPPORT,
        FactKey.JAPAN_BUSINESS_SUPPORT,
    }
)
# 残りは機能系 (FEATURE_MAX_AGE)。


def max_age_for(fact_key: FactKey) -> timedelta:
    if fact_key in _PRICING_KEYS:
        return PRICING_MAX_AGE
    if fact_key in _STATIC_KEYS:
        return STATIC_MAX_AGE
    return FEATURE_MAX_AGE


def is_fresh(fact_key: FactKey, checked_at: datetime, *, now: datetime) -> bool:
    """``0 <= now - checked_at <= max_age_for(fact_key)`` なら fresh。

    **未来の checked_at (age がマイナス) は fresh として扱わない** — 黙って通すと
    timezone バグ等を検知できないため。呼び出し側 (readiness) が stale として扱う。
    """

    age = ensure_aware(now) - ensure_aware(checked_at)
    if age < _ZERO:
        return False
    return age <= max_age_for(fact_key)
