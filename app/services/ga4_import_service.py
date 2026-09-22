"""Ga4ImportRun の prepare / execute オーケストレーション (transaction owner)。

:class:`SearchConsoleImportService` と同じ形を採る -- 実績のある設計を複製ではなく
**踏襲** する:

prepare:
  期間・property を検証し、deterministic な import identity を計算して 1 件の
  ``prepared`` run を append して commit する。ネットワーク呼び出しは無い。

execute (注入された provider に対して):
  prepared -> running を **provider 呼び出しの前に単独 commit** → provider から
  page daily (全トラフィック + オーガニック検索) を read → 全行を検証 →
  metrics を UPSERT → running -> succeeded を 1 transaction で commit。

  provider 失敗時は running -> failed。GA4 の read は外部に副作用を持たないため、
  失敗後は新しい run で安全に再取り込みできる (metrics は idempotent UPSERT)。

credential / 生レスポンスは読まない・保存しない・出力しない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.analytics.import_identity import (
    CHANNEL_SCOPES,
    PAGE_DIMENSIONS,
    PAGE_METRICS,
    compute_import_identity_hash,
    request_json,
)
from app.analytics.provider import Ga4Provider
from app.analytics.rows import validate_page_row
from app.article.fact_freshness import to_storage_utc
from app.config.settings import get_settings
from app.exceptions import (
    EntityNotFoundError,
    ExternalProviderDataError,
    ExternalProviderError,
    Ga4ImportStateError,
)
from app.models.ga4_import_run import GA4_IMPORT_PREPARED, GA4_IMPORT_RUNNING
from app.repositories.ga4_import_run_repository import Ga4ImportRunRepository
from app.repositories.ga4_metrics_repository import Ga4MetricsRepository

_RUN = "Ga4ImportRun"
_PROVIDER = "ga4"


def _assert_within_window(metric_date: date, *, start_date: date, end_date: date) -> None:
    """provider 行の metric_date が要求した window 内か (inclusive)。

    範囲外の行を run に紐付けて永続化すると provenance と metric の帰属が矛盾する
    ため、永続化前に弾く。
    """

    if metric_date < start_date or metric_date > end_date:
        raise ExternalProviderDataError(
            _PROVIDER,
            f"provider row metric_date {metric_date.isoformat()} is outside the import "
            f"window {start_date.isoformat()}..{end_date.isoformat()}",
        )


class Ga4ImportService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._runs = Ga4ImportRunRepository(session)
        self._metrics = Ga4MetricsRepository(session)

    # -- prepare (transaction owner; 通信なし) --------------------------------
    def prepare(
        self,
        *,
        start_date: date,
        end_date: date,
        property_id: str | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ):
        now = now or datetime.now(UTC)

        prop = property_id or get_settings().ga4_property_id
        if not prop:
            raise Ga4ImportStateError("no GA4 property configured (GA4_PROPERTY_ID)")
        prop = str(prop).strip()
        if prop.startswith("properties/"):
            prop = prop[len("properties/") :]
        if not prop.isdigit():
            raise Ga4ImportStateError("GA4 property id must be numeric")
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise Ga4ImportStateError("start_date / end_date must be dates")
        if start_date > end_date:
            raise Ga4ImportStateError("start_date is after end_date")
        if end_date > now.date():
            raise Ga4ImportStateError("end_date is in the future")

        identity_hash = compute_import_identity_hash(
            property_id=prop, start_date=start_date, end_date=end_date
        )
        identity = {
            "property_id": prop,
            "start_date": start_date,
            "end_date": end_date,
            "import_identity_hash": identity_hash,
        }

        if idempotency_key is not None:
            existing = self._runs.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if self._runs.identity_of(existing) == identity:
                    return existing
                raise Ga4ImportStateError(
                    f"idempotency_key {idempotency_key!r} already used for a different "
                    "import identity"
                )

        active = self._runs.find_active_by_identity(identity_hash)
        if active is not None:
            if self._runs.identity_of(active) == identity:
                return active
            raise Ga4ImportStateError(
                "an active import run for a different identity already exists"
            )

        run = self._runs.add_prepared(
            property_id=prop,
            start_date=start_date,
            end_date=end_date,
            request_json=request_json(),
            import_identity_hash=identity_hash,
            idempotency_key=idempotency_key,
            created_at=to_storage_utc(now),
        )
        self._session.commit()
        self._session.refresh(run)
        return run

    # -- execute (provider read + metrics upsert) -----------------------------
    def execute(self, run_id: int, *, provider: Ga4Provider):
        run = self._runs.get_by_id(run_id)
        if run is None:
            raise EntityNotFoundError(_RUN, run_id)

        if run.status == GA4_IMPORT_RUNNING:
            raise Ga4ImportStateError(
                f"run {run.id} is already running; a new run is required to retry"
            )
        if run.status != GA4_IMPORT_PREPARED:
            raise Ga4ImportStateError(f"run {run.id}: status={run.status!r} is not executable")

        # -- running state を provider 呼び出しの前に commit -------------------
        self._runs.mark_running(run, started_at=to_storage_utc(datetime.now(UTC)))
        self._session.commit()
        self._session.refresh(run)

        try:
            timezone = provider.fetch_property_timezone(property_id=run.property_id)
            rows = list(
                provider.fetch_page_daily(
                    property_id=run.property_id,
                    start_date=run.start_date,
                    end_date=run.end_date,
                )
            )
            for row in rows:
                validate_page_row(row)
                _assert_within_window(
                    row.metric_date, start_date=run.start_date, end_date=run.end_date
                )
        except (ExternalProviderError, ExternalProviderDataError) as exc:
            self._runs.mark_failed(
                run, error_message=str(exc), finished_at=to_storage_utc(datetime.now(UTC))
            )
            self._session.commit()
            raise
        except Exception as exc:  # provider 実装の想定外エラーも安全に failed 化
            self._runs.mark_failed(
                run,
                error_message="ga4 provider read failed",
                finished_at=to_storage_utc(datetime.now(UTC)),
            )
            self._session.commit()
            raise ExternalProviderError(_PROVIDER, "provider read failed") from exc

        upserted = self._metrics.upsert_page_daily(
            rows, property_id=run.property_id, source_import_run_id=run.id
        )
        self._runs.mark_succeeded(
            run,
            page_rows_received=len(rows),
            page_rows_upserted=upserted,
            data_through_date=max((r.metric_date for r in rows), default=None),
            property_timezone=timezone,
            response_snapshot={
                "property_id": run.property_id,
                "start_date": run.start_date.isoformat(),
                "end_date": run.end_date.isoformat(),
                "dimensions": list(PAGE_DIMENSIONS),
                "metrics": list(PAGE_METRICS),
                "channel_scopes": list(CHANNEL_SCOPES),
                "property_timezone": timezone,
            },
            finished_at=to_storage_utc(datetime.now(UTC)),
        )
        self._session.commit()
        self._session.refresh(run)
        return run
