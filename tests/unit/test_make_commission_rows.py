"""app/affiliate/make_commission_rows.py — 厳格な shape 検証 (pure)。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.affiliate.make_commission_rows import (
    validate_make_commission_row,
    validate_make_commissions_page,
)
from app.exceptions import AffiliateCommissionImportError


def _row(**over) -> dict:
    base = {
        "id": 101,
        "organization_id": 55,
        "type": "commission",
        "status": "requested",
        "commission": 12.5,
        "source": "referral",
        "created": "2026-09-01T12:00:00+00:00",
        "payout_requested": None,
        "payout_approved": None,
        "payout_realized": None,
    }
    base.update(over)
    return base


# ==================== single row ======================================
def test_valid_row_parses_all_fields() -> None:
    row = validate_make_commission_row(_row())
    assert row.source_commission_id == "101"
    assert row.source_organization_id == "55"
    assert row.event_type == "commission"
    assert row.provider_status == "requested"
    assert isinstance(row.commission_amount, Decimal)
    assert row.commission_amount == Decimal("12.5")
    assert row.source == "referral"
    assert row.occurred_at == datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    assert row.payout_requested_at is None
    assert row.payout_approved_at is None
    assert row.payout_realized_at is None


def test_id_accepts_string_and_normalizes() -> None:
    row = validate_make_commission_row(_row(id="abc-123"))
    assert row.source_commission_id == "abc-123"


def test_organization_id_nullable() -> None:
    row = validate_make_commission_row(_row(organization_id=None))
    assert row.source_organization_id is None


def test_commission_amount_nullable() -> None:
    row = validate_make_commission_row(_row(commission=None))
    assert row.commission_amount is None


def test_source_nullable() -> None:
    row = validate_make_commission_row(_row(source=None))
    assert row.source is None


def test_payout_timestamps_parsed_when_present() -> None:
    row = validate_make_commission_row(
        _row(
            payout_requested="2026-09-02T00:00:00Z",
            payout_approved="2026-09-03T00:00:00+00:00",
            payout_realized="2026-09-04T00:00:00+09:00",
        )
    )
    assert row.payout_requested_at == datetime(2026, 9, 2, tzinfo=UTC)
    assert row.payout_approved_at == datetime(2026, 9, 3, tzinfo=UTC)
    # +09:00 -> UTC 変換
    assert row.payout_realized_at == datetime(2026, 9, 3, 15, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("field", ["id"])
def test_missing_required_id_rejected(field: str) -> None:
    bad = _row()
    del bad[field]
    with pytest.raises(AffiliateCommissionImportError, match="id"):
        validate_make_commission_row(bad)


def test_id_wrong_type_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="id"):
        validate_make_commission_row(_row(id=None))


def test_id_bool_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="id"):
        validate_make_commission_row(_row(id=True))


def test_type_missing_or_empty_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="type"):
        validate_make_commission_row(_row(type=""))
    with pytest.raises(AffiliateCommissionImportError, match="type"):
        validate_make_commission_row(_row(type=None))


def test_status_missing_or_empty_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="status"):
        validate_make_commission_row(_row(status=""))


def test_commission_amount_bool_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="commission"):
        validate_make_commission_row(_row(commission=True))


# ==================== Phase E1.1/E1.2: exact decimal amount handling =====
def test_commission_amount_accepts_int() -> None:
    row = validate_make_commission_row(_row(commission=12))
    assert row.commission_amount == Decimal("12")


def test_commission_amount_accepts_decimal_directly() -> None:
    """通常経路: MakeAffiliateClient が json.loads(parse_float=Decimal) 済みの
    Decimal をそのまま渡してくる。"""

    row = validate_make_commission_row(_row(commission=Decimal("12.50")))
    assert row.commission_amount == Decimal("12.50")


def test_commission_amount_accepts_numeric_string() -> None:
    """provider が文字列で金額を返す可能性への防御。"""

    row = validate_make_commission_row(_row(commission="12.50"))
    assert row.commission_amount == Decimal("12.50")


def test_commission_amount_invalid_string_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="commission"):
        validate_make_commission_row(_row(commission="twelve"))
    with pytest.raises(AffiliateCommissionImportError, match="commission"):
        validate_make_commission_row(_row(commission=""))


def test_commission_amount_nan_and_infinity_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="finite"):
        validate_make_commission_row(_row(commission="NaN"))
    with pytest.raises(AffiliateCommissionImportError, match="finite"):
        validate_make_commission_row(_row(commission="Infinity"))
    with pytest.raises(AffiliateCommissionImportError, match="finite"):
        validate_make_commission_row(_row(commission=Decimal("NaN")))


def test_commission_amount_float_fallback_uses_string_conversion_not_binary() -> None:
    """素の float が来た場合の防御的 fallback は Decimal(str(value)) 経由であり、
    Decimal(float) の直接変換 (binary round-trip 誤差混入) ではない。"""

    row = validate_make_commission_row(_row(commission=0.1))
    assert row.commission_amount == Decimal("0.1")
    # Decimal(0.1) (直接変換) だったなら 0.1000000000000000055511151231257827021181583404541015625
    # になる -- ここでは str() 経由の安全な変換だけが使われていることを、
    # 結果の厳密一致 (余分な桁が一切無いこと) で保証する。
    assert str(row.commission_amount) == "0.1"


def test_commission_amount_exact_decimal_arithmetic_not_binary_float() -> None:
    """0.1 + 0.2 は binary float では 0.3 に厳密には一致しない
    (0.30000000000000004) -- Decimal 経路でのみ厳密に一致することを証明する。"""

    assert 0.1 + 0.2 != 0.3  # float の既知の非厳密性 (この事実そのものを固定する)

    row_a = validate_make_commission_row(_row(commission=Decimal("0.1")))
    row_b = validate_make_commission_row(_row(commission=Decimal("0.2")))
    assert row_a.commission_amount + row_b.commission_amount == Decimal("0.3")


# ==================== Phase E1.2: no forced rounding ======================
def test_commission_amount_beyond_four_decimal_places_not_rounded() -> None:
    """E1.1 は Numeric(14,4) に合わせて 4 桁小数へ強制的に丸めていたが、Make の
    公式ドキュメントはその scale を裏付けない。E1.2 では provider の値を
    exact のまま (丸めずに) 保持する。"""

    row = validate_make_commission_row(_row(commission="12.123456"))
    assert row.commission_amount == Decimal("12.123456")
    assert str(row.commission_amount) == "12.123456"


def test_commission_amount_up_to_eighteen_decimal_places_preserved_exactly() -> None:
    value = "1.123456789012345678"  # 18 桁小数 (Numeric(38, 18) の scale 上限)
    row = validate_make_commission_row(_row(commission=value))
    assert str(row.commission_amount) == value


def test_commission_amount_exceeding_max_decimal_places_rejected() -> None:
    """DB の Numeric(38, 18) scale を超える小数桁数は、黙って切り詰めずに
    fail closed で拒否する (捏造しない)。"""

    value = "1.1234567890123456789"  # 19 桁小数 (scale 18 を 1 桁超える)
    with pytest.raises(AffiliateCommissionImportError, match="decimal places"):
        validate_make_commission_row(_row(commission=value))


def test_created_missing_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="created"):
        validate_make_commission_row(_row(created=None))


def test_created_malformed_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="created"):
        validate_make_commission_row(_row(created="not-a-date"))


def test_payout_timestamp_boolean_shape_rejected() -> None:
    """payout_* が boolean で来た場合は fabricate せず shape エラーで拒否する。"""

    with pytest.raises(AffiliateCommissionImportError, match="payout_requested"):
        validate_make_commission_row(_row(payout_requested=True))


def test_payout_timestamp_malformed_string_rejected() -> None:
    """公式レスポンス例で確認済みの契約 (NULL か ISO-8601 文字列) に反する
    malformed な文字列は拒否する (Phase E1.2 §3)。"""

    with pytest.raises(AffiliateCommissionImportError, match="payout_approved"):
        validate_make_commission_row(_row(payout_approved="not-a-timestamp"))
    with pytest.raises(AffiliateCommissionImportError, match="payout_realized"):
        validate_make_commission_row(_row(payout_realized="2026-13-40T00:00:00Z"))


def test_payout_timestamp_valid_iso_variants_accepted() -> None:
    """公式レスポンス例で確認済みの契約: NULL か ISO-8601 タイムスタンプ文字列。"""

    row = validate_make_commission_row(
        _row(
            payout_requested="2026-09-02T00:00:00+00:00",
            payout_approved="2026-09-03T00:00:00Z",
            payout_realized=None,
        )
    )
    assert row.payout_requested_at == datetime(2026, 9, 2, tzinfo=UTC)
    assert row.payout_approved_at == datetime(2026, 9, 3, tzinfo=UTC)
    assert row.payout_realized_at is None


def test_row_not_object_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="object"):
        validate_make_commission_row(["not", "a", "dict"])


# ==================== page: {"commissions": [...]} envelope (Phase E1.2) ==
def test_object_envelope_with_commissions_array_accepted() -> None:
    payload = {"commissions": [_row(id=1), _row(id=2)]}
    rows, has_more = validate_make_commissions_page(payload, requested_limit=10)
    assert [r.source_commission_id for r in rows] == ["1", "2"]
    assert has_more is False


def test_bare_array_rejected() -> None:
    """公式契約は object envelope -- bare array は裏付けのない後方互換として拒否する。"""

    payload = [_row(id=1), _row(id=2)]
    with pytest.raises(AffiliateCommissionImportError, match="object"):
        validate_make_commissions_page(payload, requested_limit=10)


def test_missing_commissions_key_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="commissions"):
        validate_make_commissions_page({"other": []}, requested_limit=10)


def test_commissions_value_not_array_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="commissions"):
        validate_make_commissions_page({"commissions": "not-a-list"}, requested_limit=10)
    with pytest.raises(AffiliateCommissionImportError, match="commissions"):
        validate_make_commissions_page({"commissions": {}}, requested_limit=10)


# ==================== page (pagination termination -- still inferred) =====
def test_page_short_of_limit_has_no_more() -> None:
    payload = {"commissions": [_row(id=1), _row(id=2)]}
    rows, has_more = validate_make_commissions_page(payload, requested_limit=10)
    assert len(rows) == 2
    assert has_more is False


def test_page_exactly_at_limit_has_more() -> None:
    payload = {"commissions": [_row(id=i) for i in range(1, 6)]}
    rows, has_more = validate_make_commissions_page(payload, requested_limit=5)
    assert len(rows) == 5
    assert has_more is True


def test_empty_page_has_no_more() -> None:
    rows, has_more = validate_make_commissions_page({"commissions": []}, requested_limit=10)
    assert rows == []
    assert has_more is False


def test_page_exceeding_limit_rejected() -> None:
    payload = {"commissions": [_row(id=i) for i in range(1, 4)]}
    with pytest.raises(AffiliateCommissionImportError, match="more rows"):
        validate_make_commissions_page(payload, requested_limit=2)
