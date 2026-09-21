"""AffiliateCommissionFact — ASP (現状 Make のみ) が報告する commission/conversion
event の **current fact row** (provider-faithful, UPSERT-on-reimport)。

設計判断 (Phase E1 §8 の "current fact row + transition history" か
"append-only snapshots" のどちらかを選び明記する、という要求への回答):

**"current fact row" を選ぶ。** 理由:

1. §3 の一意性契約 ("provider を跨いで numeric source id が再利用できるよう、
   (provider, source_commission_id) で一意") は、1 commission につき 1 行だけを
   前提にしている -- 複数 snapshot 行を許すなら、この一意性契約と直接矛盾する。
2. 既存の類似パターン (:class:`~app.models.search_console_page_daily.
   SearchConsolePageDaily`) も同じ理由 (provider 側で後から値が確定/変化する)
   で **UPSERT** を採用しており、別の履歴テーブルは持たない。「既存パターンを
   優先する」という指示に従う。

「historical evidence を破壊しない」という要求は、以下の 3 点で満たす:

- identity フィールド (``provider`` / ``source_commission_id`` /
  ``source_organization_id`` / ``event_type`` / ``occurred_at``) は作成後 immutable
  -- 再取り込みで書き換えない。
- ``first_seen_at`` (作成時刻、不変) と ``last_seen_at`` (直近で確認できた
  import 時刻、更新) の両方を持つ -- 「いつ初めて観測したか」と「直近いつ
  確認が取れたか」を失わない。
- Make 側の provider_status / payout タイムスタンプは **常に Make の最新報告を
  正とする** (こちらの transition ルールで拒否しない) -- commission はこちらが
  所有する内部ワークフローではなく、Make が authoritative な外部事実であり、
  chargeback/reversal のような「後退して見える」遷移も Make 発の正当な事実で
  ありうるため。field 単位の完全な diff 履歴 (いつ status が何から何に変わったか)
  は本フェーズのスコープ外 -- 必要になれば専用の履歴テーブルを別フェーズで追加する
  (このモデルに後付けで混ぜない)。粗い監査証跡は
  :class:`~app.models.affiliate_commission_import_run.AffiliateCommissionImportRun`
  の ``response_snapshot`` (run ごとの安全な要約) で保つ。

金額/通貨 (Phase E1.1、精度は Phase E1.2 で訂正):

- ``commission_amount`` は実金額のため binary float を使わない --
  ``ExactDecimal(38, 18)`` (Python 側は ``decimal.Decimal``)。
  :class:`app.models.types.ExactDecimal` は、ネイティブ DECIMAL を持つ dialect では
  ``Numeric(38, 18)``、SQLite (本番 DB) では **TEXT で厳密保存** する型。素の
  ``Numeric`` は SQLite で float を経由し非可逆になる (実測: ``0.3`` ->
  ``0.299999999999999989``) ため、「wide exact」を型で保証するために導入した。

  Phase E1.1 では ``Numeric(14, 4)`` を採用し、
  ``app/article/draft_input_canonical.py`` の ``_COMMISSION_QUANT =
  Decimal("0.0001")`` (別モデル・別用途の commission 表現規約) に合わせて
  4 桁小数へ丸めていたが、Make commissions API の公式ドキュメントは
  ``commission`` を単に JSON "number" とだけ述べており、4 桁小数という scale
  を一切裏付けない。無関係な内部規約を根拠に provider の実際の値を丸めるのは
  誤りだったため、Phase E1.2 で **wide/exact** な ``ExactDecimal(38, 18)`` へ訂正
  した -- scale 18 桁は現実的に想定される小数精度を十分に超え、precision 38
  桁 (整数部最大 20 桁) はどのアフィリエイト報酬額も安全に収まる。値は
  :mod:`app.affiliate.make_commission_rows` で provider の JSON から
  ``Decimal(str(...))`` 経由の非精度損失な変換で作られ、**丸めずに** exact な
  ままここに渡ってくる (scale 18 桁を超える小数桁数だけは、黙って切り詰めず
  import 時点で fail closed する -- 同 module 参照)。
- ``currency`` は Make commissions API のレスポンスに通貨フィールドが無い場合、
  **絶対に補完しない** (JPY/USD を仮定しない) -- ``NULL`` のまま残す。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import ExactDecimal

#: commission_amount の DB 精度 (Phase E1.2: wide/exact -- 整数部最大 20 桁 +
#: 小数部 18 桁)。provider の実際の値を、根拠のない内部規約で丸めないための
#: 余裕を持った精度 (model docstring 参照)。
COMMISSION_AMOUNT_PRECISION = 38
COMMISSION_AMOUNT_SCALE = 18

# 作成後 immutable な identity フィールド。provider 側の状態遷移で変わりうる
# フィールド (provider_status / commission_amount / currency / source /
# payout_*_at) はここに含めない -- 再取り込みで正当に更新される。
FROZEN_FIELDS = (
    "affiliate_program_id",
    "provider",
    "source_commission_id",
    "source_organization_id",
    "event_type",
    "occurred_at",
)


class AffiliateCommissionFact(Base):
    __tablename__ = "affiliate_commission_facts"

    __table_args__ = (
        # provider を跨いで同じ numeric source id を再利用できるよう複合一意性。
        UniqueConstraint(
            "provider",
            "source_commission_id",
            name="uq_affiliate_commission_facts_provider_source_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    affiliate_program_id: Mapped[int] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # "make" (将来の他 ASP に備えた namespace)。
    provider: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    # provider 側の commission id (Make commissions API の "id")。将来の provider が
    # 数値 id を再利用しても (provider, source_commission_id) の複合一意性で衝突しない
    # よう文字列で保持する (provider ごとに数値/文字列 id が混在しうるため)。
    source_commission_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Make commissions API の "organization_id"。ローカルの redirect token は Make の
    # organization を特定する join key を一切提供しないため、この値を独自に推測しない
    # -- provider が明示的に返した値をそのまま保持するだけ。
    source_organization_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    # Make commissions API の "type" (raw, opaque -- 独自の enum を発明しない)。
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)

    # Make commissions API の "status" (raw, opaque)。独自の transition validation は
    # 課さない -- provider が authoritative。
    provider_status: Mapped[str] = mapped_column(String(100), nullable=False)

    # Make commissions API の "commission"。値が無い commission event もありうる。
    # 実金額なので binary float は使わない (Phase E1.1)。
    commission_amount: Mapped[Decimal | None] = mapped_column(
        ExactDecimal(COMMISSION_AMOUNT_PRECISION, COMMISSION_AMOUNT_SCALE), nullable=True
    )
    # Make commissions API のレスポンスに通貨フィールドが無いため常に NULL
    # (fabricate しない)。将来 provider がレスポンスに通貨を含めるようになれば
    # importer 側がここを populate する -- スキーマは既に対応済み。
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    # Make commissions API の "source" (raw, opaque)。
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Make commissions API の "created" -- この commission event が発生した時刻。
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Make commissions API の "payout_requested" / "payout_approved" /
    # "payout_realized"。値が無い間は NULL のまま (そのフェーズにまだ到達していない)。
    payout_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payout_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payout_realized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # この commission を最初に観測した時刻 (不変)。
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 直近で確認 (取り込み) できた時刻 (再取り込みのたびに更新)。
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # 現在この行の値を占めている **最新** run (SearchConsolePageDaily と同じ規約 --
    # 再取り込みのたびに更新する。「最初に取り込んだ run」ではない)。
    source_import_run_id: Mapped[int] = mapped_column(
        ForeignKey("affiliate_commission_import_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # 将来 provider 固有の追加コンテキストを人間が読める形で残す余地 (現状未使用)。
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
