"""共通の SQLAlchemy カスタム型。

``ExactDecimal`` -- 実金額用の exact な Decimal 列 (Phase E1.2)。

背景: SQLite は Decimal をネイティブに保持できない。素の ``sqlalchemy.Numeric`` を
SQLite で使うと NUMERIC affinity + float 経由の変換になり、scale が大きいと値が
非可逆に壊れる (実測: ``Decimal("0.3")`` -> ``0.299999999999999989``。scale 4 では
"%.4f" 整形が誤差を隠していただけ)。本プロジェクトの本番 DB は SQLite なので、
「wide exact」を名乗るには型自体で保証する必要がある。

- SQLite: 10 進文字列を **TEXT** (VARCHAR = TEXT affinity) で保存し、読み戻しで
  ``Decimal(str)`` に戻す -- 一切 float を経由しない。SQL 側での数値集計/大小比較は
  できない (文字列比較になる) が、この列は Python 側 (Decimal) で集計する前提
  (:mod:`app.affiliate.commission_reporting`)。
- それ以外 (PostgreSQL 等、ネイティブ DECIMAL を持つ dialect): 通常の
  ``Numeric(precision, scale)``。

precision/scale を超える値は DB へ黙って丸めさせず、bind 時点で ``ValueError`` にする
(fail closed)。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine


class ExactDecimal(TypeDecorator):
    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int, scale: int) -> None:
        super().__init__(precision=precision, scale=scale, asdecimal=True)
        self.precision = precision
        self.scale = scale

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine:
        if dialect.name == "sqlite":
            # 符号 + 小数点 + precision 桁に十分な長さ。
            return dialect.type_descriptor(String(self.precision + 3))
        return dialect.type_descriptor(Numeric(self.precision, self.scale, asdecimal=True))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, Decimal):
            raise TypeError("ExactDecimal only accepts decimal.Decimal values (no float/int)")
        if not value.is_finite():
            raise ValueError("ExactDecimal does not accept NaN/Infinity")
        _sign, digits, exponent = value.as_tuple()
        decimal_places = max(0, -int(exponent))
        if decimal_places > self.scale:
            raise ValueError(f"value exceeds the column scale ({self.scale} decimal places)")
        integer_digits = max(0, len(digits) + int(exponent))
        if integer_digits > self.precision - self.scale:
            raise ValueError(
                f"value exceeds the column precision ({self.precision - self.scale} integer digits)"
            )
        if dialect.name == "sqlite":
            return format(value, "f")
        return value

    def process_result_value(self, value, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
