"""WordPress outbound-click export レスポンスの typed 表現と厳格な検証 (pure)。

DB / network 非依存。:func:`validate_click_export_page` は 1 ページ分の JSON
(``dict``) を受け取り、契約 (deployed MU-plugin ``/outbound-clicks``) に完全一致する
場合のみ ``(rows, next_since_id)`` を返す。1 つでも違反があれば
``AffiliateClickImportError(http_status=200)`` を送出する。

エラー文言には raw body / token 値 / secret / signature を一切含めない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.exceptions import AffiliateClickImportError

#: export レスポンスの schema バージョン (deployed PHP は 1 固定)。
CLICK_EXPORT_SCHEMA_VERSION = 1

#: AffiliateLinkTarget.token と同じ字種・長さ制約。
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{16,64}\Z")

#: WordPress は ``gmdate('Y-m-d H:i:s')`` を UTC で返す。
_CLICKED_AT_RE = re.compile(r"\A\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\Z")
_CLICKED_AT_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class ClickRow:
    """export が返した 1 click。``clicked_at_utc`` は tz-aware UTC。"""

    source_click_id: int
    token: str
    clicked_at_utc: datetime


def _err(reason: str) -> AffiliateClickImportError:
    return AffiliateClickImportError(reason, http_status=200)


def _require_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(f"click export {field} is not an integer")
    return value


def _parse_clicked_at(value: object) -> datetime:
    if not isinstance(value, str) or not _CLICKED_AT_RE.match(value):
        raise _err("click export row clicked_at is not a 'YYYY-MM-DD HH:MM:SS' string")
    try:
        naive = datetime.strptime(value, _CLICKED_AT_FMT)
    except ValueError as exc:
        raise _err(
            "click export row clicked_at is not a valid calendar timestamp"
        ) from exc
    return naive.replace(tzinfo=UTC)


def validate_click_export_page(
    payload: object, *, since_id: int, limit: int
) -> tuple[list[ClickRow], int]:
    """export レスポンス 1 ページを厳格に検証し ``(rows, next_since_id)`` を返す。

    ``since_id`` / ``limit`` は要求した **実効値**。契約:
    ``rows`` は ``id`` 昇順で strictly increasing、全 ``id`` > ``since_id``、
    ``count`` == ``len(rows)`` <= ``limit``、``limit`` はエコーバック一致、
    非空なら ``next_since_id`` == 最終 ``id``、空なら ``next_since_id`` == ``since_id``。
    """

    since_id = _require_int(since_id, field="requested since_id")
    limit = _require_int(limit, field="requested limit")

    if not isinstance(payload, dict):
        raise _err("click export response is not a JSON object")
    if payload.get("schema_version") != CLICK_EXPORT_SCHEMA_VERSION:
        raise _err("click export response has an unexpected schema_version")

    resp_limit = _require_int(payload.get("limit"), field="response limit")
    if resp_limit != limit:
        raise _err("click export response echoed a different limit than requested")

    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise _err("click export response rows is not a list")
    if len(raw_rows) > limit:
        raise _err("click export response returned more rows than the requested limit")

    resp_count = _require_int(payload.get("count"), field="response count")
    if resp_count != len(raw_rows):
        raise _err("click export response count does not match the number of rows")

    next_since_id = _require_int(
        payload.get("next_since_id"), field="response next_since_id"
    )

    rows: list[ClickRow] = []
    prev_id = since_id
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise _err("click export row is not an object")
        row_id = _require_int(raw.get("id"), field="row id")
        if row_id <= since_id:
            raise _err(
                "click export row id is not greater than the requested since_id"
            )
        if row_id <= prev_id:
            raise _err(
                "click export rows are not in strictly ascending id order"
            )
        prev_id = row_id
        token = raw.get("token")
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            raise _err("click export row token has an invalid shape")
        rows.append(
            ClickRow(
                source_click_id=row_id,
                token=token,
                clicked_at_utc=_parse_clicked_at(raw.get("clicked_at")),
            )
        )

    if rows:
        if next_since_id != rows[-1].source_click_id:
            raise _err(
                "click export next_since_id does not match the last row id"
            )
    elif next_since_id != since_id:
        raise _err(
            "click export next_since_id must equal since_id for an empty page"
        )

    return rows, next_since_id
