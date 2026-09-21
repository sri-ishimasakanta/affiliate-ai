"""app/models/types.py — ExactDecimal (SQLite でも float を経由しない exact Decimal)。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.models.types import ExactDecimal


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "exact_decimal_rows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    amount: Mapped[Decimal | None] = mapped_column(ExactDecimal(38, 18), nullable=True)


@pytest.fixture
def sqlite_session():
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.mark.parametrize(
    "literal",
    [
        "0.3",
        "0.1",
        "12.123456",  # 4 桁を超える -- 丸められない
        "1.123456789012345678",  # 18 桁小数 (scale 上限)
        "12345678901234567890.123456789012345678",  # 整数部 20 桁 + 小数 18 桁
        "-5.5",
        "100",
    ],
)
def test_round_trip_is_exact_on_sqlite(sqlite_session: Session, literal: str) -> None:
    sqlite_session.add(_Row(amount=Decimal(literal)))
    sqlite_session.commit()
    sqlite_session.expire_all()
    stored = sqlite_session.scalars(select(_Row.amount)).one()
    assert isinstance(stored, Decimal)
    assert stored == Decimal(literal)


def test_stock_numeric_would_be_lossy_on_sqlite_but_exact_decimal_is_not(
    sqlite_session: Session,
) -> None:
    """ExactDecimal を導入した理由の回帰固定: 0.1 + 0.2 は float 経由だと壊れる。"""

    sqlite_session.add_all([_Row(amount=Decimal("0.1")), _Row(amount=Decimal("0.2"))])
    sqlite_session.commit()
    total = sum(sqlite_session.scalars(select(_Row.amount)).all(), Decimal("0"))
    assert total == Decimal("0.3")


def test_none_round_trips(sqlite_session: Session) -> None:
    sqlite_session.add(_Row(amount=None))
    sqlite_session.commit()
    assert sqlite_session.scalars(select(_Row.amount)).one() is None


def test_stored_representation_is_text_not_float(sqlite_session: Session) -> None:
    sqlite_session.add(_Row(amount=Decimal("0.3")))
    sqlite_session.commit()
    raw = sqlite_session.connection().exec_driver_sql(
        "select typeof(amount), amount from exact_decimal_rows"
    ).one()
    assert raw[0] == "text"
    assert raw[1] == "0.3"


@pytest.mark.parametrize("bad", [0.1, 1, True, "0.1"])
def test_non_decimal_bind_rejected(sqlite_session: Session, bad) -> None:
    sqlite_session.add(_Row(amount=bad))
    with pytest.raises(Exception, match="decimal.Decimal"):
        sqlite_session.commit()


@pytest.mark.parametrize("bad", ["NaN", "Infinity"])
def test_non_finite_rejected(sqlite_session: Session, bad: str) -> None:
    sqlite_session.add(_Row(amount=Decimal(bad)))
    with pytest.raises(Exception, match="NaN/Infinity"):
        sqlite_session.commit()


def test_scale_overflow_rejected_not_silently_rounded(sqlite_session: Session) -> None:
    sqlite_session.add(_Row(amount=Decimal("1.1234567890123456789")))  # 19 桁小数
    with pytest.raises(Exception, match="scale"):
        sqlite_session.commit()


def test_precision_overflow_rejected(sqlite_session: Session) -> None:
    sqlite_session.add(_Row(amount=Decimal("1" + "0" * 20)))  # 整数部 21 桁 (上限 20)
    with pytest.raises(Exception, match="precision"):
        sqlite_session.commit()
