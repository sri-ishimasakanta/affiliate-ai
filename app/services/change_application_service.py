"""ChangeApplicationService -- 承認済み提案の適用 (C9.2)。

**新しい WordPress 更新スタックは作らない。** 既存の managed 経路を順に使うだけ:

    ArticleEditorialRevisionService   (記事本文の改訂を正規経路で記録)
      -> ArticlePublicationPreparationService / ArtifactPersistence / Approval
      -> WordPressContentUpdateExecutionService  (読み戻し込みの 1 回の書き込み)
      -> WordPressContentUpdateReconciliationService (outcome_unknown の事後照合)

既定は **PLAN** で、本番書き込みは呼び出し側が明示したときだけ行う。

適用前に必ず確認する (どれか 1 つでも崩れていれば書かない):

- request が ``approved``
- 承認が **いまの** ``proposal_hash`` に対するもの
- 記事本文が提案時点から変わっていない
- リンク先がまだ公開されていて canonical も変わっていない
- 同じリンクがまだ存在しない
- WordPress 側の現在状態を読み、``modified_gmt`` を凍結できる
- 変更前の本文を保存できる (ロールバックの起点)

盲目的な上書きはしない。ズレていれば ``stale`` にして書き込まない。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.editorial_revision_canonical import compute_revision_content_hash
from app.article.fact_freshness import to_storage_utc
from app.change.internal_link import added_link_count, non_link_text_unchanged
from app.models import (
    CR_APPLIED,
    CR_APPLY_FAILED,
    CR_APPROVED,
    CR_RECONCILED,
    CR_STALE,
    Article,
    ChangeApplication,
    ChangeRequest,
)
from app.services.change_request_service import ChangeRequestError, ChangeRequestService

OUTCOME_PLANNED = "planned"
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_OUTCOME_UNKNOWN = "outcome_unknown"
OUTCOME_RECONCILED = "reconciled"
OUTCOME_BLOCKED = "blocked"


@dataclass
class ApplyOutcome:
    """適用 (または PLAN) の結果。安全な事実のみ。"""

    request_id: int
    article_id: int
    executed: bool
    outcome: str
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    proposal_hash: str | None = None
    source_body_hash: str | None = None
    proposed_body_hash: str | None = None
    wordpress_post_id: str | None = None
    wordpress_pre_modified_gmt: str | None = None
    wordpress_post_modified_gmt: str | None = None
    editorial_revision_id: int | None = None
    content_update_run_id: int | None = None
    reconciliation_id: int | None = None
    application_id: int | None = None
    error_category: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return not self.blocked_reasons

    def as_dict(self) -> dict:
        return asdict(self)


class ChangeApplicationService:
    def __init__(self, session_factory, *, settings, wordpress_client=None) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._wordpress_client = wordpress_client

    # -- public ---------------------------------------------------------------
    def plan(self, request_id: int, *, now: datetime | None = None) -> ApplyOutcome:
        """書き込まずに、いま適用できるかだけを判定する。"""

        now = now or datetime.now(UTC)
        with self._session_factory() as session:
            request = self._require(session, request_id)
            outcome = ApplyOutcome(
                request_id=request.id,
                article_id=request.article_id,
                executed=False,
                outcome=OUTCOME_PLANNED,
                proposal_hash=request.proposal_hash,
                source_body_hash=request.expected_source_body_hash,
                proposed_body_hash=request.proposed_body_hash,
            )
            self._check_local_gates(session, request, outcome)
            article = session.get(Article, request.article_id)
            outcome.wordpress_post_id = (
                str(article.wordpress_post_id) if article and article.wordpress_post_id else None
            )
            if outcome.blocked_reasons:
                outcome.outcome = OUTCOME_BLOCKED
            return outcome

    def apply(
        self,
        request_id: int,
        *,
        execute: bool = False,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ApplyOutcome:
        """``execute=True`` のときだけ本番へ書き込む。既定は PLAN と同じ。"""

        outcome = self.plan(request_id, now=now)
        if not execute or outcome.blocked_reasons:
            return outcome
        return self._execute(request_id, idempotency_key=idempotency_key, now=now)

    def rollback_plan(self, application_id: int) -> dict:
        """巻き戻しに必要な素材を返す (**実行はしない**)。

        自動ロールバックは作らない。適用と同じ managed 経路を人が通すほうが、
        「もう 1 つの書き込み経路」を増やすより安全だからである。ここでは
        ``pre_change_body`` がそのまま残っていることを確認し、人が次に打つ
        コマンドを提示するだけにする。
        """

        with self._session_factory() as session:
            row = session.get(ChangeApplication, application_id)
            if row is None:
                raise ChangeRequestError(f"change application {application_id} not found")
            article = session.get(Article, row.article_id)
            current_hash = compute_text_hash(article.body or "") if article else None
            return {
                "change_application_id": row.id,
                "change_request_id": row.change_request_id,
                "article_id": row.article_id,
                "outcome": row.outcome,
                "wordpress_post_id": row.wordpress_post_id,
                "wordpress_pre_modified_gmt": row.wordpress_pre_modified_gmt,
                "wordpress_post_modified_gmt": row.wordpress_post_modified_gmt,
                "rollback_available": bool(row.pre_change_body),
                "rollback_body_hash": row.source_body_hash,
                "rollback_body": row.pre_change_body,
                "current_body_hash": current_hash,
                "current_body_matches_applied": current_hash == row.proposed_body_hash,
                "instructions": [
                    "1. この出力の rollback_body を確認する (適用直前の本文そのもの)。",
                    "2. 適用と同じ managed 経路で戻す: "
                    "scripts/manage_wordpress_content_update.py "
                    f"--article-id {row.article_id} (人が本文を差し戻して改訂を作る)。",
                    "3. 戻したあと read-back を確認し、この application を履歴として残す "
                    "(行は消さない -- append-only)。",
                ],
            }

    # -- gates ----------------------------------------------------------------
    def _check_local_gates(
        self, session: Session, request: ChangeRequest, outcome: ApplyOutcome
    ) -> None:
        service = ChangeRequestService(session)

        if request.status != CR_APPROVED:
            outcome.blocked_reasons.append(
                f"request status is {request.status!r}; only 'approved' can be applied"
            )
        approval = service.latest_approval(request)
        if approval is None:
            outcome.blocked_reasons.append("no approval exists for this request")
        elif approval.approved_proposal_hash != request.proposal_hash:
            outcome.blocked_reasons.append(
                "the approval was granted for a different proposal hash; "
                "re-review and approve the current proposal"
            )

        report = service.evaluate_staleness(request)
        if report.stale:
            outcome.blocked_reasons.extend(report.reasons)
        if report.candidate_currently_present is False:
            # 消えたことは警告であって中止理由ではない -- 判断は人に残す。
            outcome.warnings.append(
                "the source candidate is no longer present in the latest evaluation; "
                "this does not mean it was fixed"
            )

        article = session.get(Article, request.article_id)
        if article is None or not article.wordpress_post_id:
            outcome.blocked_reasons.append("article has no WordPress post id")
        if article is not None:
            body = article.body or ""
            if added_link_count(body, request.proposed_body) != 1:
                outcome.blocked_reasons.append(
                    "the proposal does not add exactly one link to the current body"
                )
            if not non_link_text_unchanged(body, request.proposed_body):
                outcome.blocked_reasons.append(
                    "the proposal would modify existing prose; V1 only inserts"
                )
            if compute_text_hash(request.proposed_body) != request.proposed_body_hash:
                outcome.blocked_reasons.append("stored proposed body does not match its hash")

    # -- execution (managed path のみを使う) ----------------------------------
    def _execute(
        self, request_id: int, *, idempotency_key: str | None, now: datetime | None
    ) -> ApplyOutcome:
        import httpx

        from app.services.article_editorial_revision_service import (
            ArticleEditorialRevisionService,
        )
        from app.services.article_publication_artifact_persistence_service import (
            ArticlePublicationArtifactPersistenceService,
        )
        from app.services.article_publication_artifact_service import (
            ArticlePublicationArtifactService,
        )
        from app.services.article_publication_preparation_service import (
            ArticlePublicationPreparationService,
        )
        from app.services.wordpress_content_update_execution_service import (
            WordPressContentUpdateExecutionService,
        )
        from app.services.wordpress_content_update_reconciliation_service import (
            WordPressContentUpdateReconciliationService,
        )

        now = now or datetime.now(UTC)
        with self._session_factory() as session:
            request = self._require(session, request_id)
            article = session.get(Article, request.article_id)
            approval = ChangeRequestService(session).latest_approval(request)
            post_id = str(article.wordpress_post_id)
            pre_body = article.body or ""
            source_hash = compute_text_hash(pre_body)
            meta = article.meta_description
            proposed_body = request.proposed_body
            proposal_hash = request.proposal_hash
            proposal_version = request.proposal_version
            approval_id = approval.id if approval else None

        # -- 書き込み前の live 状態を凍結する (ロールバックと照合の起点) -------
        base = (self._settings.wordpress_base_url or "").rstrip("/")
        auth = (self._settings.wordpress_username, self._settings.wordpress_app_password)
        with httpx.Client(timeout=30) as http:
            live = http.get(
                f"{base}/wp-json/wp/v2/posts/{post_id}",
                auth=auth,
                params={"context": "edit"},
            ).json()
        pre_modified = live.get("modified_gmt") if isinstance(live, dict) else None
        pre_raw_hash = compute_text_hash((live.get("content") or {}).get("raw") or "")
        wordpress_status = live.get("status")

        outcome = ApplyOutcome(
            request_id=request_id,
            article_id=article.id,
            executed=True,
            outcome=OUTCOME_FAILED,
            proposal_hash=proposal_hash,
            source_body_hash=source_hash,
            proposed_body_hash=request.proposed_body_hash,
            wordpress_post_id=post_id,
            wordpress_pre_modified_gmt=pre_modified,
        )

        application_id = self._append_application(
            request_id=request_id,
            approval_id=approval_id,
            article_id=article.id,
            proposal_hash=proposal_hash,
            proposal_version=proposal_version,
            source_body_hash=source_hash,
            proposed_body_hash=request.proposed_body_hash,
            pre_change_body=pre_body,
            post_id=post_id,
            pre_modified=pre_modified,
            outcome=OUTCOME_PLANNED,
            attempted_at=now,
            idempotency_key=idempotency_key,
        )
        outcome.application_id = application_id

        try:
            # 1) 既存の編集改訂経路で本文を更新する。
            with self._session_factory() as session:
                revision = ArticleEditorialRevisionService(session).revise(
                    article.id,
                    body_markdown=proposed_body,
                    meta_description=meta,
                    expected_current_body_hash=source_hash,
                    expected_current_meta_hash=compute_text_hash(meta or ""),
                    expected_revision_content_hash=compute_revision_content_hash(
                        article_id=article.id,
                        body_markdown=proposed_body,
                        meta_description=meta,
                    ),
                    revision_reason=request.rationale,
                    idempotency_key=f"c9-request-{request_id}-v{proposal_version}",
                    editor_notes=[
                        f"approved change request {request_id} (proposal {proposal_hash[:16]})"
                    ],
                )
                outcome.editorial_revision_id = revision.revision.id

            # 2) 既存の artifact/承認経路を通す。
            with self._session_factory() as session:
                prepared = ArticlePublicationPreparationService(session).prepare(article.id)
                artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
                artifact_id, artifact_hash = artifact.id, artifact.artifact_hash
            with self._session_factory() as session:
                ArticlePublicationArtifactService(session).approve_artifact(
                    artifact_id, expected_artifact_hash=artifact_hash
                )

            # 3) 既存の managed 更新 (読み戻し込み、1 回だけ書く)。
            with self._session_factory() as session:
                result = WordPressContentUpdateExecutionService(
                    session, wordpress_client=self._wordpress_client
                ).execute(
                    article_id=article.id,
                    artifact_id=artifact_id,
                    artifact_hash=artifact_hash,
                    expected_wordpress_status=wordpress_status,
                    expected_pre_update_raw_content_hash=pre_raw_hash,
                    idempotency_key=f"c9-apply-{request_id}-v{proposal_version}",
                )
                outcome.content_update_run_id = result.run_id
                run_status = result.run_status

            if run_status == "succeeded":
                outcome.outcome = OUTCOME_SUCCEEDED
            elif run_status == "outcome_unknown":
                # 4) 既存の照合経路で事後確認する (新しい仕組みは作らない)。
                with self._session_factory() as session:
                    reconciled = WordPressContentUpdateReconciliationService(session).reconcile(
                        run_id=outcome.content_update_run_id,
                        expected_article_id=article.id,
                        expected_wordpress_post_id=post_id,
                        expected_wordpress_status=wordpress_status,
                        reason=f"C9 apply of change request {request_id}",
                        idempotency_key=f"c9-reconcile-{request_id}-v{proposal_version}",
                    )
                    outcome.reconciliation_id = reconciled.reconciliation_id
                outcome.outcome = (
                    OUTCOME_RECONCILED if reconciled.resolved else OUTCOME_OUTCOME_UNKNOWN
                )
            else:
                outcome.outcome = OUTCOME_FAILED
        except Exception as exc:  # noqa: BLE001 - 失敗も履歴として残す
            outcome.outcome = OUTCOME_FAILED
            outcome.error_category = type(exc).__name__
            outcome.error_message = str(exc)[:500]

        # 書き込み後の live 状態を観測する。
        try:
            with httpx.Client(timeout=30) as http:
                after = http.get(
                    f"{base}/wp-json/wp/v2/posts/{post_id}",
                    auth=auth,
                    params={"context": "edit"},
                ).json()
            outcome.wordpress_post_modified_gmt = (
                after.get("modified_gmt") if isinstance(after, dict) else None
            )
        except Exception:  # noqa: BLE001 - 観測失敗は結果を変えない
            outcome.wordpress_post_modified_gmt = None

        self._finalize(outcome)
        return outcome

    # -- persistence ----------------------------------------------------------
    def _append_application(self, **kwargs) -> int:
        with self._session_factory() as session:
            row = ChangeApplication(
                change_request_id=kwargs["request_id"],
                change_request_approval_id=kwargs["approval_id"],
                article_id=kwargs["article_id"],
                proposal_hash=kwargs["proposal_hash"],
                proposal_version=kwargs["proposal_version"],
                source_body_hash=kwargs["source_body_hash"],
                proposed_body_hash=kwargs["proposed_body_hash"],
                pre_change_body=kwargs["pre_change_body"],
                wordpress_post_id=kwargs["post_id"],
                wordpress_pre_modified_gmt=kwargs["pre_modified"],
                outcome=kwargs["outcome"],
                attempted_at=to_storage_utc(kwargs["attempted_at"]),
                idempotency_key=kwargs["idempotency_key"],
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.id

    def _finalize(self, outcome: ApplyOutcome) -> None:
        with self._session_factory() as session:
            row = session.get(ChangeApplication, outcome.application_id)
            if row is not None:
                row.outcome = outcome.outcome
                row.editorial_revision_id = outcome.editorial_revision_id
                row.content_update_run_id = outcome.content_update_run_id
                row.reconciliation_id = outcome.reconciliation_id
                row.wordpress_post_modified_gmt = outcome.wordpress_post_modified_gmt
                row.error_category = outcome.error_category
                row.error_message = outcome.error_message
                row.result_json = {"warnings": outcome.warnings}
                row.finished_at = to_storage_utc(datetime.now(UTC))
            request = session.get(ChangeRequest, outcome.request_id)
            if request is not None:
                if outcome.outcome == OUTCOME_SUCCEEDED:
                    request.status = CR_APPLIED
                elif outcome.outcome == OUTCOME_RECONCILED:
                    request.status = CR_RECONCILED
                elif outcome.outcome == OUTCOME_BLOCKED:
                    request.status = CR_STALE
                else:
                    request.status = CR_APPLY_FAILED
                request.status_reason = outcome.error_message or outcome.outcome
            session.commit()

    def _require(self, session: Session, request_id: int) -> ChangeRequest:
        request = session.get(ChangeRequest, request_id)
        if request is None:
            raise ChangeRequestError(f"change request {request_id} not found")
        return request
