"""記事 1 本の SEO 状態を表す決定的な小さなモデル (C5.1)。

**4 つの別概念を混ぜない** -- これがこの module の唯一の設計上の主張である:

1. ``live_state``        -- 公開ページ自体の技術的なインデックス可能性
2. ``sitemap_state``     -- sitemap に載っているか (発見経路)
3. ``google_index_state``-- Google が報告しているインデックス状態 (URL Inspection)
4. ``performance``       -- Search Analytics の実績 (クリック/表示回数)

Search Analytics に行があることは「インデックスされている」ことの証明ではなく、
行が無いことは「インデックスされていない」ことの証明でもない。逆に URL
Inspection の verdict は performance を意味しない。したがってこの module では
1 つの総合スコアに潰さず、状態を並べたまま保持する。

``action`` だけは運用のために 1 つに畳むが、その根拠 (``action_reason``) を必ず
併記する。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# -- 1. live technical indexability ------------------------------------------
LIVE_HEALTHY = "LIVE_HEALTHY"
LIVE_REDIRECTED = "LIVE_REDIRECTED"
LIVE_CANONICAL_MISMATCH = "LIVE_CANONICAL_MISMATCH"
LIVE_NOINDEX = "LIVE_NOINDEX"
LIVE_ROBOTS_BLOCKED = "LIVE_ROBOTS_BLOCKED"
LIVE_HTTP_ERROR = "LIVE_HTTP_ERROR"
LIVE_THIN_OR_EMPTY = "LIVE_THIN_OR_EMPTY"
LIVE_UNREACHABLE = "LIVE_UNREACHABLE"

# -- 2. sitemap discovery ------------------------------------------------------
SITEMAP_PRESENT = "SITEMAP_PRESENT"
SITEMAP_MISSING = "SITEMAP_MISSING"
SITEMAP_UNKNOWN = "SITEMAP_UNKNOWN"

# -- 3. Google-reported index state -------------------------------------------
GSC_INDEXED = "GSC_INDEXED"
GSC_DISCOVERED_NOT_INDEXED = "GSC_DISCOVERED_NOT_INDEXED"
GSC_CRAWLED_NOT_INDEXED = "GSC_CRAWLED_NOT_INDEXED"
GSC_NOT_KNOWN_TO_GOOGLE = "GSC_NOT_KNOWN_TO_GOOGLE"
GSC_EXCLUDED = "GSC_EXCLUDED"
GSC_UNKNOWN = "GSC_UNKNOWN"

# -- action -------------------------------------------------------------------
ACTION_NONE = "none"
ACTION_WAIT = "wait"
ACTION_MANUAL_INDEX_REQUEST = "manual index request candidate"
ACTION_TECHNICAL_FIX = "technical fix required"
ACTION_INVESTIGATE = "investigate"

#: URL Inspection の ``coverageState`` はロケール依存の人間向け文字列なので、
#: 機械判定には ``verdict`` と ``robotsTxtState`` / ``indexingState`` を使う。
_VERDICT_PASS = "PASS"
_VERDICT_FAIL = "FAIL"
_VERDICT_NEUTRAL = "NEUTRAL"

#: 公開直後に「未インデックス」なのは正常。この日数を過ぎて初めて手動申請を検討する。
_DISCOVERY_GRACE_DAYS = 3

#: 本文がこの文字数未満なら「実質空」とみなす (テーマの共通部分だけのページ)。
_MIN_BODY_TEXT_LENGTH = 500


@dataclass(frozen=True)
class LiveAssessment:
    state: str
    issues: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.state == LIVE_HEALTHY


def assess_live(
    *,
    expected_url: str,
    final_url: str | None,
    final_status: int | None,
    redirected: bool,
    declares_noindex: bool,
    canonical: str | None,
    robots_allowed: bool,
    text_length: int,
    error: str | None = None,
    url_matcher=None,
) -> LiveAssessment:
    """公開ページの技術的な状態を 1 つに決める (最初に当たった致命的問題が勝つ)。

    ``url_matcher`` は 2 つの URL が同じページかを返す callable
    (既定は :func:`app.seo.url_normalization.same_url`)。
    """

    if url_matcher is None:
        from app.seo.url_normalization import same_url as url_matcher  # 遅延 import

    issues: list[str] = []
    if error is not None or final_status is None:
        return LiveAssessment(LIVE_UNREACHABLE, (f"fetch failed: {error or 'no status'}",))
    if final_status >= 400:
        return LiveAssessment(LIVE_HTTP_ERROR, (f"HTTP {final_status}",))
    if not robots_allowed:
        return LiveAssessment(LIVE_ROBOTS_BLOCKED, ("robots.txt disallows this path",))
    if declares_noindex:
        return LiveAssessment(LIVE_NOINDEX, ("noindex declared",))
    if redirected or not url_matcher(expected_url, final_url):
        issues.append(f"redirected to {final_url}")
        return LiveAssessment(LIVE_REDIRECTED, tuple(issues))
    if canonical is not None and not url_matcher(canonical, expected_url):
        return LiveAssessment(LIVE_CANONICAL_MISMATCH, (f"canonical points at {canonical}",))
    if canonical is None:
        issues.append("no canonical link element")
    if text_length < _MIN_BODY_TEXT_LENGTH:
        return LiveAssessment(LIVE_THIN_OR_EMPTY, (f"only {text_length} characters of text",))
    return LiveAssessment(LIVE_HEALTHY, tuple(issues))


def assess_google_index_state(inspection: dict | None) -> tuple[str, dict[str, str | None]]:
    """URL Inspection の ``indexStatusResult`` を内部の状態語彙に写す。

    取得できていなければ ``GSC_UNKNOWN`` -- 「未取得」と「未インデックス」を
    決して混同しない。
    """

    if not inspection:
        return GSC_UNKNOWN, {}
    status = inspection.get("indexStatusResult") or {}
    facts = {
        "verdict": status.get("verdict"),
        "coverage_state": status.get("coverageState"),
        "robots_txt_state": status.get("robotsTxtState"),
        "indexing_state": status.get("indexingState"),
        "page_fetch_state": status.get("pageFetchState"),
        "last_crawl_time": status.get("lastCrawlTime"),
        "google_canonical": status.get("googleCanonical"),
        "user_canonical": status.get("userCanonical"),
        "crawled_as": status.get("crawledAs"),
        "referring_urls": ", ".join(status.get("referringUrls") or []) or None,
        "sitemaps": ", ".join(status.get("sitemap") or []) or None,
    }
    verdict = facts["verdict"]
    if verdict == _VERDICT_PASS:
        return GSC_INDEXED, facts
    if verdict == _VERDICT_FAIL:
        return GSC_EXCLUDED, facts
    if verdict == _VERDICT_NEUTRAL:
        last_crawl = facts["last_crawl_time"]
        if last_crawl:
            return GSC_CRAWLED_NOT_INDEXED, facts
        # crawl 実績が無い NEUTRAL は「発見済みだが未クロール」か「未認識」。
        # referring/ sitemap の証跡があれば発見済みとみなす。
        if facts["sitemaps"] or facts["referring_urls"]:
            return GSC_DISCOVERED_NOT_INDEXED, facts
        return GSC_NOT_KNOWN_TO_GOOGLE, facts
    return GSC_UNKNOWN, facts


@dataclass(frozen=True)
class ArticleIndexabilityState:
    """1 記事の SEO 状態。4 つの概念を分離したまま保持する。"""

    article_id: int
    url: str
    live_state: str
    live_issues: tuple[str, ...]
    sitemap_state: str
    google_index_state: str
    google_facts: dict[str, str | None] = field(default_factory=dict)
    has_performance_rows: bool = False
    impressions: int = 0
    clicks: int = 0
    average_position: float | None = None
    action: str = ACTION_NONE
    action_reason: str = ""


def decide_action(
    *,
    live_state: str,
    sitemap_state: str,
    google_index_state: str,
    days_since_publication: int | None,
    discovery_grace_days: int = _DISCOVERY_GRACE_DAYS,
) -> tuple[str, str]:
    """状態から運用アクションを 1 つ決める。根拠の文言も返す。

    既定は「何もしない」寄り。手動のインデックス登録リクエストを勧めるのは、

    1. 公開ページが技術的に健全で、
    2. sitemap に載っていて、
    3. Google がまだインデックスしておらず、
    4. **公開から猶予期間を過ぎている**

    の 4 つが揃ったときだけ。公開直後の記事が「未インデックス」なのは異常では
    なく、sitemap を出した直後に申請しても意味が薄いので ``wait`` にする。
    """

    if live_state in (LIVE_HTTP_ERROR, LIVE_UNREACHABLE, LIVE_NOINDEX, LIVE_ROBOTS_BLOCKED):
        return ACTION_TECHNICAL_FIX, f"live state is {live_state}"
    if live_state in (LIVE_CANONICAL_MISMATCH, LIVE_REDIRECTED, LIVE_THIN_OR_EMPTY):
        return ACTION_INVESTIGATE, f"live state is {live_state}"
    if sitemap_state == SITEMAP_MISSING:
        return ACTION_TECHNICAL_FIX, "healthy page is absent from the sitemap"
    if google_index_state == GSC_INDEXED:
        return ACTION_NONE, "Google reports the URL as indexed"
    if google_index_state == GSC_EXCLUDED:
        return ACTION_INVESTIGATE, "Google reports the URL as excluded"
    if google_index_state == GSC_UNKNOWN:
        return ACTION_WAIT, "no Google-reported index state available"
    # クロール済みで未インデックスなのは Google の品質判断であり、同じ URL を
    # 再申請しても通常は変わらない -- 内容側の改善を待つ。
    if google_index_state == GSC_CRAWLED_NOT_INDEXED:
        return ACTION_WAIT, "crawled but not yet indexed; re-requesting rarely helps"
    # NOT_KNOWN / DISCOVERED -- 猶予期間を過ぎたものだけが手動申請の候補。
    if days_since_publication is not None and days_since_publication < discovery_grace_days:
        return (
            ACTION_WAIT,
            f"published {days_since_publication} day(s) ago; within the "
            f"{discovery_grace_days}-day discovery grace period",
        )
    age = (
        f"published {days_since_publication} day(s) ago"
        if days_since_publication is not None
        else "publication date unknown"
    )
    return (
        ACTION_MANUAL_INDEX_REQUEST,
        f"healthy, in sitemap, still not indexed ({google_index_state}), {age}",
    )
