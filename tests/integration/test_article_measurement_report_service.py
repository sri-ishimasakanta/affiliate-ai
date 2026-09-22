"""ArticleMeasurementReportService の統合テスト (C5.2/C5.3)。

pin する契約 -- **4 つの出所を混ぜない** こと:

- Search Console / GA4 / affiliate click / commission をそれぞれ独立に集計する。
- 分母 0 のとき比率 (CTR / engagement rate) を出さない (0.0 を捏造しない)。
- 報酬を記事へ按分しない。記事に紐付かない報酬は ``UNATTRIBUTED_TO_ARTICLE``
  として **プログラム単位のまま** 出す。
- 成果件数を推定しない (``conversions`` は分からない限り ``None``)。
- GA4 の organic は total の部分集合として別 scope で保持する。
- 公開直後を「勝ち負け」で判定せず、記述的な状態だけを付ける。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import (
    AffiliateClickImportRun,
    AffiliateCommissionFact,
    AffiliateCommissionImportRun,
    AffiliateLinkTarget,
    AffiliateOutboundClick,
    AffiliateProgram,
    Article,
    Ga4ImportRun,
    Ga4PageDaily,
    Keyword,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
    SearchConsoleQueryDaily,
)
from app.services.article_measurement_report_service import (
    ATTRIBUTION_PROGRAM_ONLY,
    ATTRIBUTION_UNAVAILABLE,
    STATE_AFFILIATE_CLICKS_OBSERVED,
    STATE_AWAITING_GA4_DATA,
    STATE_AWAITING_GSC_DATA,
    STATE_GA4_NOT_CONFIGURED,
    STATE_IMPRESSIONS_OBSERVED,
    STATE_SESSIONS_OBSERVED,
    UNATTRIBUTED_TO_ARTICLE,
    ArticleMeasurementReportService,
)

_BASE = "https://bizfluxlab.com"
_PROPERTY = "sc-domain:bizfluxlab.com"
_GA4 = "987654321"
_TODAY = datetime.now(UTC).date()


class _Settings:
    wordpress_base_url = _BASE
    search_console_property_uri = _PROPERTY
    search_console_credentials_file = None
    ga4_property_id: str | None = None


class _Ga4Settings(_Settings):
    ga4_property_id = _GA4


def _seed_articles(session: Session, slugs=("make-how-to", "rpa-tools")) -> list[Article]:
    keyword = Keyword(keyword="make 使い方")
    session.add(keyword)
    session.commit()
    articles = []
    for index, slug in enumerate(slugs, start=1):
        article = Article(
            id=index * 10,
            title=slug,
            slug=slug,
            keyword_id=keyword.id if index == 1 else None,
            body="x",
            status="published",
            published_url=f"{_BASE}/{slug}/",
            published_at=datetime.now(UTC) - timedelta(days=5),
            article_type="how_to",
            monetization_mode="affiliate" if index == 1 else "supporting",
        )
        session.add(article)
        articles.append(article)
    session.commit()
    return articles


def _sc_run(session: Session) -> SearchConsoleImportRun:
    run = SearchConsoleImportRun(
        property_uri=_PROPERTY,
        start_date=_TODAY,
        end_date=_TODAY,
        status="succeeded",
        dimensions_json='[["date","page"]]',
        import_identity_hash="a" * 64,
    )
    session.add(run)
    session.commit()
    return run


def _ga4_run(session: Session, timezone: str | None = "Asia/Tokyo") -> Ga4ImportRun:
    run = Ga4ImportRun(
        property_id=_GA4,
        property_timezone=timezone,
        start_date=_TODAY,
        end_date=_TODAY,
        status="succeeded",
        request_json="{}",
        import_identity_hash="b" * 64,
    )
    session.add(run)
    session.commit()
    return run


def _sc_page(session: Session, run, *, page: str, clicks=0, impressions=0, position=0.0) -> None:
    session.add(
        SearchConsolePageDaily(
            property_uri=_PROPERTY,
            metric_date=_TODAY,
            page=page,
            clicks=clicks,
            impressions=impressions,
            ctr=(clicks / impressions) if impressions else 0.0,
            position=position,
            source_import_run_id=run.id,
        )
    )
    session.commit()


def _ga4_page(session: Session, run, *, path: str, scope="all", sessions=0, **over) -> None:
    payload = dict(
        active_users=max(sessions - 1, 0),
        new_users=max(sessions - 2, 0),
        engaged_sessions=max(sessions - 3, 0),
        engagement_rate=0.5,
        average_engagement_time_seconds=60.0,
        screen_page_views=sessions,
    )
    payload.update(over)
    session.add(
        Ga4PageDaily(
            property_id=_GA4,
            metric_date=_TODAY,
            page_path=path,
            channel_scope=scope,
            sessions=sessions,
            source_import_run_id=run.id,
            **payload,
        )
    )
    session.commit()


def _build(session: Session, *, settings=None, days: int = 30):
    return ArticleMeasurementReportService(session, settings=settings or _Settings()).build(
        days=days
    )


# ==================== identity ================================================
def test_every_published_article_is_reported(session: Session) -> None:
    _seed_articles(session)
    report = _build(session)
    assert report.article_count == 2
    assert [r.article_id for r in report.articles] == [10, 20]
    assert report.articles[0].keyword == "make 使い方"
    assert report.articles[1].keyword is None


# ==================== search console ==========================================
def test_search_console_rows_are_mapped_to_the_article(session: Session) -> None:
    _seed_articles(session)
    run = _sc_run(session)
    _sc_page(session, run, page=f"{_BASE}/make-how-to/", clicks=3, impressions=100, position=8.0)

    row = _build(session).articles[0]

    assert row.impressions == 100
    assert row.clicks == 3
    assert row.ctr == 0.03
    assert row.average_position == 8.0
    assert STATE_IMPRESSIONS_OBSERVED in row.states


def test_ctr_is_none_when_there_are_no_impressions(session: Session) -> None:
    _seed_articles(session)
    row = _build(session).articles[0]
    assert row.ctr is None
    assert row.average_position is None
    assert STATE_AWAITING_GSC_DATA in row.states


def test_position_is_weighted_by_impressions(session: Session) -> None:
    _seed_articles(session)
    run = _sc_run(session)
    _sc_page(session, run, page=f"{_BASE}/make-how-to/", impressions=90, position=10.0)
    session.add(
        SearchConsolePageDaily(
            property_uri=_PROPERTY,
            metric_date=_TODAY - timedelta(days=1),
            page=f"{_BASE}/make-how-to/",
            clicks=0,
            impressions=10,
            ctr=0.0,
            position=100.0,
            source_import_run_id=run.id,
        )
    )
    session.commit()

    assert _build(session).articles[0].average_position == 19.0


def test_top_queries_are_only_reported_when_rows_exist(session: Session) -> None:
    articles = _seed_articles(session)
    run = _sc_run(session)
    session.add(
        SearchConsoleQueryDaily(
            property_uri=_PROPERTY,
            metric_date=_TODAY,
            page=articles[0].published_url,
            query="make 使い方",
            clicks=1,
            impressions=20,
            ctr=0.05,
            position=7.0,
            source_import_run_id=run.id,
        )
    )
    session.commit()

    report = _build(session)
    assert report.articles[0].query_count == 1
    assert report.articles[0].top_queries[0]["query"] == "make 使い方"
    assert report.articles[1].query_count == 0
    assert report.articles[1].top_queries == []


def test_url_variants_still_map_to_the_article(session: Session) -> None:
    _seed_articles(session)
    run = _sc_run(session)
    _sc_page(session, run, page="https://www.bizfluxlab.com/make-how-to", impressions=5)
    assert _build(session).articles[0].impressions == 5


# ==================== ga4 =====================================================
def test_ga4_total_and_organic_are_kept_separate(session: Session) -> None:
    _seed_articles(session)
    run = _ga4_run(session)
    _ga4_page(session, run, path="/make-how-to/", scope="all", sessions=10)
    _ga4_page(session, run, path="/make-how-to/", scope="organic_search", sessions=4)

    row = _build(session, settings=_Ga4Settings()).articles[0]

    assert row.sessions == 10
    assert row.organic_sessions == 4
    assert STATE_SESSIONS_OBSERVED in row.states


def test_engagement_rate_is_none_without_sessions(session: Session) -> None:
    _seed_articles(session)
    row = _build(session, settings=_Ga4Settings()).articles[0]
    assert row.engagement_rate is None
    assert row.average_engagement_time_seconds is None
    assert STATE_AWAITING_GA4_DATA in row.states


def test_unconfigured_ga4_is_stated_explicitly(session: Session) -> None:
    _seed_articles(session)
    report = _build(session)
    assert report.ga4_configured is False
    assert STATE_GA4_NOT_CONFIGURED in report.articles[0].states


def test_ga4_property_timezone_is_surfaced(session: Session) -> None:
    _seed_articles(session)
    _ga4_run(session, timezone="Asia/Tokyo")
    assert _build(session, settings=_Ga4Settings()).ga4_property_timezone == "Asia/Tokyo"


# ==================== affiliate ===============================================
def _affiliate_click(session: Session, *, article_id: int, token="tok") -> None:
    program = session.get(AffiliateProgram, 1) or AffiliateProgram(name="Make", status="active")
    if program.id is None:
        session.add(program)
        session.commit()
    session.add(
        AffiliateLinkTarget(
            token=token,
            article_id=article_id,
            affiliate_program_id=program.id,
            destination_url="https://www.make.com/en/register",
            destination_host="www.make.com",
            status="active",
            link_identity_hash=token.ljust(64, "0"),
        )
    )
    run = AffiliateClickImportRun(status="succeeded", requested_since_id=0, requested_limit=100)
    session.add(run)
    session.commit()
    session.add(
        AffiliateOutboundClick(
            source_click_id=1,
            token=token,
            clicked_at=datetime.now(UTC),
            source_import_run_id=run.id,
        )
    )
    session.commit()


def test_affiliate_clicks_are_attributed_to_the_article(session: Session) -> None:
    _seed_articles(session)
    _affiliate_click(session, article_id=10)

    report = _build(session)
    row = report.articles[0]

    assert row.affiliate_clicks == 1
    assert row.affiliate_programs == ["Make"]
    assert STATE_AFFILIATE_CLICKS_OBSERVED in row.states
    assert report.articles[1].affiliate_clicks == 0


def test_conversions_and_commission_stay_unknown_for_an_article(session: Session) -> None:
    """click は記事に帰属できるが、報酬は provider に join key が無い。"""

    _seed_articles(session)
    _affiliate_click(session, article_id=10)

    row = _build(session).articles[0]

    assert row.attribution_scope == ATTRIBUTION_PROGRAM_ONLY
    assert row.conversions is None
    assert row.commission_amount is None


def test_article_without_affiliate_links_reports_unavailable(session: Session) -> None:
    _seed_articles(session)
    assert _build(session).articles[1].attribution_scope == ATTRIBUTION_UNAVAILABLE


def test_commission_is_reported_at_program_level_only(session: Session) -> None:
    _seed_articles(session)
    program = AffiliateProgram(name="Make", status="active")
    session.add(program)
    session.commit()
    import_run = AffiliateCommissionImportRun(
        provider="make",
        affiliate_program_id=program.id,
        status="succeeded",
        requested_date_from=_TODAY,
        requested_date_to=_TODAY,
    )
    session.add(import_run)
    session.commit()
    session.add(
        AffiliateCommissionFact(
            affiliate_program_id=program.id,
            provider="make",
            source_commission_id="c1",
            event_type="sale",
            provider_status="approved",
            commission_amount=Decimal("12.50"),
            currency="USD",
            occurred_at=datetime.now(UTC),
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
            source_import_run_id=import_run.id,
        )
    )
    session.commit()

    report = _build(session)

    assert len(report.unattributed_commissions) == 1
    bucket = report.unattributed_commissions[0]
    assert bucket["attribution"] == UNATTRIBUTED_TO_ARTICLE
    assert bucket["conversions"] == 1
    assert bucket["commission_amount"] == "12.50"
    assert bucket["currency"] == "USD"
    # 記事側には一切配分されない。
    assert all(r.commission_amount is None for r in report.articles)
    assert all(r.conversions is None for r in report.articles)


# ==================== data quality ============================================
def test_unmapped_search_console_page_is_reported(session: Session) -> None:
    _seed_articles(session)
    run = _sc_run(session)
    _sc_page(session, run, page=f"{_BASE}/category/gyomu-koritsuka/", impressions=1)

    findings = _build(session).data_quality
    assert any("not mapped to an article" in f for f in findings)


def test_future_dated_rows_are_reported(session: Session) -> None:
    _seed_articles(session)
    run = _sc_run(session)
    session.add(
        SearchConsolePageDaily(
            property_uri=_PROPERTY,
            metric_date=_TODAY + timedelta(days=3),
            page=f"{_BASE}/make-how-to/",
            clicks=0,
            impressions=1,
            ctr=0.0,
            position=1.0,
            source_import_run_id=run.id,
        )
    )
    session.commit()

    assert any("future metric_date" in f for f in _build(session).data_quality)


def test_organic_exceeding_total_sessions_is_reported(session: Session) -> None:
    _seed_articles(session)
    run = _ga4_run(session)
    _ga4_page(session, run, path="/make-how-to/", scope="all", sessions=2)
    _ga4_page(session, run, path="/make-how-to/", scope="organic_search", sessions=9)

    findings = _build(session, settings=_Ga4Settings()).data_quality
    assert any("organic sessions exceed total sessions" in f for f in findings)


def test_unattributed_click_tokens_are_reported(session: Session) -> None:
    _seed_articles(session)
    run = AffiliateClickImportRun(status="succeeded", requested_since_id=0, requested_limit=100)
    session.add(run)
    session.commit()
    session.add(
        AffiliateOutboundClick(
            source_click_id=1,
            token="tok-unknown",
            clicked_at=datetime.now(UTC),
            source_import_run_id=run.id,
        )
    )
    session.commit()

    findings = _build(session).data_quality
    assert any("no affiliate link target" in f for f in findings)


def test_clean_state_has_no_findings(session: Session) -> None:
    _seed_articles(session)
    run = _sc_run(session)
    _sc_page(session, run, page=f"{_BASE}/make-how-to/", clicks=1, impressions=10, position=5.0)
    assert _build(session).data_quality == []


def test_window_bounds_are_reported(session: Session) -> None:
    _seed_articles(session)
    report = _build(session, days=7)
    assert report.window_end == _TODAY
    assert report.window_start == _TODAY - timedelta(days=6)
    assert isinstance(report.window_start, date)
