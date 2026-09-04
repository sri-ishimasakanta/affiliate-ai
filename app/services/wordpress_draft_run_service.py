"""WordPressDraftRun の prepare / execute オーケストレーション (transaction owner)。

prepare:
  承認済み wordpress-draft-request preview を再構築 → 全 gate 検証 →
  Settings.wordpress_base_url を canonical 化 → target_request_identity_hash 計算 →
  1 件の prepared run を append → 1 transaction で commit。WordPress へは通信しない。

execute (実 WordPress への最初の書き込み):
  全 gate 再検証 (drift・target 一致・payload SHA) → 重複 draft の read-only preflight →
  prepared -> running 遷移を **外部 POST の前に単独 commit** (途中終了しても running の
  ままにしないため) → WordPress へ厳密に 1 回だけ POST → 成功なら running -> succeeded を
  1 transaction で commit。外部 POST とローカル commit は真の atomic にはできないため、
  POST 成功後のローカル commit が失敗した場合は再 POST を絶対にせず例外で reconciliation
  を要求する (§19)。

credential (username / app password) は読まない・保存しない・出力しない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.draft_input_canonical import canonical_json
from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import to_storage_utc
from app.config.settings import Settings, get_settings
from app.exceptions import (
    EntityNotFoundError,
    WordPressDraftRunStateError,
    WordPressExternalCreateLocalPersistFailedError,
)
from app.models.wordpress_draft_run import WP_RUN_PREPARED, WP_RUN_RUNNING
from app.repositories.article_repository import ArticleRepository
from app.repositories.wordpress_draft_run_repository import (
    WordPressDraftRunRepository,
)
from app.services.wordpress_preview_service import WordPressPreviewService
from app.wordpress.client import WordPressClient
from app.wordpress.draft_request import ENDPOINT_PATH, METHOD, V1_POST_STATUS
from app.wordpress.schemas import (
    WordPressDraftRunExecuteResponse,
    WordPressDraftRunPrepareResponse,
)
from app.wordpress.target import (
    canonicalize_wordpress_base_url,
    compute_target_request_identity_hash,
)

_ARTICLE = "Article"
_PROMOTION = "ArticleDraftPromotion"


class WordPressDraftRunService:
    def __init__(
        self, session: Session, *, wordpress_client: WordPressClient | None = None
    ) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._preview = WordPressPreviewService(session)
        self._repo = WordPressDraftRunRepository(session)
        # 注入時はそれを使う (テスト用)。None なら execute() 時に settings から遅延生成。
        self._wordpress_client = wordpress_client

    # -- prepare (transaction owner) ---------------------------
    def prepare(
        self,
        article_id: int,
        *,
        source_promotion_id: int,
        expected_renderer_version: str,
        expected_rendered_content_hash: str,
        expected_payload_hash: str,
        expected_request_identity_hash: str,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> WordPressDraftRunPrepareResponse:
        now = now or datetime.now(UTC)

        # -- target site (信頼できるローカル設定からのみ) ---------
        base_raw = get_settings().wordpress_base_url
        if not base_raw:
            raise WordPressDraftRunStateError("target base URL missing (WORDPRESS_BASE_URL)")
        target_base_url = canonicalize_wordpress_base_url(base_raw)  # -> WordPressTargetError

        # -- approved request preview (renderer / rendered drift guard 内蔵) -----
        pv = self._preview.draft_request_preview(
            article_id,
            expected_renderer_version=expected_renderer_version,
            expected_rendered_content_hash=expected_rendered_content_hash,
        )

        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError(_ARTICLE, article_id)

        # -- gates (§16) --------------------------------------
        self._assert_gates(
            article=article,
            pv=pv,
            source_promotion_id=source_promotion_id,
            expected_payload_hash=expected_payload_hash,
            expected_request_identity_hash=expected_request_identity_hash,
        )

        target_request_identity_hash = compute_target_request_identity_hash(
            request_identity_hash=pv.request_identity_hash,
            target_base_url=target_base_url,
        )

        payload_json = canonical_json(pv.payload)
        if compute_text_hash(payload_json) != pv.payload_hash:
            raise WordPressDraftRunStateError(
                "canonical payload serialization does not match payload_hash"
            )

        identity = {
            "article_id": article_id,
            "source_promotion_id": source_promotion_id,
            "target_request_identity_hash": target_request_identity_hash,
            "request_identity_hash": pv.request_identity_hash,
            "payload_hash": pv.payload_hash,
        }

        # -- idempotency prelookup (§17) ---------------------
        if idempotency_key is not None:
            existing = self._repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if self._repo.identity_of(existing) == identity:
                    return self._response(existing, already=True)
                raise WordPressDraftRunStateError(
                    f"idempotency_key {idempotency_key!r} already used for a "
                    "different wordpress draft run identity"
                )

        # -- duplicate protection (§18): 同一 target に active run が既にあれば再利用 ---
        active = self._repo.find_active_by_target_identity(
            article_id, target_request_identity_hash
        )
        if active is not None:
            if self._repo.identity_of(active) == identity:
                return self._response(active, already=True)
            raise WordPressDraftRunStateError(
                "an active wordpress draft run for a different identity already "
                "exists for this article/target"
            )

        # -- single transaction ------------------------------
        try:
            run = self._repo.add_prepared(
                article_id=article_id,
                source_promotion_id=source_promotion_id,
                target_base_url=target_base_url,
                method=METHOD,
                endpoint_path=ENDPOINT_PATH,
                payload_json=payload_json,
                payload_hash=pv.payload_hash,
                request_identity_hash=pv.request_identity_hash,
                target_request_identity_hash=target_request_identity_hash,
                canonical_body_hash=pv.canonical_body_hash,
                canonical_meta_hash=pv.canonical_meta_hash,
                renderer_version=pv.renderer_version,
                rendered_content_hash=pv.rendered_content_hash,
                idempotency_key=idempotency_key,
                created_at=to_storage_utc(now),
            )
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            if idempotency_key is not None:
                existing = self._repo.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return self._response(existing, already=True)
            active = self._repo.find_active_by_target_identity(
                article_id, target_request_identity_hash
            )
            if active is not None:
                return self._response(active, already=True)
            raise
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(run)
        return self._response(run, already=False)

    # -- read -----------------------------------------------
    def list_for_article(self, article_id: int):
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        return self._repo.list_by_article(article_id)

    def get(self, article_id: int, run_id: int):
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        run = self._repo.get_by_id(run_id)
        if run is None or run.article_id != article_id:
            raise EntityNotFoundError("WordPressDraftRun", run_id)
        return run

    # -- execute (実 WordPress への最初の書き込み; transaction owner) -----
    def execute(
        self,
        article_id: int,
        run_id: int,
        *,
        expected_target_request_identity_hash: str,
    ) -> WordPressDraftRunExecuteResponse:
        run = self._repo.get_by_id(run_id)
        if run is None or run.article_id != article_id:
            raise EntityNotFoundError("WordPressDraftRun", run_id)

        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError(_ARTICLE, article_id)

        # -- running-state recovery rule (§21): 絶対に二重 POST しない -----
        if run.status == WP_RUN_RUNNING:
            raise WordPressDraftRunStateError(
                f"run {run.id} is already running; recovery required, no automatic retry"
            )
        if run.status != WP_RUN_PREPARED:
            raise WordPressDraftRunStateError(
                f"run {run.id}: status={run.status!r} is not executable; retry requires "
                "a new WordPressDraftRun after explicit Human review"
            )

        settings = get_settings()
        self._assert_execute_gates(
            article=article,
            run=run,
            settings=settings,
            expected_target_request_identity_hash=expected_target_request_identity_hash,
        )

        client = self._wordpress_client or WordPressClient(settings)

        # -- duplicate preflight (§10): read-only GET。書き込みには数えない ---
        slug = _payload_field(run.payload_json, "slug") or ""
        existing_ids = client.find_draft_posts_by_slug(slug)
        if existing_ids:
            raise WordPressDraftRunStateError(
                "duplicate draft(s) already exist for this exact slug "
                f"(ids={existing_ids}); leaving run prepared for Human review"
            )

        # -- local state BEFORE the external POST (§14): 独立した commit ---
        # プロセスが外部呼び出し中に死んでも running のまま放置しない。
        self._repo.mark_running(run, started_at=to_storage_utc(datetime.now(UTC)))
        self._session.commit()
        self._session.refresh(run)

        # -- exactly ONE external POST (§15)。リトライは一切しない -----
        try:
            created = client.create_draft_post_exact(run.payload_json)
        except Exception as exc:
            self._repo.mark_failed(
                run,
                error_message=str(exc),
                finished_at=to_storage_utc(datetime.now(UTC)),
            )
            self._session.commit()
            raise

        # -- success: 1 transaction で commit (§18) ----------------------
        response_snapshot = {
            "id": created.id,
            "status": created.status,
            "slug": created.slug,
            "link": created.link,
        }
        try:
            self._repo.mark_succeeded(
                run,
                wordpress_post_id=str(created.id),
                wordpress_post_status=created.status,
                wordpress_post_url=created.link,
                response_snapshot=response_snapshot,
                finished_at=to_storage_utc(datetime.now(UTC)),
            )
            self._articles.update(article, {"wordpress_post_id": created.id})
            self._session.commit()
        except Exception as exc:
            # §19: WordPress 側は既に成功している可能性が高い。絶対に再 POST しない。
            self._session.rollback()
            raise WordPressExternalCreateLocalPersistFailedError(str(created.id)) from exc

        self._session.refresh(run)
        return self._execute_response(run)

    # -- internals ----------------------------------------
    @staticmethod
    def _assert_gates(
        *, article, pv, source_promotion_id, expected_payload_hash,
        expected_request_identity_hash,
    ) -> None:
        fails: list[str] = []
        if str(article.status) != "review":
            fails.append(f"Article.status={article.status!r} (expected review)")
        if article.wordpress_post_id is not None:
            fails.append("Article.wordpress_post_id is not null")
        if article.published_url is not None:
            fails.append("Article.published_url is not null")
        if article.published_at is not None:
            fails.append("Article.published_at is not null")
        if pv.source_promotion_id is None:
            fails.append("no ArticleDraftPromotion for this Article")
        elif pv.source_promotion_id != source_promotion_id:
            fails.append(
                f"source_promotion_id {source_promotion_id} != current "
                f"{pv.source_promotion_id}"
            )
        if not pv.publishable or pv.publication_validation_report.get("overall") != "pass":
            fails.append(
                "publication validator not pass: "
                + str(pv.blocking_reasons or pv.publication_validation_report.get("overall"))
            )
        if pv.payload is None:
            fails.append("draft-request preview did not build a sendable payload")
        if pv.payload_hash != expected_payload_hash:
            fails.append("payload_hash drifted from approved")
        if pv.request_identity_hash != expected_request_identity_hash:
            fails.append("request_identity_hash drifted from approved")
        if pv.method != METHOD:
            fails.append(f"method {pv.method!r} != {METHOD!r}")
        if pv.endpoint_path != ENDPOINT_PATH:
            fails.append(f"endpoint_path {pv.endpoint_path!r} != {ENDPOINT_PATH!r}")
        if pv.target_post_status != V1_POST_STATUS:
            fails.append(f"payload status {pv.target_post_status!r} != draft")
        if fails:
            raise WordPressDraftRunStateError("; ".join(fails))

    def _assert_execute_gates(
        self,
        *,
        article,
        run,
        settings: Settings,
        expected_target_request_identity_hash: str,
    ) -> None:
        fails: list[str] = []
        if str(article.status) != "review":
            fails.append(f"Article.status={article.status!r} (expected review)")
        if article.wordpress_post_id is not None:
            fails.append("Article.wordpress_post_id is not null")
        if article.published_url is not None:
            fails.append("Article.published_url is not null")
        if article.published_at is not None:
            fails.append("Article.published_at is not null")
        if run.target_request_identity_hash != expected_target_request_identity_hash:
            fails.append(
                "target_request_identity_hash does not match the caller-approved value"
            )
        if run.method != METHOD:
            fails.append(f"run.method {run.method!r} != {METHOD!r}")
        if run.endpoint_path != ENDPOINT_PATH:
            fails.append(f"run.endpoint_path {run.endpoint_path!r} != {ENDPOINT_PATH!r}")
        if compute_text_hash(run.payload_json) != run.payload_hash:
            fails.append("stored payload_json no longer matches payload_hash")
        if _payload_field(run.payload_json, "status") != V1_POST_STATUS:
            fails.append("stored payload status is not draft")
        if not settings.wordpress_configured:
            fails.append("wordpress_configured is false")
        else:
            configured_target = canonicalize_wordpress_base_url(
                settings.wordpress_base_url or ""
            )
            if configured_target != run.target_base_url:
                fails.append(
                    f"configured target {configured_target!r} != prepared run target "
                    f"{run.target_base_url!r}"
                )
        if fails:
            raise WordPressDraftRunStateError("; ".join(fails))

        # -- current content drift guard (§6/§13): 承認済み preview を再構築 -----
        pv = self._preview.draft_request_preview(
            run.article_id,
            expected_renderer_version=run.renderer_version,
            expected_rendered_content_hash=run.rendered_content_hash,
        )  # renderer/rendered drift -> RenderedCandidateChangedError

        drift_fails: list[str] = []
        if pv.payload_hash != run.payload_hash:
            drift_fails.append("current payload_hash has drifted from the prepared run")
        if pv.request_identity_hash != run.request_identity_hash:
            drift_fails.append(
                "current request_identity_hash has drifted from the prepared run"
            )
        if pv.canonical_body_hash != run.canonical_body_hash:
            drift_fails.append(
                "current canonical_body_hash has drifted from the prepared run"
            )
        if pv.canonical_meta_hash != run.canonical_meta_hash:
            drift_fails.append(
                "current canonical_meta_hash has drifted from the prepared run"
            )
        if pv.source_promotion_id != run.source_promotion_id:
            drift_fails.append(
                "current source_promotion_id has drifted from the prepared run"
            )
        if drift_fails:
            raise WordPressDraftRunStateError("; ".join(drift_fails))

    @staticmethod
    def _execute_response(run) -> WordPressDraftRunExecuteResponse:
        return WordPressDraftRunExecuteResponse(
            run_id=run.id,
            status=run.status,
            article_id=run.article_id,
            target_base_url=run.target_base_url,
            target_request_identity_hash=run.target_request_identity_hash,
            wordpress_post_id=run.wordpress_post_id,
            wordpress_post_status=run.wordpress_post_status,
            wordpress_post_url=run.wordpress_post_url,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

    @staticmethod
    def _response(run, *, already: bool) -> WordPressDraftRunPrepareResponse:
        return WordPressDraftRunPrepareResponse(
            run_id=run.id,
            status=run.status,
            already_prepared=already,
            article_id=run.article_id,
            source_promotion_id=run.source_promotion_id,
            target_base_url=run.target_base_url,
            method=run.method,
            endpoint_path=run.endpoint_path,
            payload_hash=run.payload_hash,
            request_identity_hash=run.request_identity_hash,
            target_request_identity_hash=run.target_request_identity_hash,
            canonical_body_hash=run.canonical_body_hash,
            canonical_meta_hash=run.canonical_meta_hash,
            renderer_version=run.renderer_version,
            rendered_content_hash=run.rendered_content_hash,
            created_at=run.created_at,
            wordpress_configured=get_settings().wordpress_configured,
        )


def _payload_field(payload_json: str, key: str) -> str | None:
    try:
        data = json.loads(payload_json)
    except ValueError:
        return None
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, str) else None
