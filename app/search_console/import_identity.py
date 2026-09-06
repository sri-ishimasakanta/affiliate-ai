"""Search Console 取り込みの deterministic な identity 計算 (pure)。

property + 期間 + dimension set + search type + aggregation + dataState + import
version を束縛する。canonical JSON (:func:`app.article.draft_input_canonical.
canonical_json`) → UTF-8 → SHA-256。

V2 (C1-B で確定した Google Search Analytics API contract):

* DAILY テーブルに正しく ``metric_date`` を入れるため dimension に ``date`` を含める。
* ``page`` で grouping する場合、Google の現行 contract に従い ``aggregationType`` は
  ``auto`` を使う。
* 安定した確定値のみを取り込むため ``dataState`` は ``final`` に固定。

production の ``search_console_import_runs`` は 0 件なので V1 互換は不要。
"""

from __future__ import annotations

import hashlib
from datetime import date

from app.article.draft_input_canonical import canonical_json

# V2 contract。設定で広げさせない code-level 定数。
SEARCH_TYPE = "web"
AGGREGATION_TYPE = "auto"
DATA_STATE = "final"
IMPORT_VERSION = 2

# 1 回の import で取得する dimension set (順序固定)。
PAGE_DIMENSIONS: tuple[str, ...] = ("date", "page")
QUERY_DIMENSIONS: tuple[str, ...] = ("date", "page", "query")
DIMENSION_SETS: tuple[tuple[str, ...], ...] = (PAGE_DIMENSIONS, QUERY_DIMENSIONS)


def dimensions_json() -> str:
    """``dimensions_json`` カラムに保存する canonical 表現 (V2)。"""

    return canonical_json([list(d) for d in DIMENSION_SETS])


def compute_import_identity_hash(
    *, property_uri: str, start_date: date, end_date: date
) -> str:
    """取り込みデータセットの意味的な identity を表す SHA-256 hex。

    pagination 制御 (rowLimit / startRow) や credential (token / path / SA email)
    は identity に**含めない**。データセットの意味を変えないため。
    """

    identity = {
        "property_uri": property_uri,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "page_dimensions": list(PAGE_DIMENSIONS),
        "query_dimensions": list(QUERY_DIMENSIONS),
        "search_type": SEARCH_TYPE,
        "aggregation_type": AGGREGATION_TYPE,
        "data_state": DATA_STATE,
        "import_version": IMPORT_VERSION,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
