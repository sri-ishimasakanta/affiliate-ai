"""WordPressPublicationRun の prepare / execute オーケストレーション (transaction owner)。

prepare:
  Article / source WordPressDraftRun を読み、全 guard を検証し、exact な publish request
  (``{"status":"publish"}`` → ``POST /wp-json/wp/v2/posts/{id}``) と 3 つの identity hash を
  計算し、1 件の ``prepared`` run を append して 1 transaction で commit する。WordPress へは
  一切通信しない。

execute (実 WordPress publish; 初回 public 公開):
  全 guard 再検証 → 1 回だけの read-only preflight GET (post が draft のまま / 内容 drift
  なし) → prepared -> running を **外部 POST の前に単独 commit** → WordPress へ厳密に 1 回
  だけ publish POST → 成功なら 1 回だけ read-back GET → 両方確認できたら running -> succeeded
  + Article approved -> published を 1 transaction で commit。自動リトライは一切しない。
  外部成功後にローカルが失敗した場合は再 POST せず reconciliation を要求する。

credential (username / app password) は読まない・保存しない・出力しない。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import to_storage_utc
from app.config.settings import Settings, get_settings
from app.exceptions import (
    EntityNotFoundError,
    ExternalProviderError,
    WordPressAmbiguousOutcomeError,
    WordPressAmbiguousPublishOutcomeError,
    WordPressPublicationExternalSuccessLocalPersistFailedError,
    WordPressPublicationPreflightError,
    WordPressPublicationReadbackFailedError,
    WordPressPublicationRunConflictError,
    WordPressPublicationRunExecutionError,
    WordPressPublicationRunPreparationError,
)
from app.models.enums import ArticleStatus
from app.models.wordpress_draft_run import WP_RUN_SUCCEEDED
from app.models.wordpress_publication_run import WP_PUBRUN_PREPARED, WP_PUBRUN_RUNNING
from app.repositories.article_repository import ArticleRepository
from app.repositories.wordpress_draft_run_repository import WordPressDraftRunRepository
from app.repositories.wordpress_publication_run_repository import (
    WordPressPublicationRunRepository,
)
from app.services.status_transitions import ARTICLE_TRANSITIONS, ensure_transition_allowed
from app.wordpress.client import WordPressClient
from app.wordpress.publish_request import (
    V1_EXPECTED_PRE_PUBLISH_STATUS,
    V1_PUBLISH_STATUS,
    build_wordpress_publish_request,
    compute_publish_payload_hash,
)
from app.wordpress.schemas import (
    WordPressPublicationRunExecuteResponse,
    WordPressPublicationRunPrepareResponse,
)
from app.wordpress.target import canonicalize_wordpress_base_url

_ARTICLE = "Article"
_DRAFT_RUN = "WordPressDraftRun"
_PUBRUN = "WordPressPublicationRun"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class WordPressPublicationRunService:
    def __init__(
        self, session: Session, *, wordpress_client: WordPressClient | None = None
    ) -> None:
        self._session = session
        # 注入時はそれを使う (テスト用)。None なら execute() 時に settings から遅延生成。
        self._wordpress_client = wordpress_client
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

    # -- execute (実 WordPress publish; transaction owner) --------------
    def execute(
        self,
        article_id: int,
        run_id: int,
        *,
        expected_target_publication_request_identity_hash: str,
    ) -> WordPressPublicationRunExecuteResponse:
        run = self._repo.get_by_id(run_id)
        if run is None or run.article_id != article_id:
            raise EntityNotFoundError(_PUBRUN, run_id)

        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError(_ARTICLE, article_id)

        # -- running-state / terminal recovery rule (§25) --------------
        if run.status == WP_PUBRUN_RUNNING:
            raise WordPressPublicationRunExecutionError(
                f"run {run.id} is already running; recovery required, no automatic retry"
            )
        if run.status != WP_PUBRUN_PREPARED:
            raise WordPressPublicationRunExecutionError(
                f"run {run.id}: status={run.status!r} is not executable; a new run "
                "requires explicit Human reconciliation"
            )

        settings = get_settings()
        source_run = self._draft_runs.get_by_id(run.source_wordpress_draft_run_id)
        self._assert_execute_gates(
            article=article,
            run=run,
            source_run=source_run,
            settings=settings,
            expected_target_publication_request_identity_hash=(
                expected_target_publication_request_identity_hash
            ),
        )

        client = self._wordpress_client or WordPressClient(settings)
        wp_post_id = int(run.wordpress_post_id)

        # -- mandatory execute preflight GET (§12); no DB write yet -----
        pre = client.get_post(wp_post_id)
        self._assert_preflight(pre, article=article, run=run)

        # -- local state BEFORE the external publish (§13): own commit --
        self._repo.mark_running(run, started_at=to_storage_utc(datetime.now(UTC)))
        self._session.commit()
        self._session.refresh(run)

        # -- exactly ONE publish POST (§14); no retry -----------------
        try:
            published = client.publish_existing_post_exact(
                wp_post_id, run.publish_payload_json
            )
        except WordPressAmbiguousOutcomeError as exc:
            # taxonomy A (§17): outcome unknown -> failed, no retry
            self._repo.mark_failed(
                run,
                error_message=f"ambiguous_wordpress_publish_outcome: {exc}",
                finished_at=to_storage_utc(datetime.now(UTC)),
            )
            self._session.commit()
            raise WordPressAmbiguousPublishOutcomeError(
                "publish POST outcome could not be confirmed"
            ) from exc
        except ExternalProviderError as exc:
            # taxonomy §18 (definite HTTP failure): -> failed, no retry
            self._repo.mark_failed(
                run,
                error_message=f"wordpress publish failed: {exc}",
                finished_at=to_storage_utc(datetime.now(UTC)),
            )
            self._session.commit()
            raise

        # -- mandatory read-back GET (§19) ---------------------------
        try:
            rb = client.get_post(wp_post_id)
        except (WordPressAmbiguousOutcomeError, ExternalProviderError) as exc:
            # taxonomy B (§20): publish already confirmed; read-back failed.
            # durable run stays running. No second POST.
            raise WordPressPublicationReadbackFailedError(run.wordpress_post_id) from exc

        self._assert_readback(rb, article=article, run=run)

        canonical_url = self._canonical_public_url(rb, published, base=run.target_base_url)
        published_at, published_at_source = self._resolve_published_at(rb, published)

        # -- ONE local finalization transaction (§23) -----------------
        response_snapshot = {
            "id": published.id,
            "status": V1_PUBLISH_STATUS,
            "link": canonical_url,
            "date_gmt": rb.get("date_gmt") or published.date_gmt,
        }
        try:
            self._repo.mark_succeeded(
                run,
                wordpress_post_status=V1_PUBLISH_STATUS,
                wordpress_post_url=canonical_url,
                published_at_source=published_at_source,
                response_snapshot=response_snapshot,
                finished_at=to_storage_utc(datetime.now(UTC)),
            )
            ensure_transition_allowed(
                _ARTICLE,
                ArticleStatus.APPROVED,
                ArticleStatus.PUBLISHED,
                ARTICLE_TRANSITIONS,
            )
            self._articles.update(
                article,
                {
                    "status": ArticleStatus.PUBLISHED.value,
                    "published_url": canonical_url,
                    "published_at": published_at,
                },
            )
            self._session.commit()
        except Exception as exc:
            # taxonomy C (§24): external publish + read-back confirmed; local failed.
            self._session.rollback()
            raise WordPressPublicationExternalSuccessLocalPersistFailedError(
                run.wordpress_post_id
            ) from exc

        self._session.refresh(run)
        self._session.refresh(article)
        return self._execute_response(run, article)

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

    # -- execute guards / preflight / read-back -------------------------
    def _assert_execute_gates(
        self,
        *,
        article,
        run,
        source_run,
        settings: Settings,
        expected_target_publication_request_identity_hash: str,
    ) -> None:
        fails: list[str] = []

        if str(article.status) != "approved":
            fails.append(f"Article.status={article.status!r} (expected approved)")
        if article.wordpress_post_id is None:
            fails.append("Article.wordpress_post_id is null")
        elif str(article.wordpress_post_id) != run.wordpress_post_id:
            fails.append(
                f"Article.wordpress_post_id={article.wordpress_post_id!r} != "
                f"run.wordpress_post_id={run.wordpress_post_id!r}"
            )
        if article.published_url is not None:
            fails.append("Article.published_url is not null")
        if article.published_at is not None:
            fails.append("Article.published_at is not null")

        if (
            run.target_publication_request_identity_hash
            != expected_target_publication_request_identity_hash
        ):
            fails.append(
                "target_publication_request_identity_hash does not match the "
                "caller-approved value"
            )
        if run.expected_pre_publish_status != V1_EXPECTED_PRE_PUBLISH_STATUS:
            fails.append(
                f"run.expected_pre_publish_status={run.expected_pre_publish_status!r} "
                "(expected draft)"
            )

        body_hash = compute_text_hash(article.body or "")
        meta_hash = compute_text_hash(article.meta_description or "")
        if body_hash != run.canonical_body_hash:
            fails.append("current Article body hash has drifted from the prepared run")
        if meta_hash != run.canonical_meta_hash:
            fails.append("current Article meta hash has drifted from the prepared run")

        # payload byte / identity integrity
        if compute_publish_payload_hash(run.publish_payload_json) != run.publish_payload_hash:
            fails.append("stored publish_payload_json no longer matches publish_payload_hash")
        try:
            parsed = json.loads(run.publish_payload_json)
        except ValueError:
            parsed = None
        if parsed != {"status": V1_PUBLISH_STATUS}:
            fails.append("stored publish_payload_json is not exactly {\"status\":\"publish\"}")

        rebuilt = build_wordpress_publish_request(
            article_id=article.id,
            source_wordpress_draft_run_id=run.source_wordpress_draft_run_id,
            wordpress_post_id=run.wordpress_post_id,
            target_base_url=run.target_base_url,
            canonical_body_hash=run.canonical_body_hash,
            canonical_meta_hash=run.canonical_meta_hash,
            wordpress_raw_content_hash=run.wordpress_raw_content_hash,
            expected_pre_publish_status=run.expected_pre_publish_status,
        )
        if rebuilt.publish_payload_hash != run.publish_payload_hash:
            fails.append("recomputed publish_payload_hash drifted from the prepared run")
        if rebuilt.publication_request_identity_hash != run.publication_request_identity_hash:
            fails.append(
                "recomputed publication_request_identity_hash drifted from the prepared run"
            )
        if (
            rebuilt.target_publication_request_identity_hash
            != run.target_publication_request_identity_hash
        ):
            fails.append(
                "recomputed target_publication_request_identity_hash drifted from the "
                "prepared run"
            )
        if run.method != "POST":
            fails.append(f"run.method {run.method!r} != 'POST'")
        if run.endpoint_path != f"/wp-json/wp/v2/posts/{run.wordpress_post_id}":
            fails.append(f"run.endpoint_path {run.endpoint_path!r} unexpected")

        # source draft run
        if source_run is None:
            fails.append("source WordPressDraftRun not found")
        else:
            if source_run.article_id != article.id:
                fails.append("source WordPressDraftRun.article_id does not match Article.id")
            if source_run.status != WP_RUN_SUCCEEDED:
                fails.append(
                    f"source WordPressDraftRun.status={source_run.status!r} "
                    "(expected succeeded)"
                )
            if source_run.wordpress_post_id != run.wordpress_post_id:
                fails.append(
                    "source WordPressDraftRun.wordpress_post_id does not match run"
                )
            if source_run.wordpress_post_status != "draft":
                fails.append(
                    "source WordPressDraftRun.wordpress_post_status is not draft"
                )
            if source_run.error_message is not None:
                fails.append("source WordPressDraftRun.error_message is not null")

        if not settings.wordpress_configured:
            fails.append("wordpress_configured is false")
        else:
            configured = canonicalize_wordpress_base_url(settings.wordpress_base_url or "")
            if not configured.startswith("https://"):
                fails.append("configured target is not https")
            if configured != run.target_base_url:
                fails.append(
                    f"configured target {configured!r} != run target "
                    f"{run.target_base_url!r}"
                )

        if fails:
            raise WordPressPublicationRunExecutionError("; ".join(fails))

    @staticmethod
    def _assert_preflight(pre: dict, *, article, run) -> None:
        fails: list[str] = []
        pid = pre.get("id")
        status = pre.get("status")
        if pid != int(run.wordpress_post_id):
            fails.append(f"preflight post id {pid!r} != {run.wordpress_post_id!r}")
        if status == V1_PUBLISH_STATUS:
            raise WordPressPublicationPreflightError(
                "WordPress post is already published; STOP for Human reconciliation"
            )
        if status != run.expected_pre_publish_status:
            fails.append(f"preflight status {status!r} != {run.expected_pre_publish_status!r}")

        title = _raw_or_rendered(pre.get("title"))
        if title != article.title:
            fails.append("preflight title does not match Article.title")
        slug = pre.get("slug")
        if not isinstance(slug, str) or unquote(slug) != article.slug:
            fails.append("preflight slug does not match Article.slug")
        excerpt = _raw_or_rendered(pre.get("excerpt"))
        if excerpt != article.meta_description:
            fails.append("preflight excerpt does not match Article.meta_description")

        content_raw = _raw_only(pre.get("content"))
        if not content_raw:
            fails.append("preflight content.raw is missing")
        else:
            if compute_text_hash(content_raw) != run.wordpress_raw_content_hash:
                fails.append("preflight content.raw hash has drifted from the approved value")
        cats = pre.get("categories")
        if not isinstance(cats, list) or len(cats) < 1:
            fails.append("preflight has no category assigned")

        if fails:
            raise WordPressPublicationPreflightError("; ".join(fails))

    @staticmethod
    def _assert_readback(rb: dict, *, article, run) -> None:
        pid = rb.get("id")
        status = rb.get("status")
        if pid != int(run.wordpress_post_id) or status != V1_PUBLISH_STATUS:
            raise WordPressPublicationReadbackFailedError(run.wordpress_post_id)
        title = _raw_or_rendered(rb.get("title"))
        slug = rb.get("slug")
        if title != article.title or not isinstance(slug, str) or unquote(slug) != article.slug:
            raise WordPressPublicationReadbackFailedError(run.wordpress_post_id)
        content_raw = _raw_only(rb.get("content"))
        if not content_raw or compute_text_hash(content_raw) != run.wordpress_raw_content_hash:
            raise WordPressPublicationReadbackFailedError(run.wordpress_post_id)

    @staticmethod
    def _canonical_public_url(rb: dict, published, *, base: str) -> str:
        link = rb.get("link") or published.link
        if not isinstance(link, str):
            raise WordPressPublicationReadbackFailedError("unknown")
        parts = urlsplit(link)
        if (
            parts.scheme != "https"
            or parts.hostname != urlsplit(base).hostname
            or parts.username
            or parts.password
        ):
            raise WordPressPublicationReadbackFailedError("unknown")
        return link

    @staticmethod
    def _resolve_published_at(rb: dict, published) -> tuple[datetime, str]:
        raw = rb.get("date_gmt") or published.date_gmt
        if isinstance(raw, str) and raw.strip():
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
                return to_storage_utc(dt), "wordpress_date_gmt"
            except ValueError:
                pass
        return to_storage_utc(datetime.now(UTC)), "local_confirmed_success_utc"

    @staticmethod
    def _execute_response(run, article) -> WordPressPublicationRunExecuteResponse:
        return WordPressPublicationRunExecuteResponse(
            run_id=run.id,
            status=run.status,
            article_id=run.article_id,
            target_base_url=run.target_base_url,
            target_publication_request_identity_hash=(
                run.target_publication_request_identity_hash
            ),
            wordpress_post_id=run.wordpress_post_id,
            wordpress_post_status=run.wordpress_post_status,
            wordpress_post_url=run.wordpress_post_url,
            published_at_source=run.published_at_source,
            article_status=article.status,
            article_published_url=article.published_url,
            article_published_at=article.published_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

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


def _raw_or_rendered(field: object) -> object:
    if isinstance(field, dict):
        return field.get("raw") if field.get("raw") is not None else field.get("rendered")
    return field


def _raw_only(field: object) -> str | None:
    if isinstance(field, dict):
        v = field.get("raw")
        return v if isinstance(v, str) else None
    return field if isinstance(field, str) else None
