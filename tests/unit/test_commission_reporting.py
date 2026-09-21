"""app/affiliate/commission_reporting.py — 月次集計 (pure、Decimal 厳密演算)。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.affiliate.commission_reporting import (
    monthly_commission_approved,
    monthly_commission_earned,
    monthly_commission_paid,
)


@dataclass
class _Fact:
    occurred_at: datetime
    commission_amount: Decimal | None
    payout_approved_at: datetime | None = None
    payout_realized_at: datetime | None = None


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


def test_earned_buckets_by_occurred_at_month() -> None:
    facts = [
        _Fact(occurred_at=_dt(2026, 9, 1), commission_amount=Decimal("10.0")),
        _Fact(occurred_at=_dt(2026, 9, 15), commission_amount=Decimal("5.0")),
        _Fact(occurred_at=_dt(2026, 10, 1), commission_amount=Decimal("2.0")),
    ]
    result = monthly_commission_earned(facts)
    assert result.amounts == {"2026-09": Decimal("15.0"), "2026-10": Decimal("2.0")}
    assert result.unknown_amount_count == 0


def test_earned_excludes_unknown_amount_and_reports_count() -> None:
    facts = [
        _Fact(occurred_at=_dt(2026, 9, 1), commission_amount=Decimal("10.0")),
        _Fact(occurred_at=_dt(2026, 9, 2), commission_amount=None),
    ]
    result = monthly_commission_earned(facts)
    assert result.amounts == {"2026-09": Decimal("10.0")}
    assert result.unknown_amount_count == 1


def test_approved_buckets_by_payout_approved_at_not_occurred_at() -> None:
    facts = [
        _Fact(
            occurred_at=_dt(2026, 9, 1),
            commission_amount=Decimal("10.0"),
            payout_approved_at=_dt(2026, 10, 5),
        ),
        _Fact(
            occurred_at=_dt(2026, 9, 1),
            commission_amount=Decimal("3.0"),
            payout_approved_at=None,
        ),
    ]
    result = monthly_commission_approved(facts)
    assert result.amounts == {"2026-10": Decimal("10.0")}  # not counted in 2026-09


def test_paid_buckets_by_payout_realized_at() -> None:
    facts = [
        _Fact(
            occurred_at=_dt(2026, 9, 1),
            commission_amount=Decimal("7.0"),
            payout_realized_at=_dt(2026, 11, 1),
        ),
    ]
    result = monthly_commission_paid(facts)
    assert result.amounts == {"2026-11": Decimal("7.0")}


def test_empty_input_yields_empty_summary() -> None:
    assert monthly_commission_earned([]).amounts == {}
    assert monthly_commission_approved([]).amounts == {}
    assert monthly_commission_paid([]).amounts == {}


def test_approved_row_with_unknown_amount_counted_separately() -> None:
    facts = [
        _Fact(
            occurred_at=_dt(2026, 9, 1),
            commission_amount=None,
            payout_approved_at=_dt(2026, 9, 5),
        ),
    ]
    result = monthly_commission_approved(facts)
    assert result.amounts == {}
    assert result.unknown_amount_count == 1


# ==================== Phase E1.1: exact decimal arithmetic ===============
def test_earned_sum_is_exact_decimal_not_binary_float() -> None:
    """0.1 + 0.2 != 0.3 in binary float, but Decimal 集計は厳密に一致する。"""

    assert 0.1 + 0.2 != 0.3  # 既知の float 非厳密性そのものを固定する

    facts = [
        _Fact(occurred_at=_dt(2026, 9, 1), commission_amount=Decimal("0.1")),
        _Fact(occurred_at=_dt(2026, 9, 2), commission_amount=Decimal("0.2")),
    ]
    result = monthly_commission_earned(facts)
    assert result.amounts["2026-09"] == Decimal("0.3")
    assert isinstance(result.amounts["2026-09"], Decimal)


def test_approved_and_paid_aggregation_with_decimal_fractions() -> None:
    facts = [
        _Fact(
            occurred_at=_dt(2026, 9, 1),
            commission_amount=Decimal("10.10"),
            payout_approved_at=_dt(2026, 9, 20),
            payout_realized_at=_dt(2026, 10, 5),
        ),
        _Fact(
            occurred_at=_dt(2026, 9, 1),
            commission_amount=Decimal("5.05"),
            payout_approved_at=_dt(2026, 9, 25),
            payout_realized_at=_dt(2026, 10, 5),
        ),
        _Fact(
            occurred_at=_dt(2026, 9, 1),
            commission_amount=Decimal("0.001"),
            payout_approved_at=_dt(2026, 9, 25),
            payout_realized_at=_dt(2026, 10, 5),
        ),
    ]
    approved = monthly_commission_approved(facts)
    paid = monthly_commission_paid(facts)
    assert approved.amounts["2026-09"] == Decimal("15.151")
    assert paid.amounts["2026-10"] == Decimal("15.151")
