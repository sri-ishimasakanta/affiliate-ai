"""WordPressPublicationRun の prepare オーケストレーション (transaction owner)。

このフェーズは **prepare のみ**。WordPress へは一切通信しない
(``WordPressClient`` を import すらしない — 事故でも publish できないようにする)。

prepare:
  Article / source WordPressDraftRun を読み、全 guard を検証し、exact な publish request
  (``{"status":"publish"}`` → ``POST /wp-json/wp/v2/posts/{id}``) と 3 つの identity hash を
  計算し、1 件の ``prepared`` run を append して 1 transaction で commit する。

credential (username / app password) は読まない・保存しない・出力しない。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import to_storage_utc
from app.config.settings import get_settings
from app.exceptions import (
    EntityNotFoundError,
    WordPressPublicationRunConflictError,
    WordPressPublicationRunPreparationError,
)
from app.models.wordpress_draft_run import WP_RUN_SUCCEEDED
from app.repositories.article_repository import ArticleRepository
from app.repositories.wordpress_draft_run_repository import WordPressDraftRunRepository
from app.repositories.wordpress_publication_run_repository import (
    WordPressPublicationRunRepository,
)
from app.wordpress.publish_request import (
    V1_EXPECTED_PRE_PUBLISH_STATUS,
    build_wordpress_publish_request,
)
from app.wordpress.schemas import WordPressPublicationRunPrepareResponse
from app.wordpress.target import canonicalize_wordpress_base_url

_ARTICLE = "Article"
_DRAFT_RUN = "WordPressDraftRun"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class WordPressPublicationRunService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._draft_runs = WordPressDraftRunRepository(session)
        self._repo = WordPressPublicationRunRepository(session)

    # -- prepare (transaction owner; 通信なし) ---------------------------
    def prepare(
        self,
        article_id: int,
        *,
        source_wordpress_draft_run_id: int,
        expected_wordpress_post_id: int,
        expected_target_request_identity_hash: str,
        wordpress_raw_content_hash: str,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> WordPressPublicationRunPrepareResponse:
        now = now or datetime.now(UTC)

        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError(_ARTICLE, article_id)

        source_run = self._draft_runs.get_by_id(source_wordpress_draft_run_id)
        if source_run is None:
            raise EntityNotFoundError(_DRAFT_RUN, source_wordpress_draft_run_id)

        settings = get_settings()
        if not _HEX64_RE.match(wordpress_raw_content_hash or ""):
            raise WordPressPublicationRunPreparationError(
                "wordpress_raw_content_hash must be 64 lowercase hex characters"
            )

        self._assert_prepare_gates(
            article=article,
            source_run=source_run,
            expected_wordpress_post_id=expected_wordpress_post_id,
            expected_target_request_identity_hash=expected_target_request_identity_hash,
            settings=settings,
        )

        target_base_url = canonicalize_wordpress_base_url(
            settings.wordpress_base_url or ""
        )
        wp_post_id = str(article.wordpress_post_id)

        pr = build_wordpress_publish_request(
            article_id=article_id,
            source_wordpress_draft_run_id=source_run.id,
            wordpress_post_id=wp_post_id,
            target_base_url=target_base_url,
            canonical_body_hash=source_run.canonical_body_hash,
            canonical_meta_hash=source_run.canonical_meta_hash,
            wordpress_raw_content_hash=wordpress_raw_content_hash,
            expected_pre_publish_status=V1_EXPECTED_PRE_PUBLISH_STATUS,
        )

        identity = {
            "article_id": article_id,
            "source_wordpress_draft_run_id": source_run.id,
            "wordpress_post_id": wp_post_id,
            "publish_payload_hash": pr.publish_payload_hash,
            "publication_request_identity_hash": pr.publication_request_identity_hash,
            "target_publication_request_identity_hash": (
                pr.target_publication_request_identity_hash
            ),
        }

        # -- idempotency prelookup -------------------------------------
        if idempotency_key is not None:
            existing = self._repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if self._repo.identity_of(existing) == identity:
                    return self._response(existing, already=True)
                raise WordPressPublicationRunConflictError(
                    f"idempotency_key {idempotency_key!r} already used for a different "
                    "publication run identity"
                )

        # -- one succeeded publication run per Article (V1: publish は 1 度きり) --
        if self._repo.succeeded_exists_for_article(article_id):
            raise WordPressPublicationRunConflictError(
                "a succeeded WordPressPublicationRun already exists for this Article"
            )

        # -- duplicate active-run protection ---------------------------
        active = self._repo.find_active_by_publication_identity(
            article_id, pr.target_publication_request_identity_hash
        )
        if active is not None:
            if self._repo.identity_of(active) == identity:
                return self._response(active, already=True)
            raise WordPressPublicationRunConflictError(
                "an active WordPressPublicationRun for a different identity already "
                "exists for this Article/target"
            )

        # -- single transaction --------------------------------------
        try:
            run = self._repo.add_prepared(
                article_id=article_id,
                source_wordpress_draft_run_id=source_run.id,
                target_base_url=target_base_url,
                wordpress_post_id=wp_post_id,
                method=pr.method,
                endpoint_path=pr.endpoint_path,
                publish_payload_json=pr.publish_payload_json,
                publish_payload_hash=pr.publish_payload_hash,
                publication_request_identity_hash=pr.publication_request_identity_hash,
                target_publication_request_identity_hash=(
                    pr.target_publication_request_identity_hash
                ),
                canonical_body_hash=source_run.canonical_body_hash,
                canonical_meta_hash=source_run.canonical_meta_hash,
                wordpress_raw_content_hash=wordpress_raw_content_hash,
                expected_pre_publish_status=V1_EXPECTED_PRE_PUBLISH_STATUS,
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
            active = self._repo.find_active_by_publication_identity(
                article_id, pr.target_publication_request_identity_hash
            )
            if active is not None:
                return self._response(active, already=True)
            raise
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(run)
        return self._response(run, already=False)

    # -- reads --------------------------------------------------------
    def list_for_article(self, article_id: int):
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        return self._repo.list_by_article(article_id)

    def get(self, article_id: int, run_id: int):
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        run = self._repo.get_by_id(run_id)
        if run is None or run.article_id != article_id:
            raise EntityNotFoundError("WordPressPublicationRun", run_id)
        return run

    # -- internals --------------------------------------------------
    def _assert_prepare_gates(
        self,
        *,
        article,
        source_run,
        expected_wordpress_post_id: int,
        expected_target_request_identity_hash: str,
        settings,
    ) -> None:
        fails: list[str] = []

        if str(article.status) != "approved":
            fails.append(f"Article.status={article.status!r} (expected approved)")
        if article.wordpress_post_id is None:
            fails.append("Article.wordpress_post_id is null")
        elif article.wordpress_post_id != expected_wordpress_post_id:
            fails.append(
                f"Article.wordpress_post_id={article.wordpress_post_id!r} != "
                f"expected_wordpress_post_id={expected_wordpress_post_id!r}"
            )
        if article.published_url is not None:
            fails.append("Article.published_url is not null")
        if article.published_at is not None:
            fails.append("Article.published_at is not null")

        body_hash = compute_text_hash(article.body or "")
        meta_hash = compute_text_hash(article.meta_description or "")

        if source_run.article_id != article.id:
            fails.append("source WordPressDraftRun.article_id does not match Article.id")
        if source_run.status != WP_RUN_SUCCEEDED:
            fails.append(
                f"source WordPressDraftRun.status={source_run.status!r} (expected succeeded)"
            )
        if source_run.wordpress_post_id != (
            str(article.wordpress_post_id) if article.wordpress_post_id is not None else None
        ):
            fails.append(
                "source WordPressDraftRun.wordpress_post_id does not match "
                "Article.wordpress_post_id"
            )
        if source_run.wordpress_post_status != "draft":
            fails.append(
                f"source WordPressDraftRun.wordpress_post_status="
                f"{source_run.wordpress_post_status!r} (expected draft)"
            )
        if source_run.error_message is not None:
            fails.append("source WordPressDraftRun.error_message is not null")
        if source_run.target_request_identity_hash != expected_target_request_identity_hash:
            fails.append(
                "source WordPressDraftRun.target_request_identity_hash does not match "
                "the caller-approved value"
            )
        if body_hash != source_run.canonical_body_hash:
            fails.append(
                "current Article body hash has drifted from the approved draft run"
            )
        if meta_hash != source_run.canonical_meta_hash:
            fails.append(
                "current Article meta hash has drifted from the approved draft run"
            )

        if not settings.wordpress_configured:
            fails.append("wordpress_configured is false")
        else:
            configured = canonicalize_wordpress_base_url(settings.wordpress_base_url or "")
            if not configured.startswith("https://"):
                fails.append("configured target is not https")
            if configured != source_run.target_base_url:
                fails.append(
                    f"configured target {configured!r} != source run target "
                    f"{source_run.target_base_url!r}"
                )

        if fails:
            raise WordPressPublicationRunPreparationError("; ".join(fails))

    @staticmethod
    def _response(run, *, already: bool) -> WordPressPublicationRunPrepareResponse:
        return WordPressPublicationRunPrepareResponse(
            run_id=run.id,
            status=run.status,
            already_prepared=already,
            article_id=run.article_id,
            source_wordpress_draft_run_id=run.source_wordpress_draft_run_id,
            target_base_url=run.target_base_url,
            wordpress_post_id=run.wordpress_post_id,
            method=run.method,
            endpoint_path=run.endpoint_path,
            publish_payload_json=run.publish_payload_json,
            publish_payload_hash=run.publish_payload_hash,
            publication_request_identity_hash=run.publication_request_identity_hash,
            target_publication_request_identity_hash=(
                run.target_publication_request_identity_hash
            ),
            canonical_body_hash=run.canonical_body_hash,
            canonical_meta_hash=run.canonical_meta_hash,
            wordpress_raw_content_hash=run.wordpress_raw_content_hash,
            expected_pre_publish_status=run.expected_pre_publish_status,
            idempotency_key=run.idempotency_key,
            created_at=run.created_at,
        )
