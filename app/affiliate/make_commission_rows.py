"""Make ``GET {base}/affiliate/commissions`` レスポンス 1 ページの typed 表現と
厳格な検証 (pure)。DB / network 非依存。

レスポンス envelope (Phase E1.2、公式ドキュメントで確認済み -- もう推論ではない):
公式ドキュメントは ``{"commissions": [...]}`` という object envelope を明示する。
bare JSON array はこの公式契約ではないため **拒否する** (裏付けのない後方互換
許容はしない、という明示指示に従う)。``pg[offset]`` / ``pg[limit]`` /
``pg[sortBy]`` / ``pg[sortDir]`` は引き続き query パラメータとして送るが、
公式ドキュメントはこのエンドポイントの総件数/次ページ有無フィールドまでは
明記していない。よって **ページ終端判定のみ** が引き続き推論のままである:
受領件数が要求 limit 未満なら最終ページ、という標準的な offset pagination
慣行を採用する (本リポジトリの WordPress click export
(``app/affiliate/click_import_rows.py`` の ``ClickExportPage.has_more``) と
同じ慣行)。この推論が実際の Make レスポンスと異なれば、strict validation が
明確な shape エラーで失敗する (黙って誤解釈しない)。

``payout_requested`` / ``payout_approved`` / ``payout_realized`` は公式の
レスポンス例で「NULL、または ISO-8601 タイムスタンプ文字列」であることが
確認済み (Phase E1.2 -- もう推論ではない)。それ以外の型 (例えば boolean) が
来た場合は shape エラーで拒否する (捏造しない)。

金額 (``commission``) は実金額のため binary float の round-trip を経由しない
(Phase E1.1)。int / Decimal (通常経路。:class:`~app.affiliate.make_client.
MakeAffiliateClient` が ``json.loads(..., parse_float=Decimal)`` で JSON の
lexical 表現から直接 Decimal 化する) / 数値文字列 (provider が文字列で送る
可能性への防御) / NULL を許可する。素の ``float`` が渡ってきた場合の防御的
fallback でも ``Decimal(str(value))`` 経由の安全な変換のみを使い、
``Decimal(float_value)`` の直接変換 (binary 表現由来の丸め誤差混入) は行わない
-- ``app/article/draft_input_canonical.py`` の ``canonical_commission()`` と
同じ規約。NaN / Infinity / 不正な文字列は拒否する (捏造しない)。

精度 (Phase E1.2 -- 訂正): 公式ドキュメントは commission を単に JSON
"number" とだけ述べ、4 桁小数への丸めを一切裏付けない。E1.1 で行っていた
``quantize(Decimal("0.0001"))`` への強制丸めは **削除した** -- provider が
返した exact な Decimal 値をそのまま (丸めずに) 返す。
:class:`~app.models.affiliate_commission_fact.AffiliateCommissionFact.
commission_amount` の ``ExactDecimal(38, 18)`` (wide, exact) scale を超える
小数桁数だけは、値を黙って切り詰めるのではなく明確な shape エラーで拒否する
(fail closed)。通貨フィールドは公式契約に存在しないため、このレスポンスからは
一切読み取らない (:mod:`app.services.affiliate_commission_import_service` 側
でも補完しない)。

エラー文言には raw body / API token 値を一切含めない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.exceptions import AffiliateCommissionImportError

MAKE_PROVIDER = "make"

# app/models/affiliate_commission_fact.py の ExactDecimal(38, 18) と一致させる
# (import はしない -- この module は DB 非依存の pure module のまま保つ)。
# 実際の丸めは一切行わない -- 超過した場合は fail closed で拒否するためだけに使う。
_COMMISSION_AMOUNT_MAX_DECIMAL_PLACES = 18


def _err(reason: str) -> AffiliateCommissionImportError:
    return AffiliateCommissionImportError(reason, http_status=200)


@dataclass(frozen=True)
class MakeCommissionRow:
    """検証済みの Make commission 1 行。``source_commission_id`` /
    ``source_organization_id`` は provider がどちらの型 (int/str) で返しても
    文字列に正規化する (provider を跨いだ ID 表現の一貫性のため)。"""

    source_commission_id: str
    source_organization_id: str | None
    event_type: str
    provider_status: str
    commission_amount: Decimal | None
    source: str | None
    occurred_at: datetime
    payout_requested_at: datetime | None
    payout_approved_at: datetime | None
    payout_realized_at: datetime | None


def _require_id_like(value: object, *, field: str) -> str:
    """``id`` / ``organization_id`` 相当: int か非空 str のみ許可し、str に正規化する。"""

    if isinstance(value, bool):
        raise _err(f"commission row {field} is not an integer or string")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value != "":
        return value
    raise _err(f"commission row {field} is not an integer or non-empty string")


def _require_non_empty_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise _err(f"commission row {field} is not a non-empty string")
    return value


def _optional_str(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise _err(f"commission row {field} is not a string or null")
    return value


def _optional_amount(value: object, *, field: str) -> Decimal | None:
    """実金額を ``Decimal`` で返す。``Decimal(float_value)`` の直接変換
    (binary round-trip 誤差混入) は一切行わない -- module docstring 参照。"""

    if value is None:
        return None
    if isinstance(value, bool):
        raise _err(f"commission row {field} is not numeric, a decimal string, or null")

    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        # 防御的 fallback のみ (通常経路は client 側の json.loads(parse_float=Decimal)
        # により float はここに到達しない) -- str() 経由でのみ Decimal 化する。
        amount = Decimal(str(value))
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except InvalidOperation as exc:
            raise _err(
                f"commission row {field} is not a valid decimal string"
            ) from exc
    else:
        raise _err(f"commission row {field} is not numeric, a decimal string, or null")

    if not amount.is_finite():
        raise _err(f"commission row {field} must be a finite value")

    # 丸めない -- exact な parsed 値をそのまま返す (Phase E1.2)。DB の
    # ExactDecimal(38, 18) scale を超える小数桁数だけは、黙って切り詰めず fail
    # closed で拒否する (exponent は amount.is_finite() 済みなので常に int)。
    _sign, _digits, exponent = amount.as_tuple()
    decimal_places = max(0, -exponent)
    if decimal_places > _COMMISSION_AMOUNT_MAX_DECIMAL_PLACES:
        raise _err(
            f"commission row {field} has more than "
            f"{_COMMISSION_AMOUNT_MAX_DECIMAL_PLACES} decimal places, which "
            "exceeds the supported storage scale"
        )
    return amount


def _require_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or value == "":
        raise _err(f"commission row {field} is not a non-empty timestamp string")
    return _parse_make_timestamp(value, field=field)


def _optional_timestamp(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise _err(f"commission row {field} is not a timestamp string or null")
    return _parse_make_timestamp(value, field=field)


def _parse_make_timestamp(value: str, *, field: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise _err(f"commission row {field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return parsed


def validate_make_commission_row(raw: object) -> MakeCommissionRow:
    if not isinstance(raw, dict):
        raise _err("commission row is not an object")
    return MakeCommissionRow(
        source_commission_id=_require_id_like(raw.get("id"), field="id"),
        source_organization_id=(
            None
            if raw.get("organization_id") is None
            else _require_id_like(raw.get("organization_id"), field="organization_id")
        ),
        event_type=_require_non_empty_str(raw.get("type"), field="type"),
        provider_status=_require_non_empty_str(raw.get("status"), field="status"),
        commission_amount=_optional_amount(raw.get("commission"), field="commission"),
        source=_optional_str(raw.get("source"), field="source"),
        occurred_at=_require_timestamp(raw.get("created"), field="created"),
        payout_requested_at=_optional_timestamp(
            raw.get("payout_requested"), field="payout_requested"
        ),
        payout_approved_at=_optional_timestamp(
            raw.get("payout_approved"), field="payout_approved"
        ),
        payout_realized_at=_optional_timestamp(
            raw.get("payout_realized"), field="payout_realized"
        ),
    )


def validate_make_commissions_page(
    payload: object, *, requested_limit: int
) -> tuple[list[MakeCommissionRow], bool]:
    """1 ページ分の commissions レスポンスを厳格に検証する。

    公式契約の object envelope ``{"commissions": [...]}`` を要求する
    (Phase E1.2 -- bare array は拒否する。裏付けのない後方互換は許容しない)。

    live 検証 (Phase E1.3/E1.4): 該当行なしの **成功** レスポンスは
    ``{"commissions": null, "pg": {...}}`` である。``"commissions": null`` は
    空リスト (0 件) として扱う。``"commissions"`` キー自体の欠落、および
    null でも配列でもない値 (文字列 / object 等) は引き続き拒否する。

    ``requested_limit`` は要求した ``pg[limit]`` の実効値。返り値は
    ``(rows, has_more)`` -- ``has_more`` は「受領件数が要求 limit と一致したか」
    という、引き続き推論のままの標準的な offset pagination 終端判定。live の
    ``pg`` は request の echo (limit/offset/returnTotalCount/sortBy/sortDir) で
    total/has-more を含まないため、終端判定には使わない。null / 空は
    ``has_more=False`` (= ページネーション終了)。
    """

    if not isinstance(payload, dict):
        raise _err("commissions response is not a JSON object")
    if "commissions" not in payload:
        raise _err("commissions response is missing the 'commissions' key")
    raw_rows = payload["commissions"]
    if raw_rows is None:
        raw_rows = []
    elif not isinstance(raw_rows, list):
        raise _err("commissions response 'commissions' is not an array or null")
    if len(raw_rows) > requested_limit:
        raise _err("commissions response returned more rows than the requested limit")

    rows = [validate_make_commission_row(raw) for raw in raw_rows]
    has_more = len(rows) == requested_limit and requested_limit > 0
    return rows, has_more
