"""AffiliateClickImportRun の実行オーケストレーション (transaction owner)。

reverse measurement path の唯一の書き込み口:
WordPress ``bfl_outbound_clicks`` (source of truth) → 署名付き GET
``/wp-json/affiliate-ai/v1/outbound-clicks`` → :class:`AffiliateOutboundClick`
replica + :class:`AffiliateClickImportRun` provenance。

``import_next_page`` は spec §17 の 9 ステップに従う:

  1. run cursor 解決     : ``latest_succeeded().response_next_since_id or 0``
  2. replica cursor 解決 : ``max_source_click_id() or 0``
  3. preflight 整合ガード: ``run_cursor == replica_cursor`` でなければ **run 行を作らず**
     ``AffiliateClickImportError("local click cursor integrity mismatch")``
     (0 run / 0 HTTP / 0 insert / cursor 不変)
  4. ``since_id = cursor``
  5. running run を作成し **commit** (ネットワーク前に durable 化)
  6. 署名付き GET を prepare
  7. GET を **ちょうど 1 回** 実行 (auto retry / redirect 追従なし)
  8. レスポンスを in-memory で厳格検証
  9. 単一 import transaction: 既存行 load → timestamp を行ごとに 1 度だけ正規化 →
     duplicate / drift 判定 → 欠損 click を収集して insert → unresolved token 数を算出 →
     post-import cursor invariant → mark succeeded → 1 度だけ commit

run 作成後に失敗したら: rollback → 可能なら別 transaction で run を failed 化して commit →
re-raise。**部分ページ取り込みはしない。**

secret / signature / raw response body / headers / token 値 は print / log / 例外文言 /
run storage に一切残さない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.affiliate.click_export_client import (
    ClickExportPage,
    execute_click_export,
    prepare_click_export,
)
from app.article.fact_freshness import to_storage_utc
from app.config.settings import get_settings
from app.exceptions import AffiliateClickImportError
from app.models.affiliate_click_import_run import ACI_RUNNING
from app.repositories.affiliate_click_import_run_repository import (
    AffiliateClickImportRunRepository,
)
from app.repositories.affiliate_link_target_repository import (
    AffiliateLinkTargetRepository,
)
from app.repositories.affiliate_outbound_click_repository import (
    AffiliateOutboundClickRepository,
    NewOutboundClick,
)

_MAX_LIMIT = 1000


@dataclass(frozen=True)
class ClickImportPlan:
    """``--execute`` なしの読み取り専用プレビュー (run を作らない)。"""

    since_id: int
    replica_max_source_click_id: int
    cursor_consistent: bool


class AffiliateClickImportService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._runs = AffiliateClickImportRunRepository(session)
        self._clicks = AffiliateOutboundClickRepository(session)
        self._targets = AffiliateLinkTargetRepository(session)

    # -- read-only cursor resolution (副作用なし) --------------------
    def _run_cursor(self) -> int:
        latest = self._runs.latest_succeeded()
        if latest is None:
            return 0
        return latest.response_next_since_id or 0

    def _replica_cursor(self) -> int:
        return self._clicks.max_source_click_id() or 0

    def plan(self) -> ClickImportPlan:
        run_cursor = self._run_cursor()
        replica_cursor = self._replica_cursor()
        return ClickImportPlan(
            since_id=run_cursor,
            replica_max_source_click_id=replica_cursor,
            cursor_consistent=(run_cursor == replica_cursor),
        )

    # -- import one page (ちょうど 1 回 GET) ------------------------
    def import_next_page(
        self,
        *,
        limit: int = 1000,
        now: datetime | None = None,
        settings=None,
        transport: httpx.BaseTransport | None = None,
    ):
        settings = settings or get_settings()
        if not settings.affiliate_runtime_push_configured:
            raise AffiliateClickImportError(
                "affiliate runtime is not configured (base URL + shared secret)"
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not (1 <= limit <= _MAX_LIMIT)
        ):
            raise AffiliateClickImportError(
                f"limit must be between 1 and {_MAX_LIMIT}"
            )

        # (1)(2)(3) pre-network cursor integrity — fail closed, run 行は作らない
        run_cursor = self._run_cursor()
        replica_cursor = self._replica_cursor()
        if run_cursor != replica_cursor:
            raise AffiliateClickImportError("local click cursor integrity mismatch")

        # (4)
        since_id = run_cursor

        # (5) running run を network 前に durable 化
        run = self._runs.add_running(
            requested_since_id=since_id,
            requested_limit=limit,
            started_at=to_storage_utc(now or datetime.now(UTC)),
        )
        self._session.commit()
        run_id = run.id

        try:
            # (6) 署名付き GET を prepare
            prepared = prepare_click_export(
                base_url=settings.wordpress_base_url,
                shared_secret=settings.affiliate_runtime_shared_secret,
                since_id=since_id,
                limit=limit,
            )
            # (7)(8) ちょうど 1 回 GET + in-memory 検証
            page = execute_click_export(
                prepared,
                transport=transport,
                verify_tls=bool(getattr(settings, "wordpress_verify_tls", True)),
            )
            # (9) 単一 import transaction
            return self._import_page(run_id, page)
        except Exception as exc:
            self._session.rollback()
            self._fail_run(run_id, exc)
            raise

    # -- (9) 単一 import transaction -------------------------------
    def _import_page(self, run_id: int, page: ClickExportPage):
        run = self._runs.get_by_id(run_id)

        existing = self._clicks.get_map_by_source_ids(
            [r.source_click_id for r in page.rows]
        )

        to_insert: list[NewOutboundClick] = []
        duplicate_count = 0
        unresolved_token_count = 0

        for row in page.rows:
            # §18: storage 用正規化は行ごとに **1 度だけ**。以降 duplicate 比較・
            # drift 比較・insert のすべてでこの値を使う (naive stored 同士の比較)。
            storage_clicked_at = to_storage_utc(row.clicked_at_utc)

            # §20: token 未解決は行数でカウント (distinct ではない)。情報のみ。
            if self._targets.get_by_token(row.token) is None:
                unresolved_token_count += 1

            prior = existing.get(row.source_click_id)
            if prior is not None:
                if (
                    prior.token == row.token
                    and prior.clicked_at == storage_clicked_at
                ):
                    duplicate_count += 1
                    continue
                # §19: token 相違 または 正規化後 clicked_at 相違 → source drift。
                # ページ全体を rollback し、既存 source 行は上書きしない。
                raise AffiliateClickImportError(
                    f"source click drift for source_click_id {row.source_click_id}"
                )

            to_insert.append(
                NewOutboundClick(
                    source_click_id=row.source_click_id,
                    token=row.token,
                    clicked_at=storage_clicked_at,
                    source_import_run_id=run_id,
                )
            )

        inserted_count = self._clicks.insert_many(to_insert)

        # §21: post-import cursor invariant — 空・非空いずれでも成立必須。
        post_max = self._clicks.max_source_click_id() or 0
        if post_max != page.next_since_id:
            raise AffiliateClickImportError(
                "post-import click cursor invariant violated"
            )

        first_id = page.rows[0].source_click_id if page.rows else None
        last_id = page.rows[-1].source_click_id if page.rows else None

        self._runs.mark_succeeded(
            run,
            http_status=page.http_status,
            response_count=page.count,
            response_next_since_id=page.next_since_id,
            inserted_count=inserted_count,
            duplicate_count=duplicate_count,
            unresolved_token_count=unresolved_token_count,
            first_source_click_id=first_id,
            last_source_click_id=last_id,
            has_more=page.has_more,
            response_snapshot={
                "schema_version": page.schema_version,
                "count": page.count,
                "limit": page.limit,
                "next_since_id": page.next_since_id,
                "inserted_count": inserted_count,
                "duplicate_count": duplicate_count,
                "unresolved_token_count": unresolved_token_count,
                "has_more": page.has_more,
            },
            finished_at=to_storage_utc(datetime.now(UTC)),
        )
        self._session.commit()
        return run

    # -- 失敗した run を別 transaction で failed 化 -----------------
    def _fail_run(self, run_id: int, exc: BaseException) -> None:
        try:
            run = self._runs.get_by_id(run_id)
            if run is None or run.status != ACI_RUNNING:
                return
            reason = (
                exc.reason
                if isinstance(exc, AffiliateClickImportError)
                else "click import failed"
            )
            self._runs.mark_failed(
                run,
                error_message=reason,
                finished_at=to_storage_utc(datetime.now(UTC)),
                http_status=getattr(exc, "http_status", None),
                server_code=getattr(exc, "server_code", None),
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
