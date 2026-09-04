"""WordPressDraftRun の prepare オーケストレーション (transaction owner)。

このフェーズは **prepare のみ**。WordPress へは一切通信しない。

prepare:
  承認済み wordpress-draft-request preview を再構築 → 全 gate 検証 →
  Settings.wordpress_base_url を canonical 化 → target_request_identity_hash 計算 →
  1 件の prepared run を append → 1 transaction で commit。
credential (username / app password) は読まない・保存しない・出力しない。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.draft_input_canonical import canonical_json
from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import to_storage_utc
from app.config.settings import get_settings
from app.exceptions import (
    EntityNotFoundError,
    WordPressDraftRunStateError,
)
from app.repositories.article_repository import ArticleRepository
from app.repositories.wordpress_draft_run_repository import (
    WordPressDraftRunRepository,
)
from app.services.wordpress_preview_service import WordPressPreviewService
from app.wordpress.draft_request import ENDPOINT_PATH, METHOD, V1_POST_STATUS
from app.wordpress.schemas import WordPressDraftRunPrepareResponse
from app.wordpress.target import (
    canonicalize_wordpress_base_url,
    compute_target_request_identity_hash,
)

_ARTICLE = "Article"
_PROMOTION = "ArticleDraftPromotion"


class WordPressDraftRunService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._preview = WordPressPreviewService(session)
        self._repo = WordPressDraftRunRepository(session)

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
