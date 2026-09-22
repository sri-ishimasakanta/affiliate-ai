"""app.analytics.rows の単体テスト (C5.2)。

GA4 は値を文字列で返すため、型の確定と契約違反の検出をここで pin する。
``pagePath`` の query string を落とす方針も、意図的な決定として固定する。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.analytics.rows import (
    Ga4PageRow,
    coerce_count,
    coerce_ratio,
    coerce_seconds,
    normalize_page_path,
    parse_ga4_date,
    validate_page_row,
)
from app.exceptions import ExternalProviderDataError


def _row(**over) -> Ga4PageRow:
    kwargs = dict(
        metric_date=date(2026, 9, 22),
        page_path="/rpa-tools/",
        channel_scope="all",
        sessions=10,
        active_users=8,
        new_users=6,
        engaged_sessions=5,
        engagement_rate=0.5,
        average_engagement_time_seconds=42.5,
        screen_page_views=12,
    )
    kwargs.update(over)
    return Ga4PageRow(**kwargs)


# ==================== date ====================================================
def test_ga4_date_is_parsed() -> None:
    assert parse_ga4_date("20260922") == date(2026, 9, 22)


@pytest.mark.parametrize("value", ["2026-09-22", "202609", "", None, "20261301"])
def test_bad_ga4_date_is_rejected(value) -> None:
    with pytest.raises(ExternalProviderDataError):
        parse_ga4_date(value)


# ==================== page path ===============================================
def test_query_string_is_dropped() -> None:
    assert normalize_page_path("/rpa-tools/?utm_source=x&gclid=y") == "/rpa-tools/"


def test_fragment_is_dropped() -> None:
    assert normalize_page_path("/rpa-tools/#section") == "/rpa-tools/"


def test_trailing_slash_is_preserved() -> None:
    assert normalize_page_path("/rpa-tools/") == "/rpa-tools/"
    assert normalize_page_path("/rpa-tools") == "/rpa-tools"


def test_bare_query_becomes_root() -> None:
    assert normalize_page_path("/?utm=1") == "/"


def test_relative_path_is_rejected() -> None:
    with pytest.raises(ExternalProviderDataError):
        normalize_page_path("rpa-tools/")


def test_empty_page_path_is_rejected() -> None:
    with pytest.raises(ExternalProviderDataError):
        normalize_page_path("  ")


# ==================== numbers =================================================
def test_counts_are_coerced_from_strings() -> None:
    assert coerce_count("12", field="sessions") == 12
    assert coerce_count("", field="sessions") == 0
    assert coerce_count(None, field="sessions") == 0


def test_negative_count_is_rejected() -> None:
    with pytest.raises(ExternalProviderDataError):
        coerce_count("-1", field="sessions")


def test_fractional_count_is_rejected() -> None:
    with pytest.raises(ExternalProviderDataError):
        coerce_count("1.5", field="sessions")


def test_ratio_outside_zero_to_one_is_rejected() -> None:
    assert coerce_ratio("0.75", field="engagementRate") == 0.75
    with pytest.raises(ExternalProviderDataError):
        coerce_ratio("1.5", field="engagementRate")


def test_negative_seconds_is_rejected() -> None:
    assert coerce_seconds("12.5", field="d") == 12.5
    with pytest.raises(ExternalProviderDataError):
        coerce_seconds("-1", field="d")


# ==================== row invariants ==========================================
def test_valid_row_passes() -> None:
    assert validate_page_row(_row()) is not None


def test_engaged_sessions_cannot_exceed_sessions() -> None:
    with pytest.raises(ExternalProviderDataError):
        validate_page_row(_row(sessions=2, engaged_sessions=3))


def test_unknown_channel_scope_is_rejected() -> None:
    with pytest.raises(ExternalProviderDataError):
        validate_page_row(_row(channel_scope="paid"))


def test_negative_metric_is_rejected() -> None:
    with pytest.raises(ExternalProviderDataError):
        validate_page_row(_row(sessions=-1))
