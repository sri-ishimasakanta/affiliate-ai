"""ArticleIndexabilityReportService -- 公開記事の SEO 状態を 1 回で棚卸しする (C5.1)。

「公開済みの全記事について、いま live / sitemap / インデックス / 検索実績は
どうなっているか」に答えるための read-mostly な合成 service。

構成要素はすべて既存のものを **合成** するだけで、再実装しない:

- 記事一覧          -- :class:`ArticleRepository` (production DB が唯一の正)
- 公開ページの観測  -- :func:`app.seo.live_probe.probe_url`
- robots.txt        -- :func:`app.seo.robots_rules.parse_robots_txt`
- sitemap           -- :func:`app.seo.sitemap.discover_sitemaps`
- index 状態        -- :class:`UrlInspectionClient` (read-only)
- 検索実績          -- C1.1 が取り込んだ ``SearchConsolePageDaily`` /
                       ``SearchConsoleQueryDaily`` (再取得はしない)

**この service は書き込みを一切行わない** -- DB にも WordPress にも Google にも。
Search Console のデータ取り込みは C1.1 の import workflow の責務であり、ここでは
既に取り込まれた行を読むだけ。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, SearchConsolePageDaily, SearchConsoleQueryDaily
from app.seo.indexability import (
    GSC_UNKNOWN,
    SITEMAP_MISSING,
    SITEMAP_PRESENT,
    SITEMAP_UNKNOWN,
    ArticleIndexabilityState,
    assess_google_index_state,
    assess_live,
    decide_action,
)
from app.seo.live_probe import probe_url
from app.seo.robots_rules import RobotsTxt, parse_robots_txt
from app.seo.sitemap import SitemapInventory, discover_sitemaps
from app.seo.url_normalization import normalize_url_key, same_url

_PUBLISHED = "published"
_TIMEOUT_SECONDS = 30.0


@dataclass
class IndexabilityReport:
    """棚卸しの結果 (本文・credential は含まない)。"""

    generated_at: datetime
    site_base_url: str
    property_uri: str | None
    article_count: int
    robots_txt_status: int | None = None
    robots_txt_sitemaps: list[str] = field(default_factory=list)
    sitemap_entry_point: str | None = None
    sitemap_entry_point_source: str | None = None
    sitemap_documents: list[dict] = field(default_factory=list)
    sitemap_url_count: int = 0
    sitemap_extra_urls: list[str] = field(default_factory=list)
    inspection_available: bool = False
    inspection_limitation: str | None = None
    performance_data_through: date | None = None
    articles: list[ArticleIndexabilityState] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        payload["performance_data_through"] = (
            self.performance_data_through.isoformat() if self.performance_data_through else None
        )
        return payload


class ArticleIndexabilityReportService:
    def __init__(
        self,
        session: Session,
        *,
        settings,
        http_client: httpx.Client | None = None,
        inspection_client=None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._http_client = http_client
        self._inspection_client = inspection_client

    # -- public ---------------------------------------------------------------
    def build(self, *, inspect: bool = True) -> IndexabilityReport:
        base = (self._settings.wordpress_base_url or "").rstrip("/")
        owns_client = self._http_client is None
        http = self._http_client or httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=True)
        try:
            report = IndexabilityReport(
                generated_at=datetime.now(UTC),
                site_base_url=base,
                property_uri=getattr(self._settings, "search_console_property_uri", None),
                article_count=0,
            )
            robots = self._load_robots(http, base, report)
            sitemap = self._load_sitemap(http, base, report)
            sitemap_keys = {normalize_url_key(u) for u in sitemap.urls}

            if not inspect:
                report.inspection_limitation = "not requested (--no-inspect)"

            articles = self._published_articles()
            report.article_count = len(articles)
            performance = self._performance_by_url()
            report.performance_data_through = self._latest_metric_date()

            article_keys: set[str | None] = set()
            for article in articles:
                url = article.published_url or ""
                article_keys.add(normalize_url_key(url))
                probe = probe_url(url, client=http)
                path = urlsplit(url).path or "/"
                live = assess_live(
                    expected_url=url,
                    final_url=probe.final_url,
                    final_status=probe.final_status,
                    redirected=probe.redirected,
                    declares_noindex=probe.declares_noindex,
                    canonical=probe.canonical,
                    robots_allowed=robots.is_allowed(path),
                    text_length=probe.text_length,
                    error=probe.error,
                    url_matcher=same_url,
                )
                if not sitemap.urls:
                    sitemap_state = SITEMAP_UNKNOWN
                elif normalize_url_key(url) in sitemap_keys:
                    sitemap_state = SITEMAP_PRESENT
                else:
                    sitemap_state = SITEMAP_MISSING

                index_state, google_facts = GSC_UNKNOWN, {}
                if inspect:
                    inspection = self._inspect(url, report)
                    index_state, google_facts = assess_google_index_state(inspection)

                metrics = performance.get(normalize_url_key(url))
                action, reason = decide_action(
                    live_state=live.state,
                    sitemap_state=sitemap_state,
                    google_index_state=index_state,
                    days_since_publication=_days_since(article.published_at),
                )
                report.articles.append(
                    ArticleIndexabilityState(
                        article_id=article.id,
                        url=url,
                        live_state=live.state,
                        live_issues=live.issues,
                        sitemap_state=sitemap_state,
                        google_index_state=index_state,
                        google_facts=google_facts,
                        has_performance_rows=metrics is not None,
                        impressions=metrics["impressions"] if metrics else 0,
                        clicks=metrics["clicks"] if metrics else 0,
                        average_position=metrics["position"] if metrics else None,
                        action=action,
                        action_reason=reason,
                    )
                )

            report.sitemap_url_count = len(sitemap.urls)
            report.sitemap_extra_urls = sorted(
                u for u in sitemap.urls if normalize_url_key(u) not in article_keys
            )
            report.warnings.extend(sitemap.errors)
            return report
        finally:
            if owns_client:
                http.close()

    # -- internals ------------------------------------------------------------
    def _published_articles(self) -> list[Article]:
        stmt = (
            select(Article)
            .where(Article.status == _PUBLISHED, Article.published_url.is_not(None))
            .order_by(Article.id)
        )
        return list(self._session.scalars(stmt).all())

    def _load_robots(self, http: httpx.Client, base: str, report: IndexabilityReport) -> RobotsTxt:
        try:
            response = http.get(f"{base}/robots.txt")
        except Exception as exc:  # noqa: BLE001 - 取得失敗も結果
            report.warnings.append(f"robots.txt unreadable: {type(exc).__name__}")
            return parse_robots_txt("")
        report.robots_txt_status = response.status_code
        if response.status_code != 200:
            report.warnings.append(f"robots.txt returned HTTP {response.status_code}")
            return parse_robots_txt("")
        robots = parse_robots_txt(response.text)
        report.robots_txt_sitemaps = list(robots.sitemaps)
        return robots

    def _load_sitemap(
        self, http: httpx.Client, base: str, report: IndexabilityReport
    ) -> SitemapInventory:
        inventory = discover_sitemaps(base, client=http)
        report.sitemap_entry_point = inventory.entry_point
        report.sitemap_entry_point_source = inventory.entry_point_source
        report.sitemap_documents = [
            {
                "url": d.url,
                "status": d.status,
                "is_index": d.is_index,
                "loc_count": len(d.locs),
                "error": d.error,
            }
            for d in inventory.documents
        ]
        return inventory

    def _inspect(self, url: str, report: IndexabilityReport) -> dict | None:
        client = self._inspection_client
        if client is None:
            from app.search_console.url_inspection_client import UrlInspectionClient

            client = self._inspection_client = UrlInspectionClient(self._settings)
        try:
            result = client.inspect(url)
        except Exception as exc:  # noqa: BLE001 - 能力が無いことも結果
            report.inspection_limitation = f"{type(exc).__name__}"
            return None
        if result.ok:
            report.inspection_available = True
            return result.inspection_result
        if report.inspection_limitation is None:
            report.inspection_limitation = (
                f"{result.error_status}: {result.error_message}"
                if result.error_status
                else "inspection unavailable"
            )
        return None

    def _performance_by_url(self) -> dict[str | None, dict]:
        """C1.1 が取り込んだ page 行を URL キーで集計する (再取得はしない)。"""

        rows = self._session.execute(
            select(
                SearchConsolePageDaily.page,
                func.sum(SearchConsolePageDaily.clicks),
                func.sum(SearchConsolePageDaily.impressions),
                func.avg(SearchConsolePageDaily.position),
                func.max(SearchConsolePageDaily.metric_date),
            ).group_by(SearchConsolePageDaily.page)
        ).all()
        out: dict[str | None, dict] = {}
        for page, clicks, impressions, position, latest in rows:
            key = normalize_url_key(page)
            bucket = out.setdefault(
                key,
                {"clicks": 0, "impressions": 0, "position": None, "latest": None, "pages": []},
            )
            bucket["clicks"] += int(clicks or 0)
            bucket["impressions"] += int(impressions or 0)
            bucket["position"] = float(position) if position is not None else bucket["position"]
            bucket["latest"] = max(filter(None, (bucket["latest"], latest)), default=None)
            bucket["pages"].append(page)
        return out

    def _latest_metric_date(self) -> date | None:
        return self._session.scalar(select(func.max(SearchConsolePageDaily.metric_date)))

    def queries_for_url(self, url: str) -> list[SearchConsoleQueryDaily]:
        """その URL に紐づく query 行 (正規化キーで突き合わせる)。"""

        key = normalize_url_key(url)
        rows = self._session.scalars(select(SearchConsoleQueryDaily)).all()
        return [r for r in rows if normalize_url_key(r.page) == key]


def _days_since(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - moment).days, 0)
