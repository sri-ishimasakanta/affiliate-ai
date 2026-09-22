"""ソースごとの鮮度事実の収集 (C8.5)。

各取り込みの run 表から、:class:`SourceFreshness` の 3 つの値を組み立てる:

- ``coverage_through``          -- 成功した取り込みが問い合わせ終えた最終日
- ``latest_observed_data_date`` -- 実データの行が存在する最終日
- ``last_successful_import_at`` -- 直近で取り込みが成功した時刻

判定はしない (それは :mod:`app.operations.source_health` の責務)。ここは
「何が事実か」を既存の run 表から読むだけで、行を作りも変えもしない。

**0 行で成功した問い合わせも coverage を前進させる。** metric 行を捏造することは
決してない -- 行が無い日は行が無いまま扱う。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AffiliateClickImportRun,
    AffiliateCommissionFact,
    AffiliateCommissionImportRun,
    AffiliateOutboundClick,
    Ga4ImportRun,
    Ga4PageDaily,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
)
from app.operations.source_health import SourceFreshness

SOURCE_SEARCH_CONSOLE = "search_console"
SOURCE_GA4 = "ga4"
SOURCE_AFFILIATE_CLICKS = "affiliate_clicks"
SOURCE_MAKE_COMMISSIONS = "make_commissions"

SOURCES = (
    SOURCE_SEARCH_CONSOLE,
    SOURCE_GA4,
    SOURCE_AFFILIATE_CLICKS,
    SOURCE_MAKE_COMMISSIONS,
)

_SUCCEEDED = "succeeded"


def collect_source_freshness(session: Session) -> dict[str, SourceFreshness]:
    return {
        SOURCE_SEARCH_CONSOLE: _search_console(session),
        SOURCE_GA4: _ga4(session),
        SOURCE_AFFILIATE_CLICKS: _affiliate_clicks(session),
        SOURCE_MAKE_COMMISSIONS: _make_commissions(session),
    }


def _search_console(session: Session) -> SourceFreshness:
    coverage = session.scalar(
        select(func.max(SearchConsoleImportRun.end_date)).where(
            SearchConsoleImportRun.status == _SUCCEEDED
        )
    )
    finished = session.scalar(
        select(func.max(SearchConsoleImportRun.finished_at)).where(
            SearchConsoleImportRun.status == _SUCCEEDED
        )
    )
    latest = session.scalar(select(func.max(SearchConsolePageDaily.metric_date)))
    rows = session.scalar(select(func.count()).select_from(SearchConsolePageDaily)) or 0
    return SourceFreshness(
        source=SOURCE_SEARCH_CONSOLE,
        coverage_through=_as_date(coverage),
        latest_observed_data_date=_as_date(latest),
        last_successful_import_at=_as_datetime(finished),
        ever_had_data=rows > 0,
        ever_imported=coverage is not None,
    )


def _ga4(session: Session) -> SourceFreshness:
    coverage = session.scalar(
        select(func.max(Ga4ImportRun.end_date)).where(Ga4ImportRun.status == _SUCCEEDED)
    )
    finished = session.scalar(
        select(func.max(Ga4ImportRun.finished_at)).where(Ga4ImportRun.status == _SUCCEEDED)
    )
    latest = session.scalar(select(func.max(Ga4PageDaily.metric_date)))
    rows = session.scalar(select(func.count()).select_from(Ga4PageDaily)) or 0
    return SourceFreshness(
        source=SOURCE_GA4,
        coverage_through=_as_date(coverage),
        latest_observed_data_date=_as_date(latest),
        last_successful_import_at=_as_datetime(finished),
        ever_had_data=rows > 0,
        ever_imported=coverage is not None,
    )


def _affiliate_clicks(session: Session) -> SourceFreshness:
    """click は日付範囲ではなく cursor で取り込むため、coverage は「接触時刻」で表す。

    新しいクリックが 0 件なのは正常なので、``coverage_stale_after_days`` は設定せず、
    取り込みの成功時刻だけで健全性を見る。
    """

    finished = session.scalar(
        select(func.max(AffiliateClickImportRun.finished_at)).where(
            AffiliateClickImportRun.status == _SUCCEEDED
        )
    )
    latest = session.scalar(select(func.max(AffiliateOutboundClick.clicked_at)))
    runs = (
        session.scalar(
            select(func.count())
            .select_from(AffiliateClickImportRun)
            .where(AffiliateClickImportRun.status == _SUCCEEDED)
        )
        or 0
    )
    rows = session.scalar(select(func.count()).select_from(AffiliateOutboundClick)) or 0
    return SourceFreshness(
        source=SOURCE_AFFILIATE_CLICKS,
        # cursor ベースなので「どの日付まで問い合わせた」という概念が無い。
        coverage_through=None,
        latest_observed_data_date=_as_date(latest),
        last_successful_import_at=_as_datetime(finished),
        ever_had_data=rows > 0,
        ever_imported=runs > 0,
    )


def _make_commissions(session: Session) -> SourceFreshness:
    coverage = session.scalar(
        select(func.max(AffiliateCommissionImportRun.requested_date_to)).where(
            AffiliateCommissionImportRun.status == _SUCCEEDED
        )
    )
    finished = session.scalar(
        select(func.max(AffiliateCommissionImportRun.finished_at)).where(
            AffiliateCommissionImportRun.status == _SUCCEEDED
        )
    )
    latest = session.scalar(select(func.max(AffiliateCommissionFact.occurred_at)))
    rows = session.scalar(select(func.count()).select_from(AffiliateCommissionFact)) or 0
    return SourceFreshness(
        source=SOURCE_MAKE_COMMISSIONS,
        coverage_through=_as_date(coverage),
        latest_observed_data_date=_as_date(latest),
        last_successful_import_at=_as_datetime(finished),
        ever_had_data=rows > 0,
        ever_imported=coverage is not None,
    )


def _as_date(value) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def _as_datetime(value) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
