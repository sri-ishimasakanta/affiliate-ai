"""URL の索引の状態 (C10-A、provider に依らない形、pure)。

「検索の実績が無い」「索引されていない」「状態が分からない」を分ける。

- **生の状態** (``raw_status``): Google の URL Inspection の値そのまま (``verdict`` /
  ``coverageState`` / ``robotsTxtState`` / ``indexingState`` / ``pageFetchState`` /
  ``lastCrawlTime``) と、既存の内部の語彙 (``GSC_*``、``app/seo/indexability.py``)。
- **正規化した状態** (``normalized_status``): 下の 8 つ。Google の応答に無い状態は作らない。

  ============================  ==========================================================
  ``indexed``                   verdict PASS (``GSC_INDEXED``)
  ``crawled_not_indexed``       verdict NEUTRAL + クロールの記録あり
  ``discovered_not_indexed``    verdict NEUTRAL + クロール無し + sitemap / 参照あり
  ``not_indexed``               verdict NEUTRAL + 手掛かり無し ("URL is unknown to Google")
  ``excluded``                  verdict FAIL (``GSC_EXCLUDED``。Google の "Excluded")
  ``blocked``                   robotsTxtState DISALLOWED、または indexingState BLOCKED_*
  ``error``                     調べたが結果を得られなかった (provider の失敗)
  ``unknown``                   調べていない・結果が無い (**「索引されていない」ではない**)
  ============================  ==========================================================

``GSC_UNKNOWN`` は ``unknown`` のまま。日ごとの確認 (URL Inspection をしない) の結果を、
索引の状態の代わりに使わない。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.seo.indexability import (
    GSC_CRAWLED_NOT_INDEXED,
    GSC_DISCOVERED_NOT_INDEXED,
    GSC_EXCLUDED,
    GSC_INDEXED,
    GSC_NOT_KNOWN_TO_GOOGLE,
    GSC_UNKNOWN,
)

INDEXED = "indexed"
CRAWLED_NOT_INDEXED = "crawled_not_indexed"
DISCOVERED_NOT_INDEXED = "discovered_not_indexed"
NOT_INDEXED = "not_indexed"
EXCLUDED = "excluded"
BLOCKED = "blocked"
ERROR = "error"
UNKNOWN = "unknown"
NORMALIZED_STATES = (INDEXED, CRAWLED_NOT_INDEXED, DISCOVERED_NOT_INDEXED, NOT_INDEXED,
                     EXCLUDED, BLOCKED, ERROR, UNKNOWN)  # fmt: skip
#: 確かに分かっている状態 (unknown / error 以外)。
KNOWN_STATES = frozenset(NORMALIZED_STATES) - {UNKNOWN, ERROR}

PROVIDER_URL_INSPECTION = "google_search_console_url_inspection"

_FROM_GSC = {
    GSC_INDEXED: INDEXED,
    GSC_CRAWLED_NOT_INDEXED: CRAWLED_NOT_INDEXED,
    GSC_DISCOVERED_NOT_INDEXED: DISCOVERED_NOT_INDEXED,
    GSC_NOT_KNOWN_TO_GOOGLE: NOT_INDEXED,
    GSC_EXCLUDED: EXCLUDED,
    GSC_UNKNOWN: UNKNOWN,
}
#: 生の値 (Google の enum) で「止められている」を示すもの。
_BLOCKED_ROBOTS = frozenset({"DISALLOWED"})
_BLOCKED_INDEXING = frozenset({"BLOCKED_BY_META_TAG", "BLOCKED_BY_HTTP_HEADER",
                               "BLOCKED_BY_ROBOTS_TXT"})  # fmt: skip
#: 保存する生の値 (URL を含むもの・他のサイトの参照は保存しない)。
RAW_FIELDS = ("verdict", "coverage_state", "robots_txt_state", "indexing_state",
              "page_fetch_state", "last_crawl_time")  # fmt: skip


def normalize_index_state(google_index_state: str | None, raw: dict | None = None, *,
                          inspected: bool = True, provider_error: bool = False) -> str:
    """生の状態 → 正規化した状態 (決定論的)。"""

    raw = raw or {}
    if provider_error:
        return ERROR
    if not inspected or not google_index_state:
        return UNKNOWN
    if raw.get("robots_txt_state") in _BLOCKED_ROBOTS or (
            raw.get("indexing_state") in _BLOCKED_INDEXING):
        return BLOCKED
    return _FROM_GSC.get(google_index_state, UNKNOWN)


@dataclass(frozen=True)
class IndexObservation:
    article_id: int
    url: str | None
    provider: str
    #: 調べた時刻 (その確認の実行が終わった時刻)。
    observed_at: str | None
    #: どの記録から (例: ``operations_step_runs:72 (weekly, inspect)``)。
    data_source: str
    raw_status: dict = field(default_factory=dict)
    normalized_status: str = UNKNOWN
    last_crawl: str | None = None
    #: URL Inspection をした確認の結果か (日ごとの確認は False)。
    inspected: bool = False
    #: fresh / stale / missing (``app.analysis.sources``)。
    freshness_state: str = "missing"
    missing_reason: str | None = None
    #: サイト側の確認 (公開ページ・sitemap)。最新の確認 (日ごと) の値。Google の状態ではない。
    site_checks: dict = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.normalized_status in KNOWN_STATES

    def as_dict(self) -> dict:
        return {**asdict(self), "known": self.known}


def observation_from_row(row: dict, *, observed_at: str | None, data_source: str,
                         inspected: bool, freshness_state: str) -> IndexObservation:
    """``check_indexability`` の 1 記事の行 → 観測。"""

    raw = {k: row.get(k) for k in RAW_FIELDS if row.get(k) is not None}
    raw["google_index_state"] = row.get("google_index_state")
    status = normalize_index_state(row.get("google_index_state"), raw, inspected=inspected)
    reason = None
    if status == UNKNOWN:
        reason = ("the run did not use URL Inspection" if not inspected else
                  "URL Inspection returned no index status for this URL")
    return IndexObservation(
        article_id=int(row["article_id"]), url=row.get("url"), provider=PROVIDER_URL_INSPECTION,
        observed_at=observed_at, data_source=data_source, raw_status=raw,
        normalized_status=status, last_crawl=row.get("last_crawl_time"), inspected=inspected,
        freshness_state=freshness_state if inspected else "missing", missing_reason=reason)


__all__ = ["BLOCKED", "CRAWLED_NOT_INDEXED", "DISCOVERED_NOT_INDEXED", "ERROR", "EXCLUDED",
           "INDEXED", "IndexObservation", "KNOWN_STATES", "NORMALIZED_STATES", "NOT_INDEXED",
           "PROVIDER_URL_INSPECTION", "RAW_FIELDS", "UNKNOWN", "normalize_index_state",
           "observation_from_row"]
