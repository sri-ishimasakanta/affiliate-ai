"""AffiliateCommissionImportRun の実行オーケストレーション (transaction owner)。

Make ``GET {base}/affiliate/commissions`` -> :class:`AffiliateCommissionFact`
(current fact row, UPSERT -- 設計理由はモデル docstring 参照) +
:class:`AffiliateCommissionImportRun` provenance。

流れ (:class:`~app.services.affiliate_click_import_service.AffiliateClickImportService`
と同じ設計方針):

  1. 設定確認 (base URL + token)。未設定なら run 行を作らず fail closed。
  2. date range 検証 (Phase E1.4): ``date_from`` と ``date_to`` は **両方必須**
     (live で、どちらか欠けると Make API が HTTP 400 を返すことを確認済み)。
     補完/デフォルト化はせず、HTTP も run 行も作る前に fail closed する。
     ``date_from <= date_to`` も検証する。
  3. running run を作成し **commit** (ネットワーク前に durable 化)。
  4. 完全なページネーション (``pg[offset]`` を進めながら) を
     ``MAKE_COMMISSIONS_MAX_PAGES`` 上限内で実行。各ページは
     :class:`MakeAffiliateClient` が厳格検証済み。
  5. 単一 import transaction: 全ページの行をまとめて
     (provider, source_commission_id) で既存行と突き合わせ、insert / update /
     unchanged を判定し、mark succeeded、**1 度だけ commit**。

run 作成後に失敗したら: rollback -> 別 transaction で run を failed 化して commit ->
re-raise。**部分ページ取り込みはしない。**

API token / Authorization ヘッダ値 / raw response body は print / log / 例外文言 /
run storage に一切残さない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
from sqlalchemy.orm import Session

from app.affiliate.make_client import MAKE_COMMISSIONS_MAX_PAGES, MakeAffiliateClient
from app.affiliate.make_commission_rows import MAKE_PROVIDER, MakeCommissionRow
from app.article.fact_freshness import to_storage_utc
from app.config.settings import get_settings
from app.exceptions import AffiliateCommissionImportError, EntityNotFoundError
from app.models import AffiliateProgram
from app.models.affiliate_commission_import_run import ACIR_RUNNING
from app.repositories.affiliate_commission_fact_repository import (
    AffiliateCommissionFactRepository,
)
from app.repositories.affiliate_commission_import_run_repository import (
    AffiliateCommissionImportRunRepository,
)

_DEFAULT_PAGE_LIMIT = 100


@dataclass(frozen=True)
class CommissionImportPlan:
    """``--execute`` なしの読み取り専用プレビュー (run を作らない、network 0 回)。"""

    provider: str
    affiliate_program_id: int
    configured: bool
    date_from: date | None
    date_to: date | None
    #: ``date_from`` / ``date_to`` が両方揃っているか。False の間は ``--execute``
    #: できない (Make API が両方必須)。PLAN 自体は日付なしでも実行できる。
    dates_complete: bool
    would_execute: bool = False


def validate_date_range(
    date_from: date | None, date_to: date | None, *, require_both: bool
) -> None:
    """``date_from`` / ``date_to`` の検証 (Phase E1.4)。

    ``require_both=True`` (EXECUTE): 両方必須。片方/両方欠けは fail closed。
    ``require_both=False`` (PLAN): 両方なしは許可 (PLAN は HTTP を送らず、日付が
    無いことを ``dates_complete=False`` として報告する) が、片方だけは常に不正。
    日付を勝手に補完/デフォルト化しない。
    """

    missing = [d is None for d in (date_from, date_to)]
    if require_both and any(missing):
        raise AffiliateCommissionImportError(
            "date_from and date_to are both required (the Make API rejects "
            "requests missing either); provide them together"
        )
    if any(missing) and not all(missing):
        raise AffiliateCommissionImportError(
            "date_from and date_to must be provided together"
        )
    if not any(missing) and date_from > date_to:
        raise AffiliateCommissionImportError("date_from must not be after date_to")


class AffiliateCommissionImportService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._runs = AffiliateCommissionImportRunRepository(session)
        self._facts = AffiliateCommissionFactRepository(session)

    # -- PLAN: 完全に read-only, network 0 回, DB 書き込み 0 回 --------------
    def plan(
        self,
        *,
        affiliate_program_id: int,
        date_from: date | None = None,
        date_to: date | None = None,
        settings=None,
    ) -> CommissionImportPlan:
        settings = settings or get_settings()
        validate_date_range(date_from, date_to, require_both=False)
        if self._session.get(AffiliateProgram, affiliate_program_id) is None:
            raise EntityNotFoundError("AffiliateProgram", affiliate_program_id)
        return CommissionImportPlan(
            provider=MAKE_PROVIDER,
            affiliate_program_id=affiliate_program_id,
            configured=settings.make_api_configured,
            date_from=date_from,
            date_to=date_to,
            dates_complete=(date_from is not None and date_to is not None),
        )

    # -- EXECUTE: 完全なページネーション + 単一 import transaction ----------
    def import_commissions(
        self,
        *,
        affiliate_program_id: int,
        date_from: date | None = None,
        date_to: date | None = None,
        page_limit: int = _DEFAULT_PAGE_LIMIT,
        now: datetime | None = None,
        settings=None,
        transport: httpx.BaseTransport | None = None,
    ):
        settings = settings or get_settings()
        # Phase E1.4: 日付は両方必須。DB 参照・client 構築・run 作成・HTTP のいずれよりも
        # 先に fail closed する (補完/デフォルト化は一切しない)。
        validate_date_range(date_from, date_to, require_both=True)
        if self._session.get(AffiliateProgram, affiliate_program_id) is None:
            raise EntityNotFoundError("AffiliateProgram", affiliate_program_id)
        if not settings.make_api_configured:
            raise AffiliateCommissionImportError(
                "Make API is not configured (base URL + token)"
            )

        # run 作成前に client を構築 (設定不足はここでも fail closed するが、
        # 上の明示チェックで既に安全側 -- run 行を作る前に必ず弾く)。
        client = MakeAffiliateClient(settings, transport=transport)

        run = self._runs.add_running(
            provider=MAKE_PROVIDER,
            affiliate_program_id=affiliate_program_id,
            requested_date_from=date_from,
            requested_date_to=date_to,
            started_at=to_storage_utc(now or datetime.now(UTC)),
        )
        self._session.commit()
        run_id = run.id

        try:
            rows, page_count = self._fetch_all_pages(
                client,
                date_from=date_from,
                date_to=date_to,
                page_limit=page_limit,
            )
            return self._import_rows(run_id, rows, page_count=page_count)
        except Exception as exc:
            self._session.rollback()
            self._fail_run(run_id, exc)
            raise

    # -- complete deterministic pagination -----------------------------
    @staticmethod
    def _fetch_all_pages(
        client: MakeAffiliateClient,
        *,
        date_from: date,
        date_to: date,
        page_limit: int,
    ) -> tuple[list[MakeCommissionRow], int]:
        """``pg[offset]`` / ``pg[limit]`` で全ページを取得する。

        終端判定は「受領件数 < limit」(推論のまま)。live の ``pg`` は
        limit/offset/returnTotalCount/sortBy/sortDir の echo (metadata) で
        total/has-more を含まないため、終端判定には **使わない**。
        ``pg[returnTotalCount]`` は別途 live 検証するまで有効化しない。
        ``"commissions": null`` (該当行なし) は空ページ = ``has_more=False`` で
        即終了する。無限ループ防止の ``MAKE_COMMISSIONS_MAX_PAGES`` 上限は維持。
        """

        # validate_date_range(require_both=True) 済み -- ここで日付は必ず存在する。
        date_from_str = date_from.isoformat()
        date_to_str = date_to.isoformat()

        all_rows: list[MakeCommissionRow] = []
        offset = 0
        page_count = 0
        while True:
            page_count += 1
            if page_count > MAKE_COMMISSIONS_MAX_PAGES:
                raise AffiliateCommissionImportError(
                    f"exceeded max page safety limit ({MAKE_COMMISSIONS_MAX_PAGES})"
                )
            rows, has_more = client.get_commissions(
                date_from=date_from_str,
                date_to=date_to_str,
                offset=offset,
                limit=page_limit,
            )
            all_rows.extend(rows)
            if not has_more:
                break
            offset += page_limit
        return all_rows, page_count

    # -- single import transaction --------------------------------------
    def _import_rows(self, run_id: int, rows: list[MakeCommissionRow], *, page_count: int):
        run = self._runs.get_by_id(run_id)
        now = to_storage_utc(datetime.now(UTC))

        existing = self._facts.get_map_by_source_ids(
            provider=MAKE_PROVIDER,
            source_commission_ids=[r.source_commission_id for r in rows],
        )

        inserted_count = 0
        updated_count = 0
        unchanged_count = 0

        for row in rows:
            # storage 用正規化は行ごとに 1 度だけ (D-D2 click import と同じ規律) --
            # 以降 drift 比較・変更検出・insert/update のすべてでこの値を使う。
            # DB から読み戻した既存行は naive UTC (to_storage_utc 済み) なので、
            # 新しく parse した aware datetime とそのまま比較すると常に不一致に
            # なってしまう -- 両辺を同じ表現に揃える。
            occurred_at = to_storage_utc(row.occurred_at)
            payout_requested_at = (
                to_storage_utc(row.payout_requested_at)
                if row.payout_requested_at is not None
                else None
            )
            payout_approved_at = (
                to_storage_utc(row.payout_approved_at)
                if row.payout_approved_at is not None
                else None
            )
            payout_realized_at = (
                to_storage_utc(row.payout_realized_at)
                if row.payout_realized_at is not None
                else None
            )

            prior = existing.get(row.source_commission_id)
            if prior is None:
                new_fact = self._facts.add(
                    affiliate_program_id=run.affiliate_program_id,
                    provider=MAKE_PROVIDER,
                    source_commission_id=row.source_commission_id,
                    source_organization_id=row.source_organization_id,
                    event_type=row.event_type,
                    provider_status=row.provider_status,
                    commission_amount=row.commission_amount,
                    currency=None,  # Make commissions response には通貨が無い -- 捏造しない。
                    source=row.source,
                    occurred_at=occurred_at,
                    payout_requested_at=payout_requested_at,
                    payout_approved_at=payout_approved_at,
                    payout_realized_at=payout_realized_at,
                    first_seen_at=now,
                    last_seen_at=now,
                    source_import_run_id=run_id,
                )
                # offset ページネーションは、取り込み中に Make 側の行が増減すると
                # 同じ source_commission_id が別ページに跨って重複して現れうる
                # (offset drift)。``existing`` はループ開始前の 1 回きりの
                # snapshot なので、後続の同一 id 行がここに反映されないと
                # 二重 insert を試みて DB 一意性制約で落ちてしまう -- 今 insert
                # した行をその場で snapshot に反映し、同じ run 内の再出現を
                # 通常の update/unchanged 経路で正しく扱えるようにする。
                existing[row.source_commission_id] = new_fact
                inserted_count += 1
                continue

            # 不変であるべき identity フィールドの drift は fail closed
            # (source_commission_id が同一なら根本の事象は同一のはず)。
            if (
                prior.source_organization_id != row.source_organization_id
                or prior.event_type != row.event_type
                or prior.occurred_at != occurred_at
            ):
                raise AffiliateCommissionImportError(
                    f"source drift for commission {row.source_commission_id!r} "
                    "(organization_id/type/created changed for an existing "
                    "commission identity)"
                )

            changed = (
                prior.provider_status != row.provider_status
                or prior.commission_amount != row.commission_amount
                or prior.source != row.source
                or prior.payout_requested_at != payout_requested_at
                or prior.payout_approved_at != payout_approved_at
                or prior.payout_realized_at != payout_realized_at
            )
            self._facts.update_observed_state(
                prior,
                provider_status=row.provider_status,
                commission_amount=row.commission_amount,
                currency=None,
                source=row.source,
                payout_requested_at=payout_requested_at,
                payout_approved_at=payout_approved_at,
                payout_realized_at=payout_realized_at,
                last_seen_at=now,
                source_import_run_id=run_id,
            )
            if changed:
                updated_count += 1
            else:
                unchanged_count += 1

        self._runs.mark_succeeded(
            run,
            http_status=200,
            page_count=page_count,
            response_count=len(rows),
            inserted_count=inserted_count,
            updated_count=updated_count,
            unchanged_count=unchanged_count,
            response_snapshot={
                "page_count": page_count,
                "response_count": len(rows),
                "inserted_count": inserted_count,
                "updated_count": updated_count,
                "unchanged_count": unchanged_count,
            },
            finished_at=to_storage_utc(datetime.now(UTC)),
        )
        self._session.commit()
        return run

    # -- 失敗した run を別 transaction で failed 化 -----------------
    def _fail_run(self, run_id: int, exc: BaseException) -> None:
        try:
            run = self._runs.get_by_id(run_id)
            if run is None or run.status != ACIR_RUNNING:
                return
            reason = (
                exc.reason
                if isinstance(exc, AffiliateCommissionImportError)
                else "commission import failed"
            )
            self._runs.mark_failed(
                run,
                error_message=reason,
                finished_at=to_storage_utc(datetime.now(UTC)),
                http_status=getattr(exc, "http_status", None),
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
