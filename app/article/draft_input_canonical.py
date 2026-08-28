"""DraftInputSnapshot payload の canonical 化と semantic content hash (pure)。

DB / SQLAlchemy / FastAPI 非依存。builder (preview / freeze 共通) がこのモジュールで
canonical な文字列表現を作り、``content_hash`` を計算する。

方針
----
* payload に格納する値は **最初から canonical な形** にする (datetime は UTC 秒精度
  ``+00:00`` 文字列、commission は Decimal 由来の固定桁文字列)。payload は JSON 列に
  そのまま入るので JSON-native (str/num/bool/null/list/dict) のみ。
* ``content_hash`` は「保存 payload をそのまま hash」ではなく、
  :func:`semantic_payload_for_hash` で **意味的入力だけ** を取り出してから hash する。
  非意味的 (audit/debug) 情報はすべて payload の ``"audit"`` サブツリーに置く約束。
* 同一 instant は offset に依らず同一 canonical string → 同一 hash。
* 意味的入力が 1 つでも変われば hash が変わる。builder ロジックが意味的に変わったら
  ``BUILDER_VERSION`` を更新する (これも hash INCLUDE)。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

# content_hash から除外する payload トップレベルキー。
# 非意味的な値は必ずここ (audit) の下に置くこと。
SEMANTIC_EXCLUDED_TOP_KEYS: frozenset[str] = frozenset({"audit"})

_COMMISSION_QUANT = Decimal("0.0001")


def canonical_datetime(value: datetime | None) -> str | None:
    """datetime を UTC instant・秒精度・``YYYY-MM-DDTHH:MM:SS+00:00`` へ正規化する。

    naive は UTC とみなす。``+09:00`` / ``-05:00`` などの aware は同一 instant なら
    同一文字列になる。``None`` は ``None``。
    """

    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(microsecond=0).isoformat()


def canonical_commission(value: float | int | Decimal | None) -> str | None:
    """commission 値を float 表現に依存しない固定桁文字列にする (例 ``"35.0000"``)。"""

    if value is None:
        return None
    return str(Decimal(str(value)).quantize(_COMMISSION_QUANT))


def semantic_payload_for_hash(payload: dict) -> dict:
    """payload から content_hash 対象の意味的部分だけを取り出す。

    現状はトップレベル ``"audit"`` を落とすだけ (builder が非意味的値を audit 配下に
    集約しているため)。将来 exclude を増やす場合もこの関数に集約する。
    """

    return {k: v for k, v in payload.items() if k not in SEMANTIC_EXCLUDED_TOP_KEYS}


def canonical_json(obj: object) -> str:
    """decision 用の安定した JSON 文字列。

    ``sort_keys`` で dict キー順を固定、``separators`` で余白を排除、``allow_nan=False``
    で NaN/Infinity を拒否。list の順序は呼び出し側 (builder) が決める。
    """

    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def compute_content_hash(payload: dict) -> str:
    """payload の semantic 部分の SHA-256 hex (64 文字)。"""

    canonical = canonical_json(semantic_payload_for_hash(payload))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
