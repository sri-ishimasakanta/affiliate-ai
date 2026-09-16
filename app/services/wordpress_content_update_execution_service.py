"""WordPressContentUpdateExecutionService — D-D5D の write 境界 (transaction owner)。

D-D5C の :class:`WordPressContentUpdatePreflightService` を **そのまま再利用** する
(local gate / live GET / CONTENT_NOOP / UPDATE_REQUIRED / WORDPRESS_CURRENT_CONTENT_DRIFT
の分類ロジックを一切複製しない)。1 回の ``execute()`` 呼び出しにつき、``classify()``
を **必ずこの呼び出しの中で新規に** 実行する -- 以前の CLI 呼び出しが返した
classification 結果を信用しない。

execute() の contract:

    fresh classify() (D-D5C, 内部で高々 1 回の GET)
      -> CONTENT_NOOP / WORDPRESS_CURRENT_CONTENT_DRIFT / local gate 不通過:
         run を一切作らない。WordPress write 0。ContentUpdateExecutionResult を返す。
      -> UPDATE_REQUIRED:
         Transaction A (running 行を insert して commit -- POST の前に確定)
         -> 厳密に 1 回だけ content-update POST
         -> mandatory read-back GET
         -> Transaction B (succeeded / outcome_unknown を mark して commit)

自動リトライは一切しない。

``running`` の意味 (D-D5A.1/D-D5B.1 から変更なし): 「WordPress へ request が実際に
送信済みであることの証明」ではない -- durable に記録されるのは preflight 完了後・
network-write 境界の **直前** の update 試行/意図。Transaction A commit 後・実際の
HTTP 送信前に crash する window が理論上あるため、``running`` は意図的に
blocking/ambiguous なまま。

failed vs outcome_unknown (D-D5D §17): 既存の :class:`WordPressClient` の契約上、
``ExternalProviderError`` は 401/403/redirect-block/その他の unexpected status を
すべて同じ例外型 (メッセージ文字列でしか区別できない) に集約している。この service は
「zero effect を証明できる」既知の狭い分離が client 契約から得られない限り、
conservative に ``outcome_unknown`` として扱う -- ``failed`` へ遷移する経路はこの
実装には **存在しない** (詳細は module 末尾のコメントを参照)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import to_storage_utc
from app.config.settings import get_settings
from app.exceptions import (
    ExternalProviderError,
    WordPressAmbiguousOutcomeError,
    WordPressContentUpdateRunError,
    WordPressContentUpdateTerminalPersistFailedError,
)
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.repositories.wordpress_content_update_run_repository import (
    WordPressContentUpdateRunRepository,
)
from app.services.wordpress_content_update_preflight_service import (
    CLASSIFICATION_UPDATE_REQUIRED,
    ContentUpdateClassificationResult,
    WordPressContentUpdatePreflightService,
)
from app.wordpress.client import WordPressClient
from app.wordpress.content_update_request import build_wordpress_content_update_request

_EXPECTED_LIVE_STATUS = "publish"

# -- outcome/reason codes (execution-specific; distinct from D-D5C's classification
# reason codes, which remain untouched and are surfaced verbatim as preflight_*). ---
OUTCOME_NOT_EXECUTED = "NOT_EXECUTED"
OUTCOME_SUCCEEDED = "SUCCEEDED"
OUTCOME_OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True)
class ContentUpdateExecutionResult:
    """安全な事実のみを含む immutable な実行結果 (D-D5D §25)。

    ``tracked_html`` 全文・manifest 全文・token・credential は一切含まない。
    """

    article_id: int
    wordpress_post_id: str | None

    artifact_id: int
    artifact_hash: str

    preflight_classification: str | None

    executed: bool
    run_id: int | None
    run_status: str | None

    request_content_hash: str | None
    expected_pre_update_wordpress_raw_content_hash: str | None
    observed_pre_update_wordpress_raw_content_hash: str | None
    response_content_raw_hash: str | None

    wordpress_status: str | None
    reason_code: str


class WordPressContentUpdateExecutionService:
    def __init__(
        self, session: Session, *, wordpress_client: WordPressClient | None = None
    ) -> None:
        self._session = session
        self._wordpress_client = wordpress_client
        self._artifacts = ArticlePublicationArtifactRepository(session)
        self._runs = WordPressContentUpdateRunRepository(session)

    def execute(
        self,
        *,
        article_id: int,
        artifact_id: int,
        artifact_hash: str,
        idempotency_key: str | None = None,
    ) -> ContentUpdateExecutionResult:
        client = self._wordpress_client or WordPressClient(get_settings())

        # -- D-D5C reuse: fresh classification, obtained inside THIS call ----
        preflight = WordPressContentUpdatePreflightService(
            self._session, wordpress_client=client
        )
        classification = preflight.classify(
            article_id=article_id, artifact_id=artifact_id, artifact_hash=artifact_hash
        )

        if classification.classification != CLASSIFICATION_UPDATE_REQUIRED:
            # CONTENT_NOOP / WORDPRESS_CURRENT_CONTENT_DRIFT / any local-gate fail-closed
            # reason -- no run, no POST (D-D5D §5-6).
            return self._not_executed_result(classification)

        # -- §7: re-require the frozen evidence before Transaction A ---------
        # (classify() already enforced expected==observed raw as part of reaching
        # UPDATE_REQUIRED; re-verify defensively rather than trust a stale belief.)
        if (
            classification.expected_pre_update_wordpress_raw_content_hash is None
            or classification.observed_pre_update_wordpress_raw_content_hash is None
            or classification.expected_pre_update_wordpress_raw_content_hash
            != classification.observed_pre_update_wordpress_raw_content_hash
        ):
            raise WordPressContentUpdateRunError(
                "UPDATE_REQUIRED classification is missing the required raw-baseline "
                "equality evidence; refusing to construct Transaction A"
            )
        if classification.wordpress_post_id is None or classification.target_base_url is None:
            raise WordPressContentUpdateRunError(
                "UPDATE_REQUIRED classification is missing wordpress_post_id/"
                "target_base_url; refusing to construct Transaction A"
            )

        # -- §9/§11: rebuild the exact candidate request material via the D-D5B
        # pure helper (not a reimplementation of D-D5C's gate/baseline logic --
        # a pure, deterministic recomputation from already-validated inputs, with
        # a defense-in-depth equality check against the classification result). --
        artifact = self._artifacts.get_by_id(artifact_id)
        if artifact is None:
            raise WordPressContentUpdateRunError(
                "artifact disappeared between classification and execution"
            )
        candidate = build_wordpress_content_update_request(
            wordpress_post_id=classification.wordpress_post_id,
            article_publication_artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            tracked_html=artifact.tracked_html,
            target_base_url=classification.target_base_url,
        )
        if candidate.request_content_hash != classification.candidate_request_content_hash:
            raise WordPressContentUpdateRunError(
                "recomputed request_content_hash drifted from the fresh classification "
                "result; refusing to construct Transaction A"
            )

        # D-D5D.1: a historical succeeded run sharing this exact target identity is
        # deliberately NOT treated as a duplicate-execution veto here. Historical
        # success proves nothing about CURRENT WordPress state -- only the fresh
        # D-D5C classification above is authoritative about that (D-D5D.1 §3). The
        # legitimate A -> B -> A restoration case must still be able to execute: if
        # content A previously succeeded, was superseded by B, and a Human now wants
        # to restore A, fresh classify() correctly reports UPDATE_REQUIRED (current
        # raw matches B's baseline, candidate A != B's last-submitted hash) and this
        # service must honor that, not silently suppress the POST because *some*
        # past run happens to share A's identity. The correct, current-state-aware
        # duplicate protection is CONTENT_NOOP itself (D-D5D.1 §9), which already ran
        # as part of the fresh classification above.

        # -- §21: idempotency prelookup (before Transaction A; no POST yet) ----
        if idempotency_key is not None:
            existing = self._runs.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.content_update_request_identity_hash
                    == candidate.content_update_request_identity_hash
                ):
                    return self._existing_result(
                        existing, classification, reason_code=existing.status.upper()
                    )
                raise WordPressContentUpdateRunError(
                    f"idempotency_key {idempotency_key!r} already used for a different "
                    "content-update request identity"
                )

        # -- Transaction A: durable running row, committed BEFORE the POST -----
        # No try/except here by design: if this commit fails, the exception
        # propagates naturally and the POST section below is never reached
        # (matches WordPressPublicationRunService.execute()'s identical pattern
        # for its own prepared->running transition).
        #
        # D-D5D.2: commit is the LAST local DB operation at this boundary. No
        # post-commit refresh/query -- WordPressContentUpdateRunRepository.
        # add_running() already flush()es internally, and SessionLocal is built
        # with expire_on_commit=False (app/config/database.py), so `run`'s
        # in-memory attributes (including server-generated id/created_at, via
        # flush()'s RETURNING) are already correct and do not go stale on
        # commit. A post-commit refresh() here would be an unnecessary fallible
        # DB round-trip sitting between the durable running intent and the
        # network-write boundary -- if it failed, it would raise an unrelated,
        # undocumented exception even though Transaction A succeeded and no
        # POST had been attempted yet. Removed (D-D5D.2 fix).
        started_at = to_storage_utc(datetime.now(UTC))
        run = self._runs.add_running(
            article_id=article_id,
            wordpress_post_id=classification.wordpress_post_id,
            article_publication_artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            method=candidate.method,
            endpoint_path=candidate.endpoint_path,
            update_payload_json=candidate.update_payload_json,
            update_payload_hash=candidate.update_payload_hash,
            content_update_request_identity_hash=candidate.content_update_request_identity_hash,
            target_content_update_request_identity_hash=(
                candidate.target_content_update_request_identity_hash
            ),
            target_base_url=classification.target_base_url,
            request_content_hash=candidate.request_content_hash,
            expected_pre_update_wordpress_raw_content_hash=(
                classification.expected_pre_update_wordpress_raw_content_hash
            ),
            observed_pre_update_wordpress_raw_content_hash=(
                classification.observed_pre_update_wordpress_raw_content_hash
            ),
            observed_pre_update_modified_gmt_raw=classification.wordpress_modified_gmt_raw,
            idempotency_key=idempotency_key,
            started_at=started_at,
        )
        self._session.commit()

        wp_post_id_int = int(classification.wordpress_post_id)

        # -- exactly ONE content-update POST; no retry ------------------------
        try:
            posted = client.update_post_content_exact(
                wp_post_id_int, candidate.update_payload_json
            )
        except (WordPressAmbiguousOutcomeError, ExternalProviderError) as exc:
            # D-D5D §17: this client's ExternalProviderError conflates every
            # non-2xx case (401/403/redirect-block/other) into one type with no
            # structural zero-effect proof -- classify conservatively as
            # outcome_unknown, never failed.
            self._mark_outcome_unknown(
                run, error_message=f"wordpress content update outcome unknown: {exc}"
            )
            return self._executed_result(run, classification, OUTCOME_OUTCOME_UNKNOWN)

        # -- §18: POST "succeeded" at the transport/HTTP layer but returned an
        # unexpected post id -- cannot prove zero effect after a write request. --
        if posted.id != wp_post_id_int:
            self._mark_outcome_unknown(
                run,
                error_message=(
                    f"post-write response post id {posted.id!r} != authoritative "
                    f"{wp_post_id_int!r}"
                ),
            )
            return self._executed_result(run, classification, OUTCOME_OUTCOME_UNKNOWN)

        # -- §14/§19: mandatory read-back GET; failure -> outcome_unknown, no retry --
        try:
            rb = client.get_post(wp_post_id_int)
        except Exception as exc:  # noqa: BLE001 - read-back failure is itself evidence
            self._mark_outcome_unknown(
                run, error_message=f"post-write read-back failed: {exc}"
            )
            return self._executed_result(run, classification, OUTCOME_OUTCOME_UNKNOWN)

        rb_id = rb.get("id")
        rb_status = rb.get("status") if isinstance(rb.get("status"), str) else None
        rb_content_raw = _raw_only(rb.get("content"))
        rb_content_rendered = _rendered_only(rb.get("content"))
        rb_modified_gmt_raw = _safe_str(rb.get("modified_gmt"))

        if (
            rb_id != wp_post_id_int
            or rb_status != _EXPECTED_LIVE_STATUS
            or not rb_content_raw
        ):
            self._mark_outcome_unknown(
                run,
                error_message=(
                    "post-write read-back malformed/unverifiable "
                    f"(id={rb_id!r}, status={rb_status!r}, raw_present={bool(rb_content_raw)})"
                ),
            )
            return self._executed_result(run, classification, OUTCOME_OUTCOME_UNKNOWN)

        # -- §15/§16: success. response_content_raw_hash is NORMATIVE (becomes the
        # next operation's expected baseline) but is never asserted equal to
        # request_content_hash -- different representations (D-D5A.1 §3). --
        response_content_raw_hash = compute_text_hash(rb_content_raw)
        response_content_rendered_hash = (
            compute_text_hash(rb_content_rendered) if rb_content_rendered else None
        )
        wordpress_modified_at = _parse_modified_gmt(rb_modified_gmt_raw)

        self._mark_succeeded(
            run,
            http_status=200,
            response_content_raw_hash=response_content_raw_hash,
            response_content_rendered_hash=response_content_rendered_hash,
            wordpress_modified_at=wordpress_modified_at,
            wordpress_modified_gmt_raw=rb_modified_gmt_raw,
            response_snapshot={
                "id": rb_id,
                "status": rb_status,
                "link": rb.get("link") if isinstance(rb.get("link"), str) else None,
                "modified_gmt": rb_modified_gmt_raw,
            },
        )
        return self._executed_result(run, classification, OUTCOME_SUCCEEDED)

    # -- Transaction B helpers ------------------------------------------------
    def _mark_outcome_unknown(self, run, *, error_message: str) -> None:
        finished_at = to_storage_utc(datetime.now(UTC))
        self._runs.mark_outcome_unknown(run, error_message=error_message, finished_at=finished_at)
        self._commit_transaction_b(run)

    def _mark_succeeded(
        self,
        run,
        *,
        http_status: int,
        response_content_raw_hash: str,
        response_content_rendered_hash: str | None,
        wordpress_modified_at,
        wordpress_modified_gmt_raw: str | None,
        response_snapshot: dict,
    ) -> None:
        finished_at = to_storage_utc(datetime.now(UTC))
        self._runs.mark_succeeded(
            run,
            http_status=http_status,
            response_content_raw_hash=response_content_raw_hash,
            response_content_rendered_hash=response_content_rendered_hash,
            wordpress_modified_at=wordpress_modified_at,
            wordpress_modified_gmt_raw=wordpress_modified_gmt_raw,
            response_snapshot=response_snapshot,
            finished_at=finished_at,
        )
        self._commit_transaction_b(run)

    def _commit_transaction_b(self, run) -> None:
        # D-D5D.2: commit is the LAST local DB operation for Transaction B. No
        # post-commit refresh -- mark_succeeded()/mark_outcome_unknown() already
        # flush() internally, and expire_on_commit=False means `run`'s in-memory
        # attributes already reflect exactly what was committed. A post-commit
        # refresh() here would be an unnecessary fallible DB round-trip that, if
        # it failed, would raise an unrelated, undocumented exception even
        # though the terminal outcome was already durably recorded -- durable
        # state and surfaced outcome must never be allowed to disagree. Removed
        # (D-D5D.2 fix); a genuine commit failure is still handled below exactly
        # as before.
        try:
            self._session.commit()
        except Exception as exc:
            # D-D5D §20/§34: external write boundary already crossed; the terminal
            # outcome is determined but could not be durably recorded. Never retry
            # the POST. Durable row remains `running`. Loudly surface this.
            self._session.rollback()
            raise WordPressContentUpdateTerminalPersistFailedError(
                run.wordpress_post_id
            ) from exc

    # -- result builders --------------------------------------------------
    @staticmethod
    def _not_executed_result(
        classification: ContentUpdateClassificationResult,
    ) -> ContentUpdateExecutionResult:
        return ContentUpdateExecutionResult(
            article_id=classification.article_id,
            wordpress_post_id=classification.wordpress_post_id,
            artifact_id=classification.artifact_id,
            artifact_hash=classification.artifact_hash,
            preflight_classification=classification.classification,
            executed=False,
            run_id=None,
            run_status=None,
            request_content_hash=classification.candidate_request_content_hash,
            expected_pre_update_wordpress_raw_content_hash=(
                classification.expected_pre_update_wordpress_raw_content_hash
            ),
            observed_pre_update_wordpress_raw_content_hash=(
                classification.observed_pre_update_wordpress_raw_content_hash
            ),
            response_content_raw_hash=None,
            wordpress_status=classification.wordpress_status,
            reason_code=classification.reason_code,
        )

    @staticmethod
    def _existing_result(
        run,
        classification: ContentUpdateClassificationResult,
        *,
        reason_code: str,
    ) -> ContentUpdateExecutionResult:
        return ContentUpdateExecutionResult(
            article_id=classification.article_id,
            wordpress_post_id=run.wordpress_post_id,
            artifact_id=classification.artifact_id,
            artifact_hash=classification.artifact_hash,
            preflight_classification=classification.classification,
            executed=False,
            run_id=run.id,
            run_status=run.status,
            request_content_hash=run.request_content_hash,
            expected_pre_update_wordpress_raw_content_hash=(
                run.expected_pre_update_wordpress_raw_content_hash
            ),
            observed_pre_update_wordpress_raw_content_hash=(
                run.observed_pre_update_wordpress_raw_content_hash
            ),
            response_content_raw_hash=run.response_content_raw_hash,
            wordpress_status=classification.wordpress_status,
            reason_code=reason_code,
        )

    @staticmethod
    def _executed_result(
        run,
        classification: ContentUpdateClassificationResult,
        reason_code: str,
    ) -> ContentUpdateExecutionResult:
        return ContentUpdateExecutionResult(
            article_id=classification.article_id,
            wordpress_post_id=run.wordpress_post_id,
            artifact_id=classification.artifact_id,
            artifact_hash=classification.artifact_hash,
            preflight_classification=classification.classification,
            executed=True,
            run_id=run.id,
            run_status=run.status,
            request_content_hash=run.request_content_hash,
            expected_pre_update_wordpress_raw_content_hash=(
                run.expected_pre_update_wordpress_raw_content_hash
            ),
            observed_pre_update_wordpress_raw_content_hash=(
                run.observed_pre_update_wordpress_raw_content_hash
            ),
            response_content_raw_hash=run.response_content_raw_hash,
            wordpress_status=classification.wordpress_status,
            reason_code=reason_code,
        )


def _raw_only(field: object) -> str | None:
    if isinstance(field, dict):
        v = field.get("raw")
        return v if isinstance(v, str) else None
    return field if isinstance(field, str) else None


def _rendered_only(field: object) -> str | None:
    if isinstance(field, dict):
        v = field.get("rendered")
        return v if isinstance(v, str) else None
    return None


def _safe_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_modified_gmt(raw: str | None):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return to_storage_utc(dt)


# ---------------------------------------------------------------------------
# D-D5D §17/§32 -- exact failure classification rules implemented, reported here
# per phase instruction rather than invented:
#
# The existing WordPressClient contract (``_check_status`` in app/wordpress/client.py)
# raises exactly ONE exception type, ``ExternalProviderError``, for every non-2xx
# HTTP outcome it can observe (blocked redirect, 401, 403, or "unexpected response
# status" covering every other 4xx/5xx) -- the *type* never distinguishes them, only
# the message string does. No existing, already-approved part of this codebase
# treats any of those cases as a source-provable "definitely zero write effect"
# signal distinguishable from the others without fragile string-matching.
#
# Given that contract, this service does NOT invent a definitive-failure code path.
# Every non-2xx/timeout/transport outcome from ``update_post_content_exact`` --
# and every post-write verification failure (post-id mismatch, read-back failure,
# read-back malformed/unverifiable) -- is classified as ``outcome_unknown``.
# ``WordPressContentUpdateRunRepository.mark_failed`` remains available (unchanged,
# from D-D5B) for a future phase that adds structural, provider-contract-verified
# zero-effect detection (e.g. a client-level distinction for 401/403 verified
# against WordPress's documented pre-write auth-check ordering); this phase does
# not add that distinction, per the explicit instruction not to invent one solely
# to exercise a "failed" code path.
# ---------------------------------------------------------------------------
