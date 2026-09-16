"""WordPressContentUpdatePreflightService — D-D5C の READ-ONLY 分類境界。

「承認済み artifact の内容で、既に published 済みの WordPress post を update する
必要があるか」を **一切書き込まずに** 判定する。DB write 0、WordPress write 0。

2 段階の operation を公開する:

- :meth:`plan` — 完全に local のみ (WordPress へ一切通信しない)。全 local gate を
  検証し、live GET なしで判定できる範囲の **provisional** な分類
  (``PROVISIONAL_NOOP_CANDIDATE`` / ``PROVISIONAL_UPDATE_CANDIDATE``) を返す。
- :meth:`classify` — ``plan`` と同じ local gate をまず検証し (失敗すれば **0 回**
  の GET で即座に fail closed で返す)、全て通れば ``WordPressClient.get_post`` を
  **ちょうど 1 回だけ** 呼んで最終分類 (``CONTENT_NOOP`` / ``UPDATE_REQUIRED`` /
  ``WORDPRESS_CURRENT_CONTENT_DRIFT``) を確定する。

D-D5A.1 で確定した 2 つの hash namespace の分離をこの service でも厳密に守る:

- ``request_content_hash`` 系 (candidate / last successful submitted) — 意図した
  content の identity。
- ``*_wordpress_raw_content_hash`` 系 (expected / observed) — WordPress が実際に
  保存している content (``content.raw``) の identity。

この 2 つを等値比較することは一切ない。expected raw == observed raw の比較のみが
raw namespace 内の正当な比較であり、candidate request hash == last successful
request hash の比較のみが意図 namespace 内の正当な比較である。

D-D5C は **分類のみ**。``WordPressContentUpdateRun`` 行は一切作らない。将来の
D-D5D execution service が、この判定の ``UPDATE_REQUIRED`` 結果を根拠に
Transaction A を構築する権限を持つに過ぎない。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import get_settings
from app.models.wordpress_content_update_run import WP_CONTENT_UPDATE_RUNNING
from app.models.wordpress_draft_run import WP_RUN_SUCCEEDED
from app.models.wordpress_publication_run import WP_PUBRUN_SUCCEEDED
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.repositories.article_repository import ArticleRepository
from app.repositories.wordpress_content_update_run_repository import (
    WordPressContentUpdateRunRepository,
)
from app.repositories.wordpress_draft_run_repository import WordPressDraftRunRepository
from app.repositories.wordpress_publication_run_repository import (
    WordPressPublicationRunRepository,
)
from app.services.article_publication_artifact_inspection_service import (
    CURRENT_CANONICAL_MATCH,
    ArticlePublicationArtifactInspectionService,
)
from app.wordpress.client import WordPressClient
from app.wordpress.content_update_request import build_wordpress_content_update_request
from app.wordpress.target import canonicalize_wordpress_base_url

# -- 最終分類 (live GET 後のみ到達できる) -------------------------------------
CLASSIFICATION_CONTENT_NOOP = "CONTENT_NOOP"
CLASSIFICATION_UPDATE_REQUIRED = "UPDATE_REQUIRED"
CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT = "WORDPRESS_CURRENT_CONTENT_DRIFT"

# -- local-only provisional 分類 (live GET なし。最終判定ではない) -------------
PROVISIONAL_NOOP_CANDIDATE = "PROVISIONAL_NOOP_CANDIDATE"
PROVISIONAL_UPDATE_CANDIDATE = "PROVISIONAL_UPDATE_CANDIDATE"

# -- fail-closed reason code (local gate / network) ---------------------------
REASON_ARTICLE_NOT_FOUND = "ARTICLE_NOT_FOUND"
REASON_ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
REASON_ARTIFACT_ARTICLE_MISMATCH = "ARTIFACT_ARTICLE_MISMATCH"
REASON_ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
REASON_ARTIFACT_NOT_APPROVED = "ARTIFACT_NOT_APPROVED"
REASON_APPROVAL_HASH_MISMATCH = "APPROVAL_HASH_MISMATCH"
REASON_ARTIFACT_INVALID = "ARTIFACT_INVALID"
REASON_CURRENT_CANONICAL_DRIFT = "CURRENT_CANONICAL_DRIFT"
REASON_WORDPRESS_PUBLICATION_NOT_FOUND = "WORDPRESS_PUBLICATION_NOT_FOUND"
REASON_WORDPRESS_POST_ID_MISMATCH = "WORDPRESS_POST_ID_MISMATCH"
REASON_NO_WORDPRESS_RAW_BASELINE = "NO_WORDPRESS_RAW_BASELINE"
REASON_NO_SUBMITTED_CONTENT_BASELINE = "NO_SUBMITTED_CONTENT_BASELINE"
REASON_PRIOR_RUN_RUNNING = "PRIOR_RUN_RUNNING"
REASON_PRIOR_RUN_AMBIGUOUS = "PRIOR_RUN_AMBIGUOUS"
REASON_WORDPRESS_PREFLIGHT_FAILED = "WORDPRESS_PREFLIGHT_FAILED"
REASON_WORDPRESS_PREFLIGHT_POST_ID_MISMATCH = "WORDPRESS_PREFLIGHT_POST_ID_MISMATCH"
# D-D5C §20: 既に published 済みの前提を live 側でも要求する。draft/private/trash
# を黙って CONTENT_NOOP 相当として扱わない。
REASON_WORDPRESS_STATUS_MISMATCH = "WORDPRESS_STATUS_MISMATCH"

_EXPECTED_LIVE_STATUS = "publish"


@dataclass(frozen=True)
class ContentUpdateClassificationResult:
    """安全な事実のみを含む immutable な分類結果 (D-D5C §17)。

    ``tracked_html`` 全文・manifest 全文・token・credential は一切含まない。
    """

    article_id: int
    article_title: str | None
    wordpress_post_id: str | None

    artifact_id: int
    artifact_hash: str
    approved: bool

    current_canonical_status: str | None
    substitution_count: int | None

    candidate_request_content_hash: str | None
    last_successful_request_content_hash: str | None

    expected_pre_update_wordpress_raw_content_hash: str | None
    observed_pre_update_wordpress_raw_content_hash: str | None

    classification: str | None
    would_execute: bool
    reason_code: str

    target_base_url: str | None
    wordpress_status: str | None
    wordpress_modified_gmt_raw: str | None


@dataclass(frozen=True)
class _LocalContext:
    """全 local gate を通過した後にのみ構築される、live GET に必要な事実の束。"""

    article_id: int
    article_title: str
    artifact_id: int
    artifact_hash: str
    current_canonical_status: str
    substitution_count: int
    wordpress_post_id: str
    expected_pre_update_wordpress_raw_content_hash: str
    last_successful_request_content_hash: str
    candidate_request_content_hash: str
    target_base_url: str


class WordPressContentUpdatePreflightService:
    """READ-ONLY。``commit``/``flush``/``add`` を一度も呼ばない。"""

    def __init__(
        self, session: Session, *, wordpress_client: WordPressClient | None = None
    ) -> None:
        self._session = session
        self._wordpress_client = wordpress_client
        self._articles = ArticleRepository(session)
        self._artifacts = ArticlePublicationArtifactRepository(session)
        self._inspection = ArticlePublicationArtifactInspectionService(session)
        self._publication_runs = WordPressPublicationRunRepository(session)
        self._draft_runs = WordPressDraftRunRepository(session)
        self._content_update_runs = WordPressContentUpdateRunRepository(session)

    # -- local-only planning (0 network calls) ---------------------------
    def plan(
        self, *, article_id: int, artifact_id: int, artifact_hash: str
    ) -> ContentUpdateClassificationResult:
        ctx, blocked_result = self._resolve_local_context(
            article_id=article_id, artifact_id=artifact_id, artifact_hash=artifact_hash
        )
        if blocked_result is not None:
            return blocked_result

        assert ctx is not None  # for type-checkers; _resolve_local_context guarantees this
        provisional = (
            PROVISIONAL_NOOP_CANDIDATE
            if ctx.candidate_request_content_hash == ctx.last_successful_request_content_hash
            else PROVISIONAL_UPDATE_CANDIDATE
        )
        return self._result(
            ctx,
            observed_raw=None,
            wordpress_status=None,
            wordpress_modified_gmt_raw=None,
            classification=provisional,
            would_execute=False,
            reason_code=provisional,
        )

    # -- live classification (at most 1 WordPress GET) -------------------
    def classify(
        self, *, article_id: int, artifact_id: int, artifact_hash: str
    ) -> ContentUpdateClassificationResult:
        ctx, blocked_result = self._resolve_local_context(
            article_id=article_id, artifact_id=artifact_id, artifact_hash=artifact_hash
        )
        if blocked_result is not None:
            return blocked_result
        assert ctx is not None

        client = self._wordpress_client or WordPressClient(get_settings())
        try:
            pre = client.get_post(int(ctx.wordpress_post_id))
        except Exception:  # noqa: BLE001 - read-only preflight は fail closed で扱うだけ
            # D-D5C §19: read-only GET の失敗は outcome_unknown ではない -- 外部 state
            # を一切変化させないため、単純に fail closed する。retry は行わない。
            return self._result(
                ctx,
                observed_raw=None,
                wordpress_status=None,
                wordpress_modified_gmt_raw=None,
                classification=None,
                would_execute=False,
                reason_code=REASON_WORDPRESS_PREFLIGHT_FAILED,
            )

        returned_id = pre.get("id")
        if returned_id != int(ctx.wordpress_post_id):
            return self._result(
                ctx,
                observed_raw=None,
                wordpress_status=pre.get("status") if isinstance(pre.get("status"), str) else None,
                wordpress_modified_gmt_raw=_safe_str(pre.get("modified_gmt")),
                classification=None,
                would_execute=False,
                reason_code=REASON_WORDPRESS_PREFLIGHT_POST_ID_MISMATCH,
            )

        content_raw = _raw_only(pre.get("content"))
        wp_status = pre.get("status") if isinstance(pre.get("status"), str) else None
        wp_modified_gmt_raw = _safe_str(pre.get("modified_gmt"))

        if not content_raw:
            return self._result(
                ctx,
                observed_raw=None,
                wordpress_status=wp_status,
                wordpress_modified_gmt_raw=wp_modified_gmt_raw,
                classification=None,
                would_execute=False,
                reason_code=REASON_WORDPRESS_PREFLIGHT_FAILED,
            )

        if wp_status != _EXPECTED_LIVE_STATUS:
            return self._result(
                ctx,
                observed_raw=None,
                wordpress_status=wp_status,
                wordpress_modified_gmt_raw=wp_modified_gmt_raw,
                classification=None,
                would_execute=False,
                reason_code=REASON_WORDPRESS_STATUS_MISMATCH,
            )

        observed_raw = compute_text_hash(content_raw)

        # -- §13: raw namespace comparison only; short-circuits everything else --
        if observed_raw != ctx.expected_pre_update_wordpress_raw_content_hash:
            return self._result(
                ctx,
                observed_raw=observed_raw,
                wordpress_status=wp_status,
                wordpress_modified_gmt_raw=wp_modified_gmt_raw,
                classification=CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT,
                would_execute=False,
                reason_code=CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT,
            )

        # -- §14/§15: intended-content namespace comparison only ------------
        if ctx.candidate_request_content_hash == ctx.last_successful_request_content_hash:
            classification = CLASSIFICATION_CONTENT_NOOP
            would_execute = False
        else:
            classification = CLASSIFICATION_UPDATE_REQUIRED
            would_execute = True

        return self._result(
            ctx,
            observed_raw=observed_raw,
            wordpress_status=wp_status,
            wordpress_modified_gmt_raw=wp_modified_gmt_raw,
            classification=classification,
            would_execute=would_execute,
            reason_code=classification,
        )

    # -- local gates (§4-9); 0 network calls; returns (ctx, None) on success --
    # or (None, blocked_result) on the first failing gate.
    def _resolve_local_context(
        self, *, article_id: int, artifact_id: int, artifact_hash: str
    ) -> tuple[_LocalContext | None, ContentUpdateClassificationResult | None]:
        def blocked(reason_code: str, **overrides) -> ContentUpdateClassificationResult:
            base = dict(
                article_id=article_id,
                article_title=None,
                wordpress_post_id=None,
                artifact_id=artifact_id,
                artifact_hash=artifact_hash,
                approved=False,
                current_canonical_status=None,
                substitution_count=None,
                candidate_request_content_hash=None,
                last_successful_request_content_hash=None,
                expected_pre_update_wordpress_raw_content_hash=None,
                observed_pre_update_wordpress_raw_content_hash=None,
                classification=None,
                would_execute=False,
                reason_code=reason_code,
                target_base_url=None,
                wordpress_status=None,
                wordpress_modified_gmt_raw=None,
            )
            base.update(overrides)
            return ContentUpdateClassificationResult(**base)

        # -- §4: article exists ------------------------------------------
        article = self._articles.get_by_id(article_id)
        if article is None:
            return None, blocked(REASON_ARTICLE_NOT_FOUND)

        # -- §4: artifact exists ------------------------------------------
        artifact = self._artifacts.get_by_id(artifact_id)
        if artifact is None:
            return None, blocked(REASON_ARTIFACT_NOT_FOUND, article_title=article.title)

        if artifact.article_id != article_id:
            return None, blocked(REASON_ARTIFACT_ARTICLE_MISMATCH, article_title=article.title)
        if artifact.artifact_hash != artifact_hash:
            return None, blocked(REASON_ARTIFACT_HASH_MISMATCH, article_title=article.title)
        if artifact.approved_at is None:
            return None, blocked(REASON_ARTIFACT_NOT_APPROVED, article_title=article.title)
        if artifact.approved_artifact_hash != artifact_hash:
            return None, blocked(REASON_APPROVAL_HASH_MISMATCH, article_title=article.title)

        inspection = self._inspection.inspect(artifact_id)
        frozen_valid = (
            inspection.artifact_hash_valid
            and inspection.tracked_html_hash_valid
            and inspection.manifest_valid
            and inspection.strict_html_validation_valid
        )
        if not frozen_valid:
            return None, blocked(
                REASON_ARTIFACT_INVALID,
                article_title=article.title,
                approved=True,
                substitution_count=artifact.substitution_count,
            )
        if inspection.current_canonical_status != CURRENT_CANONICAL_MATCH:
            return None, blocked(
                REASON_CURRENT_CANONICAL_DRIFT,
                article_title=article.title,
                approved=True,
                current_canonical_status=inspection.current_canonical_status,
                substitution_count=artifact.substitution_count,
            )

        # -- §5: authoritative WordPress post id (Article.wordpress_post_id;
        # never a caller-supplied value) + supporting succeeded first-publication
        # evidence for the same article_id/wordpress_post_id -----------------
        succeeded_pubrun = _latest_with_status(
            self._publication_runs.list_by_article(article_id), WP_PUBRUN_SUCCEEDED
        )
        if succeeded_pubrun is None:
            return None, blocked(
                REASON_WORDPRESS_PUBLICATION_NOT_FOUND,
                article_title=article.title,
                approved=True,
                current_canonical_status=inspection.current_canonical_status,
                substitution_count=artifact.substitution_count,
            )
        if (
            article.wordpress_post_id is None
            or str(article.wordpress_post_id) != succeeded_pubrun.wordpress_post_id
        ):
            return None, blocked(
                REASON_WORDPRESS_POST_ID_MISMATCH,
                article_title=article.title,
                approved=True,
                current_canonical_status=inspection.current_canonical_status,
                substitution_count=artifact.substitution_count,
            )
        wp_post_id = succeeded_pubrun.wordpress_post_id

        # -- §6: blocking prior WordPressContentUpdateRun --------------------
        blocking_run = self._content_update_runs.find_blocking_run_for_post(
            article_id=article_id, wordpress_post_id=wp_post_id
        )
        if blocking_run is not None:
            reason = (
                REASON_PRIOR_RUN_RUNNING
                if blocking_run.status == WP_CONTENT_UPDATE_RUNNING
                else REASON_PRIOR_RUN_AMBIGUOUS
            )
            return None, blocked(
                reason,
                article_title=article.title,
                wordpress_post_id=wp_post_id,
                approved=True,
                current_canonical_status=inspection.current_canonical_status,
                substitution_count=artifact.substitution_count,
            )

        # -- §7: authoritative expected raw baseline --------------------------
        latest_succeeded_cur = self._content_update_runs.latest_succeeded_for_post(
            article_id=article_id, wordpress_post_id=wp_post_id
        )
        expected_raw = (
            latest_succeeded_cur.response_content_raw_hash
            if latest_succeeded_cur is not None
            else succeeded_pubrun.wordpress_raw_content_hash
        )
        if not expected_raw:
            return None, blocked(
                REASON_NO_WORDPRESS_RAW_BASELINE,
                article_title=article.title,
                wordpress_post_id=wp_post_id,
                approved=True,
                current_canonical_status=inspection.current_canonical_status,
                substitution_count=artifact.substitution_count,
            )

        # -- §8: last successfully submitted request-content hash (DIFFERENT
        # namespace from §7) --------------------------------------------------
        if latest_succeeded_cur is not None:
            last_submitted = latest_succeeded_cur.request_content_hash
        else:
            succeeded_draft = _latest_draft_for_post(
                self._draft_runs.list_by_article(article_id), wp_post_id
            )
            last_submitted = succeeded_draft.rendered_content_hash if succeeded_draft else None
        if not last_submitted:
            return None, blocked(
                REASON_NO_SUBMITTED_CONTENT_BASELINE,
                article_title=article.title,
                wordpress_post_id=wp_post_id,
                approved=True,
                current_canonical_status=inspection.current_canonical_status,
                substitution_count=artifact.substitution_count,
                expected_pre_update_wordpress_raw_content_hash=expected_raw,
            )

        # -- §9: candidate intended-content identity (pure D-D5B helper) -----
        settings = get_settings()
        target_base_url = canonicalize_wordpress_base_url(settings.wordpress_base_url or "")
        candidate = build_wordpress_content_update_request(
            wordpress_post_id=wp_post_id,
            article_publication_artifact_id=artifact.id,
            artifact_hash=artifact.artifact_hash,
            tracked_html=artifact.tracked_html,
            target_base_url=target_base_url,
        )

        return (
            _LocalContext(
                article_id=article_id,
                article_title=article.title,
                artifact_id=artifact_id,
                artifact_hash=artifact_hash,
                current_canonical_status=inspection.current_canonical_status,
                substitution_count=artifact.substitution_count,
                wordpress_post_id=wp_post_id,
                expected_pre_update_wordpress_raw_content_hash=expected_raw,
                last_successful_request_content_hash=last_submitted,
                candidate_request_content_hash=candidate.request_content_hash,
                target_base_url=target_base_url,
            ),
            None,
        )

    @staticmethod
    def _result(
        ctx: _LocalContext,
        *,
        observed_raw: str | None,
        wordpress_status: str | None,
        wordpress_modified_gmt_raw: str | None,
        classification: str | None,
        would_execute: bool,
        reason_code: str,
    ) -> ContentUpdateClassificationResult:
        return ContentUpdateClassificationResult(
            article_id=ctx.article_id,
            article_title=ctx.article_title,
            wordpress_post_id=ctx.wordpress_post_id,
            artifact_id=ctx.artifact_id,
            artifact_hash=ctx.artifact_hash,
            approved=True,
            current_canonical_status=ctx.current_canonical_status,
            substitution_count=ctx.substitution_count,
            candidate_request_content_hash=ctx.candidate_request_content_hash,
            last_successful_request_content_hash=ctx.last_successful_request_content_hash,
            expected_pre_update_wordpress_raw_content_hash=(
                ctx.expected_pre_update_wordpress_raw_content_hash
            ),
            observed_pre_update_wordpress_raw_content_hash=observed_raw,
            classification=classification,
            would_execute=would_execute,
            reason_code=reason_code,
            target_base_url=ctx.target_base_url,
            wordpress_status=wordpress_status,
            wordpress_modified_gmt_raw=wordpress_modified_gmt_raw,
        )


def _latest_with_status(runs, status: str):
    for run in runs:  # already ordered newest-first by the repository
        if run.status == status:
            return run
    return None


def _latest_draft_for_post(runs, wordpress_post_id: str):
    for run in runs:  # already ordered newest-first by the repository
        if run.status == WP_RUN_SUCCEEDED and run.wordpress_post_id == wordpress_post_id:
            return run
    return None


def _raw_only(field: object) -> str | None:
    if isinstance(field, dict):
        v = field.get("raw")
        return v if isinstance(v, str) else None
    return field if isinstance(field, str) else None


def _safe_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
