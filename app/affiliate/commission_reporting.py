"""AffiliateCommissionFact からの月次集計 (pure)。DB / network 非依存。

用語を明確に分ける (Phase E1 §10):

- ``commission_earned``   : Make が記録した commission の合計 (``occurred_at`` の月
  でバケット化)。まだ承認も支払いもされていない可能性がある「記録された事実」。
- ``commission_approved`` : ``payout_approved_at`` が確定している行の合計
  (``payout_approved_at`` の月でバケット化)。**推奨する business KPI
  ("approved affiliate revenue")** -- 承認は Make 側の明示的な payout タイムスタンプ
  フィールドから判定し、``status`` 文字列の想定値を独自に決め打ちしない
  (Make の正確な status 語彙は公式ドキュメントで未確認のため)。
- ``commission_paid``     : ``payout_realized_at`` が確定している行の合計
  (``payout_realized_at`` の月でバケット化)。実際の現金化。

``commission_available`` (承認/支払いされていない残高) はここでは計算しない --
Make の ``GET /affiliate/commission-info`` が ``availablePayout`` を直接返すため、
最低支払額のしきい値などの Make 側のビジネスルールをこちらで独自に再現しようと
すると誤りうる。ローカルで独自に "available" を導出するくらいなら、Make の
集計値をそのまま使うほうが安全 -- このフェーズではその配線をまだ行わない
(README/report で明示的に述べるに留める)。

金額が ``None`` の行は合計から除外し、``*_unknown_amount_count`` で件数を
別途報告する (無言で 0 として合算しない)。raw click (redirect) は一切参照しない
-- この module は import 済みの :class:`AffiliateCommissionFact` のみを扱う。

金額は実金額のため ``decimal.Decimal`` のまま合算する (Phase E1.1) -- ``float``
への変換は一切行わない (``float(Decimal(...))`` の丸め誤差混入を避ける)。
``AffiliateCommissionFact.commission_amount`` は既に ``ExactDecimal(38, 18)``
(Phase E1.2 -- 根拠のない内部規約による丸めをしない、wide/exact な精度) 由来の
``Decimal`` なので、ここでの追加の quantize は不要。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class MonthlyAmountSummary:
    """1 指標の月次集計。``amounts`` は ``"YYYY-MM"`` -> 合計 (existing rows のみ)。"""

    amounts: dict[str, Decimal]
    unknown_amount_count: int


def _month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _bucket(
    facts: Iterable[object],
    *,
    date_attr: str,
    filter_fn,
) -> MonthlyAmountSummary:
    amounts: dict[str, Decimal] = {}
    unknown_amount_count = 0
    for fact in facts:
        if not filter_fn(fact):
            continue
        bucket_dt = getattr(fact, date_attr)
        if bucket_dt is None:
            continue
        amount = fact.commission_amount
        if amount is None:
            unknown_amount_count += 1
            continue
        key = _month_key(bucket_dt)
        amounts[key] = amounts.get(key, Decimal("0")) + amount
    return MonthlyAmountSummary(amounts=amounts, unknown_amount_count=unknown_amount_count)


def monthly_commission_earned(facts: Iterable[object]) -> MonthlyAmountSummary:
    """全 commission event を ``occurred_at`` の月でバケット化した合計
    (承認/支払い状態を問わない)。"""

    return _bucket(facts, date_attr="occurred_at", filter_fn=lambda _f: True)


def monthly_commission_approved(facts: Iterable[object]) -> MonthlyAmountSummary:
    """``payout_approved_at`` が確定している行を、その月でバケット化した合計。
    推奨する "approved affiliate revenue" business KPI。"""

    return _bucket(
        facts,
        date_attr="payout_approved_at",
        filter_fn=lambda f: f.payout_approved_at is not None,
    )


def monthly_commission_paid(facts: Iterable[object]) -> MonthlyAmountSummary:
    """``payout_realized_at`` が確定している行を、その月でバケット化した合計。
    実際に現金化された金額。"""

    return _bucket(
        facts,
        date_attr="payout_realized_at",
        filter_fn=lambda f: f.payout_realized_at is not None,
    )
