"""GA4 取り込みの deterministic な identity 計算 (pure)。

property + 期間 + dimension/metric set + channel scope + import version を束縛し、
canonical JSON -> UTF-8 -> SHA-256 で identity を作る。pagination 制御や
credential は identity に含めない (データセットの意味を変えないため)。
"""

from __future__ import annotations

import hashlib
from datetime import date

from app.article.draft_input_canonical import canonical_json

IMPORT_VERSION = 1

#: 行の次元。``date`` は property のタイムゾーン暦日、``pagePath`` は query string を
#: 含まない (:mod:`app.analytics.rows` で正規化する)。
PAGE_DIMENSIONS: tuple[str, ...] = ("date", "pagePath")

#: 取得する指標。GA4 が返す値をそのまま保存し、こちらで再計算しない。
PAGE_METRICS: tuple[str, ...] = (
    "sessions",
    "activeUsers",
    "newUsers",
    "engagedSessions",
    "engagementRate",
    "userEngagementDuration",
    "screenPageViews",
)

#: 全トラフィックとオーガニック検索を **別 scope の行** として取り込む。
CHANNEL_SCOPE_ALL = "all"
CHANNEL_SCOPE_ORGANIC_SEARCH = "organic_search"
CHANNEL_SCOPES: tuple[str, ...] = (CHANNEL_SCOPE_ALL, CHANNEL_SCOPE_ORGANIC_SEARCH)

#: organic scope を切り出す GA4 の documented な dimension と値。
ORGANIC_DIMENSION = "sessionDefaultChannelGroup"
ORGANIC_DIMENSION_VALUE = "Organic Search"


def request_json() -> str:
    """``request_json`` カラムに保存する canonical 表現。"""

    return canonical_json(
        {
            "dimensions": list(PAGE_DIMENSIONS),
            "metrics": list(PAGE_METRICS),
            "channel_scopes": list(CHANNEL_SCOPES),
            "organic_filter": {
                "dimension": ORGANIC_DIMENSION,
                "value": ORGANIC_DIMENSION_VALUE,
            },
        }
    )


def compute_import_identity_hash(*, property_id: str, start_date: date, end_date: date) -> str:
    identity = {
        "property_id": property_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "dimensions": list(PAGE_DIMENSIONS),
        "metrics": list(PAGE_METRICS),
        "channel_scopes": list(CHANNEL_SCOPES),
        "organic_dimension": ORGANIC_DIMENSION,
        "organic_dimension_value": ORGANIC_DIMENSION_VALUE,
        "import_version": IMPORT_VERSION,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
