"""SearchConsoleImportRun の prepare / execute オーケストレーション (transaction owner)。

prepare:
  期間・property を検証し、deterministic な import identity を計算して 1 件の ``prepared``
  run を append し、1 transaction で commit する。ネットワーク呼び出しは無い。

execute (注入された provider に対して):
  prepared -> running を **provider 呼び出しの前に単独 commit** → provider から
  page daily / query daily を read → 全行を検証 → metrics を UPSERT →
  running -> succeeded を 1 transaction で commit。

  provider 失敗時は running -> failed。Search Console read は破壊的な外部副作用を持たない
  ため、失敗後は新しい run で安全に再取り込みできる (metrics は idempotent UPSERT)。

C0 では **実 provider を production 実行しない**。実行は fake provider を使うテストのみ。
credential / 生レスポンスは読まない・保存しない・出力しない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.config.settings import get_settings
from app.exceptions import (
    EntityNotFoundError,
    ExternalProviderDataError,
    ExternalProviderError,
    SearchConsoleImportStateError,
)
from app.models.search_console_import_run import (
    SC_IMPORT_PREPARED,
    SC_IMPORT_RUNNING,
)
from app.repositories.search_console_import_run_repository import (
    SearchConsoleImportRunRepository,
)
from app.repositories.search_console_metrics_repository import (
    SearchConsoleMetricsRepository,
)
from app.search_console.import_identity import (
    V1_DIMENSION_SETS,
    compute_import_identity_hash,
    dimensions_json,
)
from app.search_console.provider import SearchConsoleProvider
from app.search_console.rows import validate_page_row, validate_query_row

_RUN = "SearchConsoleImportRun"


class SearchConsoleImportService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._runs = SearchConsoleImportRunRepository(session)
        self._metrics = SearchConsoleMetricsRepository(session)

    # -- prepare (transaction owner; 通信なし) -------------------------
    def prepare(
        self,
        *,
        start_date: date,
        end_date: date,
        property_uri: str | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ):
        now = now or datetime.now(UTC)

        prop = property_uri or get_settings().search_console_property_uri
        if not prop:
            raise SearchConsoleImportStateError(
                "no Search Console property configured (SEARCH_CONSOLE_PROPERTY_URI)"
            )
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise SearchConsoleImportStateError("start_date / end_date must be dates")
        if start_date > end_date:
            raise SearchConsoleImportStateError("start_date is after end_date")
        # 期間の長さそのものに対する application 側の上限は設けない。実際の
        # データ可用性 / バッチ制約は C1 の provider/API 層で扱う。
        if end_date > now.date():
            raise SearchConsoleImportStateError("end_date is in the future")

        identity_hash = compute_import_identity_hash(
            property_uri=prop, start_date=start_date, end_date=end_date
        )
        identity = {
            "property_uri": prop,
            "start_date": start_date,
            "end_date": end_date,
            "import_identity_hash": identity_hash,
        }

        if idempotency_key is not None:
            existing = self._runs.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if self._runs.identity_of(existing) == identity:
                    return existing
                raise SearchConsoleImportStateError(
                    f"idempotency_key {idempotency_key!r} already used for a different "
                    "import identity"
                )

        active = self._runs.find_active_by_identity(identity_hash)
        if active is not None:
            if self._runs.identity_of(active) == identity:
                return active
            raise SearchConsoleImportStateError(
                "an active import run for a different identity already exists"
            )

        run = self._runs.add_prepared(
            property_uri=prop,
            start_date=start_date,
            end_date=end_date,
            dimensions_json=dimensions_json(),
            import_identity_hash=identity_hash,
            idempotency_key=idempotency_key,
            created_at=to_storage_utc(now),
        )
        self._session.commit()
        self._session.refresh(run)
        return run

    # -- execute (provider read + metrics upsert) --------------------
    def execute(self, run_id: int, *, provider: SearchConsoleProvider):
        run = self._runs.get_by_id(run_id)
        if run is None:
            raise EntityNotFoundError(_RUN, run_id)

        if run.status == SC_IMPORT_RUNNING:
            raise SearchConsoleImportStateError(
                f"run {run.id} is already running; a new run is required to retry"
            )
        if run.status != SC_IMPORT_PREPARED:
            raise SearchConsoleImportStateError(
                f"run {run.id}: status={run.status!r} is not executable"
            )

        # -- running state を provider 呼び出しの前に commit (§18) -----
        self._runs.mark_running(run, started_at=to_storage_utc(datetime.now(UTC)))
        self._session.commit()
        self._session.refresh(run)

        # -- provider read ----------------------------------------
        try:
            page_rows = list(
                provider.fetch_page_daily(
                    property_uri=run.property_uri,
                    start_date=run.start_date,
                    end_date=run.end_date,
                )
            )
            query_rows = list(
                provider.fetch_query_daily(
                    property_uri=run.property_uri,
                    start_date=run.start_date,
                    end_date=run.end_date,
                )
            )
            for r in page_rows:
                validate_page_row(r)
            for r in query_rows:
                validate_query_row(r)
        except (ExternalProviderError, ExternalProviderDataError) as exc:
            self._runs.mark_failed(
                run,
                error_message=str(exc),
                finished_at=to_storage_utc(datetime.now(UTC)),
            )
            self._session.commit()
            raise
        except Exception as exc:  # provider 実装の想定外エラーも安全に failed 化
            self._runs.mark_failed(
                run,
                error_message="search console provider read failed",
                finished_at=to_storage_utc(datetime.now(UTC)),
            )
            self._session.commit()
            raise ExternalProviderError(
                "search_console", "provider read failed"
            ) from exc

        # -- metrics upsert + succeeded を 1 transaction で ----------
        page_upserted = self._metrics.upsert_page_daily(
            page_rows, property_uri=run.property_uri, source_import_run_id=run.id
        )
        query_upserted = self._metrics.upsert_query_daily(
            query_rows, property_uri=run.property_uri, source_import_run_id=run.id
        )
        self._runs.mark_succeeded(
            run,
            page_rows_received=len(page_rows),
            query_rows_received=len(query_rows),
            page_rows_upserted=page_upserted,
            query_rows_upserted=query_upserted,
            response_snapshot={
                "property_uri": run.property_uri,
                "start_date": run.start_date.isoformat(),
                "end_date": run.end_date.isoformat(),
                "dimension_sets": [list(d) for d in V1_DIMENSION_SETS],
                "page_rows_received": len(page_rows),
                "query_rows_received": len(query_rows),
                "page_rows_upserted": page_upserted,
                "query_rows_upserted": query_upserted,
            },
            finished_at=to_storage_utc(datetime.now(UTC)),
        )
        self._session.commit()
        self._session.refresh(run)
        return run

    # -- reads ------------------------------------------------------
    def get(self, run_id: int):
        run = self._runs.get_by_id(run_id)
        if run is None:
            raise EntityNotFoundError(_RUN, run_id)
        return run

    def list_recent(self, limit: int = 50):
        return self._runs.list_recent(limit=limit)
