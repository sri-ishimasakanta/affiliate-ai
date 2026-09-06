"""Search Console 取り込みの deterministic な identity 計算 (pure)。

property + 期間 + dimension set + search type + aggregation + import version を束縛する。
canonical JSON (:func:`app.article.draft_input_canonical.canonical_json`) → UTF-8 → SHA-256。
"""

from __future__ import annotations

import hashlib
from datetime import date

from app.article.draft_input_canonical import canonical_json

# V1 は web 検索・byPage 集計に固定。将来拡張時は import version を上げる。
V1_SEARCH_TYPE = "web"
V1_AGGREGATION_TYPE = "byPage"
V1_IMPORT_VERSION = 1

# 1 回の import で取得する dimension set 群 (順序固定)。
V1_DIMENSION_SETS: tuple[tuple[str, ...], ...] = (
    ("date", "page"),
    ("date", "page", "query"),
)


def dimensions_json() -> str:
    """``dimensions_json`` カラムに保存する canonical 表現。"""

    return canonical_json([list(d) for d in V1_DIMENSION_SETS])


def compute_import_identity_hash(
    *, property_uri: str, start_date: date, end_date: date
) -> str:
    identity = {
        "property_uri": property_uri,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "dimension_sets": [list(d) for d in V1_DIMENSION_SETS],
        "search_type": V1_SEARCH_TYPE,
        "aggregation_type": V1_AGGREGATION_TYPE,
        "import_version": V1_IMPORT_VERSION,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
