"""AffiliateTargetProjectionPushService — projection POST の **二段階 transaction**
オーケストレーション (D-D0.3 §9-11 の契約を実装する)。

Transaction A (POST 前):
    ローカル target を 1 回だけ読み、snapshot/署名済み request を 1 回だけ
    prepare し、**その同じ snapshot から** request manifest を作り、running run
    を作成して commit する。commit が失敗したら abort — 0 HTTP。

(commit 成功後、どの transaction も開いていない状態で) ちょうど 1 回 POST する。
auto retry しない。

Transaction B (POST 後):
    run を再読込し、結果を分類して terminal state (succeeded / failed /
    outcome_unknown) を書き込み commit する。commit が失敗しても **再 POST しない**
    — run は running のまま残る (fail-safe)。

commit(Transaction A) と POST の間で :class:`AffiliateLinkTarget` を再読込・再構築
しない — 永続化した ``requested_snapshot_hash`` / ``request_manifest_json`` は
実際に送信された内容と必ず一致する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.affiliate.projection import build_snapshot_from_targets
from app.affiliate.projection_push_acknowledgement import (
    SNAPSHOT_SCOPE_FULL,
    build_request_manifest,
    is_definitive_failure_code,
    serialize_manifest,
)
from app.affiliate.projection_push_client import (
    ProjectionPushResult,
    execute_projection_push,
    prepare_projection_push,
)
from app.affiliate.runtime_http import require_https_origin
from app.article.fact_freshness import to_storage_utc
from app.exceptions import AffiliateProjectionPushError
from app.models.affiliate_target_projection_push_run import ATPP_FAILED
from app.repositories.affiliate_link_target_repository import (
    AffiliateLinkTargetRepository,
)
from app.repositories.affiliate_target_projection_push_run_repository import (
    AffiliateTargetProjectionPushRunRepository,
)


@dataclass(frozen=True)
class AffiliateTargetProjectionPushOutcome:
    """成功時に呼び出し側 (CLI) へ返す薄い結果。"""

    result: ProjectionPushResult
    run_id: int


class AffiliateTargetProjectionPushService:
    def __init__(self, *, session_factory) -> None:
        self._session_factory = session_factory

    def push(
        self,
        *,
        settings,
        transport: httpx.BaseTransport | None = None,
        now: int | None = None,
    ) -> AffiliateTargetProjectionPushOutcome:
        base_url = settings.wordpress_base_url
        shared_secret = settings.affiliate_runtime_shared_secret
        verify_tls = getattr(settings, "wordpress_verify_tls", True)

        # -- Transaction A: prepare + freeze + commit BEFORE any network ---
        with self._session_factory() as session:
            orm_targets = AffiliateLinkTargetRepository(
                session
            ).list_all_ordered_by_token()
            snapshot, _ineligible = build_snapshot_from_targets(orm_targets)

            prepared = prepare_projection_push(
                snapshot, base_url=base_url, shared_secret=shared_secret, now=now
            )
            runtime_origin = require_https_origin(
                base_url, error_cls=AffiliateProjectionPushError
            )
            manifest = build_request_manifest(
                orm_targets=orm_targets, snapshot_targets=snapshot.targets
            )
            manifest_json = serialize_manifest(manifest)

            started_at = _started_at(now)
            run = AffiliateTargetProjectionPushRunRepository(session).add_running(
                snapshot_scope=SNAPSHOT_SCOPE_FULL,
                runtime_origin=runtime_origin,
                requested_snapshot_hash=prepared.projection_snapshot_hash,
                requested_target_count=prepared.target_count,
                request_manifest_json=manifest_json,
                started_at=started_at,
            )
            session.commit()  # durable BEFORE the network call (D-D0.3 §9/§16)
            run_id = run.id

        # -- exactly one POST, outside any open transaction ----------------
        try:
            result = execute_projection_push(
                prepared, transport=transport, verify_tls=verify_tls
            )
        except AffiliateProjectionPushError as exc:
            self._mark_terminal_failure(run_id, exc)
            raise
        else:
            self._mark_terminal_success(run_id, result)
            return AffiliateTargetProjectionPushOutcome(result=result, run_id=run_id)

    # -- Transaction B: reload + classify + commit ------------------------
    def _mark_terminal_success(
        self, run_id: int, result: ProjectionPushResult
    ) -> None:
        with self._session_factory() as session:
            repo = AffiliateTargetProjectionPushRunRepository(session)
            run = repo.get_by_id(run_id)
            repo.mark_succeeded(
                run,
                http_status=result.http_status,
                response_projection_snapshot_hash=result.projection_snapshot_hash,
                received_count=result.received_count,
                inserted_count=result.inserted_count,
                updated_count=result.updated_count,
                unchanged_count=result.unchanged_count,
                finished_at=to_storage_utc(datetime.now(UTC)),
            )
            session.commit()

    def _mark_terminal_failure(
        self, run_id: int, exc: AffiliateProjectionPushError
    ) -> None:
        status = (
            ATPP_FAILED
            if is_definitive_failure_code(exc.server_code)
            else None  # outcome_unknown; repository method chosen below
        )
        with self._session_factory() as session:
            repo = AffiliateTargetProjectionPushRunRepository(session)
            run = repo.get_by_id(run_id)
            finished_at = to_storage_utc(datetime.now(UTC))
            if status == ATPP_FAILED:
                repo.mark_failed(
                    run,
                    error_message=exc.reason,
                    finished_at=finished_at,
                    http_status=exc.http_status,
                    server_code=exc.server_code,
                )
            else:
                repo.mark_outcome_unknown(
                    run,
                    error_message=exc.reason,
                    finished_at=finished_at,
                    http_status=exc.http_status,
                    server_code=exc.server_code,
                )
            session.commit()


def _started_at(now: int | None) -> datetime:
    if now is None:
        return to_storage_utc(datetime.now(UTC))
    return to_storage_utc(datetime.fromtimestamp(now, tz=UTC))
