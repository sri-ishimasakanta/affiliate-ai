"""ArticleIndexabilityReportService の統合テスト (C5.1)。

``httpx.MockTransport`` と fake inspection client で、実サイト・実 Google に触れず
に合成ロジックを検証する。pin する契約:

- production DB の published 記事が唯一の URL 一覧の出所 (別リストを持たない)。
- sitemap 掲載 / live 健全性 / Google のインデックス状態 / 検索実績は別概念。
- Search Analytics に行が無いことを未インデックスと解釈しない。
- URL Inspection が使えない場合は ``GSC_UNKNOWN`` で、限界を明示する。
- この service は DB にも外部にも書き込まない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.models import Article, SearchConsoleImportRun, SearchConsolePageDaily
from app.seo.indexability import (
    ACTION_MANUAL_INDEX_REQUEST,
    ACTION_TECHNICAL_FIX,
    ACTION_WAIT,
    GSC_DISCOVERED_NOT_INDEXED,
    GSC_INDEXED,
    GSC_UNKNOWN,
    LIVE_HEALTHY,
    LIVE_NOINDEX,
    SITEMAP_MISSING,
    SITEMAP_PRESENT,
)
from app.services.article_indexability_report_service import (
    ArticleIndexabilityReportService,
)

_BASE = "https://bizfluxlab.com"
_ROBOTS = "User-agent: *\nDisallow: /wp-admin/\nSitemap: https://bizfluxlab.com/wp-sitemap.xml\n"
_INDEX = (
    "<sitemapindex><sitemap><loc>https://bizfluxlab.com/wp-sitemap-posts-post-1.xml"
    "</loc></sitemap></sitemapindex>"
)


class _Settings:
    wordpress_base_url = _BASE
    search_console_property_uri = "sc-domain:bizfluxlab.com"
    search_console_credentials_file = None


class _FakeInspectionClient:
    """read-only の inspection fake。呼び出し URL を記録する。"""

    def __init__(self, verdicts: dict[str, dict] | None = None, raises: bool = False) -> None:
        self._verdicts = verdicts or {}
        self._raises = raises
        self.calls: list[str] = []

    def inspect(self, url: str, *, language_code: str = "ja"):
        self.calls.append(url)
        if self._raises:
            raise RuntimeError("inspection unavailable")
        payload = self._verdicts.get(url)

        class _Result:
            ok = payload is not None
            inspection_result = payload
            error_status = None if payload is not None else "PERMISSION_DENIED"
            error_message = None if payload is not None else "not an owner"

        return _Result()


def _page(html: str) -> httpx.Response:
    return httpx.Response(200, html=html)


def _article_html(url: str, *, noindex: bool = False) -> str:
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    return (
        f'<html><head><title>t</title><link rel="canonical" href="{url}">{robots}</head>'
        f"<body><h1>h</h1><p>{'本文' * 600}</p></body></html>"
    )


def _make_client(*, sitemap_urls: list[str], noindex_urls: tuple[str, ...] = ()) -> httpx.Client:
    post_sitemap = (
        "<urlset>"
        + "".join(
            f"<url><loc>{u}</loc><lastmod>2026-09-22T00:00:00+09:00</lastmod></url>"
            for u in sitemap_urls
        )
        + "</urlset>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=_ROBOTS)
        if path == "/wp-sitemap.xml":
            return httpx.Response(200, text=_INDEX)
        if path == "/wp-sitemap-posts-post-1.xml":
            return httpx.Response(200, text=post_sitemap)
        url = str(request.url)
        return _page(_article_html(url, noindex=url in noindex_urls))

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _seed_articles(session: Session, slugs: list[str]) -> list[Article]:
    articles = []
    for index, slug in enumerate(slugs, start=1):
        article = Article(
            title=f"title {index}",
            slug=slug,
            keyword_id=None,
            body="本文",
            status="published",
            published_url=f"{_BASE}/{slug}/",
            published_at=datetime.now(UTC) - timedelta(days=30),
            wordpress_post_id=str(60 + index),
        )
        session.add(article)
        articles.append(article)
    session.add(
        Article(title="draft", slug="not-published", keyword_id=None, body="x", status="review")
    )
    session.commit()
    return articles


def _import_run(session: Session) -> SearchConsoleImportRun:
    run = SearchConsoleImportRun(
        property_uri="sc-domain:bizfluxlab.com",
        start_date=datetime.now(UTC).date(),
        end_date=datetime.now(UTC).date(),
        status="succeeded",
        dimensions_json='[["date","page"]]',
        import_identity_hash="f" * 64,
    )
    session.add(run)
    session.commit()
    return run


def _build(session: Session, client: httpx.Client, inspection=None, inspect: bool = True):
    return ArticleIndexabilityReportService(
        session,
        settings=_Settings(),
        http_client=client,
        inspection_client=inspection,
    ).build(inspect=inspect)


# ==================== inventory ==============================================
def test_only_published_articles_are_inventoried(session: Session) -> None:
    _seed_articles(session, ["a", "b"])
    with _make_client(sitemap_urls=[f"{_BASE}/a/", f"{_BASE}/b/"]) as client:
        report = _build(session, client, _FakeInspectionClient())
    assert report.article_count == 2
    assert [r.url for r in report.articles] == [f"{_BASE}/a/", f"{_BASE}/b/"]


def test_healthy_article_in_sitemap(session: Session) -> None:
    _seed_articles(session, ["a"])
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, _FakeInspectionClient())
    row = report.articles[0]
    assert row.live_state == LIVE_HEALTHY
    assert row.sitemap_state == SITEMAP_PRESENT


def test_article_missing_from_sitemap_is_a_technical_fix(session: Session) -> None:
    _seed_articles(session, ["a", "b"])
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, _FakeInspectionClient())
    missing = [r for r in report.articles if r.url.endswith("/b/")][0]
    assert missing.sitemap_state == SITEMAP_MISSING
    assert missing.action == ACTION_TECHNICAL_FIX


def test_sitemap_extras_are_listed_separately(session: Session) -> None:
    _seed_articles(session, ["a"])
    with _make_client(sitemap_urls=[f"{_BASE}/a/", f"{_BASE}/about/"]) as client:
        report = _build(session, client, _FakeInspectionClient())
    assert report.sitemap_extra_urls == [f"{_BASE}/about/"]


def test_trailing_slash_and_encoding_differences_still_match_the_sitemap(
    session: Session,
) -> None:
    _seed_articles(session, ["a"])
    with _make_client(sitemap_urls=[f"{_BASE}/a"]) as client:
        report = _build(session, client, _FakeInspectionClient())
    assert report.articles[0].sitemap_state == SITEMAP_PRESENT


# ==================== live defects ===========================================
def test_noindex_article_is_detected(session: Session) -> None:
    _seed_articles(session, ["a"])
    with _make_client(sitemap_urls=[f"{_BASE}/a/"], noindex_urls=(f"{_BASE}/a/",)) as client:
        report = _build(session, client, _FakeInspectionClient())
    assert report.articles[0].live_state == LIVE_NOINDEX
    assert report.articles[0].action == ACTION_TECHNICAL_FIX


# ==================== index state vs performance =============================
def test_indexed_verdict_is_reported(session: Session) -> None:
    _seed_articles(session, ["a"])
    inspection = _FakeInspectionClient({f"{_BASE}/a/": {"indexStatusResult": {"verdict": "PASS"}}})
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, inspection)
    assert report.articles[0].google_index_state == GSC_INDEXED
    assert report.inspection_available is True


def test_discovered_not_indexed_is_a_manual_request_candidate(session: Session) -> None:
    _seed_articles(session, ["a"])
    inspection = _FakeInspectionClient(
        {
            f"{_BASE}/a/": {
                "indexStatusResult": {
                    "verdict": "NEUTRAL",
                    "sitemap": [f"{_BASE}/wp-sitemap.xml"],
                }
            }
        }
    )
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, inspection)
    assert report.articles[0].google_index_state == GSC_DISCOVERED_NOT_INDEXED
    assert report.articles[0].action == ACTION_MANUAL_INDEX_REQUEST


def test_unavailable_inspection_is_unknown_and_records_the_limitation(
    session: Session,
) -> None:
    _seed_articles(session, ["a"])
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, _FakeInspectionClient())
    assert report.articles[0].google_index_state == GSC_UNKNOWN
    assert report.inspection_available is False
    assert "PERMISSION_DENIED" in (report.inspection_limitation or "")
    assert report.articles[0].action == ACTION_WAIT


def test_inspection_exception_does_not_break_the_report(session: Session) -> None:
    _seed_articles(session, ["a"])
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, _FakeInspectionClient(raises=True))
    assert report.articles[0].google_index_state == GSC_UNKNOWN
    assert report.inspection_limitation == "RuntimeError"


def test_no_inspect_skips_every_inspection_call(session: Session) -> None:
    _seed_articles(session, ["a"])
    inspection = _FakeInspectionClient()
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, inspection, inspect=False)
    assert inspection.calls == []
    assert report.articles[0].google_index_state == GSC_UNKNOWN
    assert "not requested" in (report.inspection_limitation or "")


def test_absent_performance_rows_do_not_imply_missing_index(session: Session) -> None:
    _seed_articles(session, ["a"])
    inspection = _FakeInspectionClient({f"{_BASE}/a/": {"indexStatusResult": {"verdict": "PASS"}}})
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, inspection)
    row = report.articles[0]
    assert row.has_performance_rows is False
    assert row.google_index_state == GSC_INDEXED


def test_performance_rows_are_matched_on_normalized_urls(session: Session) -> None:
    _seed_articles(session, ["a"])
    session.add(
        SearchConsolePageDaily(
            property_uri="sc-domain:bizfluxlab.com",
            metric_date=datetime.now(UTC).date(),
            page=f"{_BASE}/a",  # 末尾スラッシュなしの表記揺れ
            clicks=2,
            impressions=40,
            ctr=0.05,
            position=12.5,
            source_import_run_id=_import_run(session).id,
        )
    )
    session.commit()
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, _FakeInspectionClient())
    row = report.articles[0]
    assert row.has_performance_rows is True
    assert row.impressions == 40
    assert row.clicks == 2
    assert row.average_position == 12.5


def test_performance_rows_for_other_pages_are_not_attributed_to_articles(
    session: Session,
) -> None:
    _seed_articles(session, ["a"])
    session.add(
        SearchConsolePageDaily(
            property_uri="sc-domain:bizfluxlab.com",
            metric_date=datetime.now(UTC).date(),
            page=f"{_BASE}/category/gyomu-koritsuka/",
            clicks=0,
            impressions=1,
            ctr=0.0,
            position=97.0,
            source_import_run_id=_import_run(session).id,
        )
    )
    session.commit()
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, _FakeInspectionClient())
    assert report.articles[0].has_performance_rows is False
    assert report.articles[0].impressions == 0


# ==================== read-only ==============================================
def test_report_does_not_mutate_articles(session: Session) -> None:
    articles = _seed_articles(session, ["a"])
    before = (articles[0].status, articles[0].published_url, articles[0].body)
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        _build(session, client, _FakeInspectionClient())
    session.refresh(articles[0])
    assert (articles[0].status, articles[0].published_url, articles[0].body) == before


def test_robots_and_sitemap_discovery_facts_are_reported(session: Session) -> None:
    _seed_articles(session, ["a"])
    with _make_client(sitemap_urls=[f"{_BASE}/a/"]) as client:
        report = _build(session, client, _FakeInspectionClient())
    assert report.robots_txt_status == 200
    assert report.robots_txt_sitemaps == [f"{_BASE}/wp-sitemap.xml"]
    assert report.sitemap_entry_point == f"{_BASE}/wp-sitemap.xml"
    assert report.sitemap_entry_point_source == "robots.txt"
    assert report.sitemap_url_count == 1
