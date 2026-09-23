"""週次運用レポートの意味論 (C8.7)。

これまでのフェーズで決めた区別が、レポートでも崩れないことを pin する:

- coverage (問い合わせ終端) と activity (最後に観測できた日) を分ける (C8.5)。
- 観測が無いものを 0 と書かない。``unavailable`` と書く。
- クリックは raw / excluded / clean を 3 つ同時に出す (C7)。
- Make の報酬を記事単位に割り当てない (C7)。
- 効果は因果を主張しない (C9.3)。
- read-only -- DB に何も書かない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Article,
    ChangeApplication,
    ChangeRequest,
    OperationsRun,
    RevenueOptimizationRun,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
)
from app.operations.report_format import render_weekly_report, weekly_subject
from app.services.operations_weekly_report_service import (
    UNAVAILABLE,
    OperationsWeeklyReportService,
)

_NOW = datetime(2026, 9, 23, 6, 40, tzinfo=UTC)
_PROPERTY = "sc-domain:example.com"


class _Settings:
    wordpress_base_url = "https://example.com"
    search_console_property_uri = _PROPERTY
    search_console_credentials_file = None
    ga4_property_id = None
    operations_email_enabled = False


def _build(session: Session, **kwargs):
    return OperationsWeeklyReportService(session, settings=_Settings()).build(now=_NOW, **kwargs)


def test_coverage_and_activity_are_reported_separately(session: Session) -> None:
    """取り込みは 9/22 まで問い合わせ済みだが、データは 9/20 までしか無い。"""

    run = SearchConsoleImportRun(
        property_uri=_PROPERTY,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 22),
        status="succeeded",
        dimensions_json='[["date","page"]]',
        import_identity_hash="a" * 64,
        finished_at=_NOW,
    )
    session.add(run)
    session.commit()
    session.add(
        SearchConsolePageDaily(
            property_uri=_PROPERTY,
            metric_date=date(2026, 9, 20),
            page="https://example.com/a/",
            clicks=1,
            impressions=40,
            ctr=0.025,
            position=11.0,
            source_import_run_id=run.id,
        )
    )
    session.commit()

    report = _build(session)
    gsc = report.search_console

    assert gsc["coverage_through"] == "2026-09-22"
    assert gsc["latest_observed_data_date"] == "2026-09-20"
    assert gsc["impressions"] == 40
    assert gsc["clicks"] == 1
    # provider の日付境界を揃っているふりをしない。
    assert "Pacific Time" in gsc["note"]


def test_missing_measurement_is_unavailable_not_zero(session: Session) -> None:
    report = _build(session)

    assert report.search_console["impressions"] == UNAVAILABLE
    assert report.search_console["clicks"] == UNAVAILABLE
    assert report.ga4["sessions"] == UNAVAILABLE
    assert report.ga4["organic_sessions"] == UNAVAILABLE
    assert "0" != report.ga4["sessions"]


def test_click_counts_keep_the_raw_excluded_clean_distinction(session: Session) -> None:
    session.add(
        RevenueOptimizationRun(
            policy_version="rev-1",
            window_start=date(2026, 8, 24),
            window_end=date(2026, 9, 22),
            trusted_measurement_start_at=datetime(2026, 9, 23, tzinfo=UTC),
            raw_clicks=29,
            excluded_clicks=29,
            clean_clicks=0,
            evaluated_article_count=25,
            monetized_article_count=3,
            candidate_count=13,
        )
    )
    session.commit()

    revenue = _build(session).revenue

    assert revenue["raw_clicks"] == 29
    assert revenue["excluded_clicks"] == 29
    assert revenue["clean_clicks"] == 0
    # clean だけを見せて「クリックゼロ」と読ませない。
    assert {"raw_clicks", "excluded_clicks", "clean_clicks"} <= set(revenue)


def test_article_level_revenue_stays_unavailable(session: Session) -> None:
    session.add(
        RevenueOptimizationRun(
            policy_version="rev-1",
            window_start=date(2026, 8, 24),
            window_end=date(2026, 9, 22),
            raw_clicks=0,
            excluded_clicks=0,
            clean_clicks=0,
            evaluated_article_count=25,
            monetized_article_count=3,
            candidate_count=0,
        )
    )
    session.commit()

    revenue = _build(session).revenue

    assert revenue["article_level_revenue"] == UNAVAILABLE
    assert "join key" in revenue["attribution_note"]


def test_content_changes_never_claim_causality(session: Session) -> None:
    report = _build(session)

    assert report.content_changes["causal_claim"] == "none"
    body = render_weekly_report(report)
    assert "causal_claim=none" in body
    assert "改善した" not in body.replace("改善したとは書かない", "")


def test_an_absent_weekly_run_makes_the_report_incomplete(session: Session) -> None:
    report = _build(session)

    assert report.complete is False
    assert any("no weekly operations run" in r for r in report.incomplete_reasons)
    assert "incomplete" in weekly_subject(report)


def test_a_succeeded_weekly_run_makes_the_report_complete(session: Session) -> None:
    session.add(
        OperationsRun(
            profile="weekly",
            policy_version="ops-1",
            effective_date=date(2026, 9, 23),
            timezone_name="Asia/Tokyo",
            trigger="scheduler",
            status="succeeded",
            step_total=8,
            step_succeeded=8,
            started_at=_NOW,
            finished_at=_NOW,
        )
    )
    session.commit()

    report = _build(session)

    assert report.complete is True
    assert report.incomplete_reasons == []
    assert "incomplete" not in weekly_subject(report)


def test_the_report_is_read_only(session: Session) -> None:
    before = (
        session.scalar(select(func.count()).select_from(Article)),
        session.scalar(select(func.count()).select_from(ChangeRequest)),
        session.scalar(select(func.count()).select_from(ChangeApplication)),
        session.scalar(select(func.count()).select_from(OperationsRun)),
    )

    _build(session)

    after = (
        session.scalar(select(func.count()).select_from(Article)),
        session.scalar(select(func.count()).select_from(ChangeRequest)),
        session.scalar(select(func.count()).select_from(ChangeApplication)),
        session.scalar(select(func.count()).select_from(OperationsRun)),
    )
    assert before == after


def test_the_rendered_body_stays_readable(session: Session) -> None:
    """携帯で読めるサイズに収める (巨大な JSON を貼らない)。"""

    body = render_weekly_report(_build(session))
    lines = body.split("\n")

    assert len(body) < 12000
    assert max(len(line) for line in lines) < 220
    for section in ("SYSTEM", "SEARCH CONSOLE", "GA4", "SEO / C6", "REVENUE / C7", "ALERTS"):
        assert section in body


def test_the_report_contains_no_secret_shaped_values(session: Session) -> None:
    """secret そのものが載らないこと。

    末尾の注意書きは ``/go/ token は含まない`` と **書いている** ので、単純な
    部分一致では判定できない。実際の URL / 代入の形で探す。
    """

    import re

    body = render_weekly_report(_build(session))

    assert re.search(r"https?://\S*/go/\S+", body) is None
    assert re.search(r"(?i)(password|secret|api[_-]?key|token)\s*[=:]\s*\S+", body) is None
    assert "Bearer " not in body
    assert "private_key" not in body
    assert "BEGIN PRIVATE KEY" not in body
