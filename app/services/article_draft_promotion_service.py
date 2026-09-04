"""ArticleDraftPromotion の preview / promote オーケストレーション。

- :meth:`preview` は完全 read-only (hash + validator を計算するだけ、DB write 0)。
- :meth:`promote` が **transaction owner**:
  gate 検証 → 3-hash drift guard → 候補 validator 再実行 → source run integrity 検証
  → immutable 採用行を append → Article.body / meta 書き込み → drafting→review 遷移
  を 1 transaction で行う。途中失敗は full rollback。Repository は commit しない。
- 生成 run (DraftGenerationRun) は一切変更しない (terminal semantics を壊さない)。
- 現行ポリシー: **最初の promotion のみ** (Article.body/meta が未設定・status=drafting)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.draft_output_contract import ParsedDraft
from app.article.draft_output_validators import validate_draft_output
from app.article.draft_promotion_canonical import (
    compute_candidate_content_hash,
    compute_text_hash,
)
from app.article.draft_prompt_canonical import (
    compute_prompt_input_hash,
    compute_rendered_prompt_hash,
)
from app.article.fact_freshness import to_storage_utc
from app.article.schemas import (
    DraftPromotionCreateResponse,
    DraftPromotionGates,
    DraftPromotionPreviewResponse,
    DraftPromotionRead,
)
from app.exceptions import (
    CandidateChangedError,
    DraftGenerationNotReadyError,
    DraftPromotionStateError,
    EntityNotFoundError,
)
from app.models import ArticleDraftPromotion
from app.models.draft_generation_run import RUN_SUCCEEDED
from app.models.enums import ArticleStatus
from app.repositories.article_draft_promotion_repository import (
    ArticleDraftPromotionRepository,
)
from app.repositories.article_repository import ArticleRepository
from app.repositories.draft_generation_run_repository import (
    DraftGenerationRunRepository,
)
from app.services.status_transitions import ARTICLE_TRANSITIONS, ensure_transition_allowed

_ARTICLE = "Article"
_RUN = "DraftGenerationRun"
_ENTITY = "ArticleDraftPromotion"
_ALLOWED_ARTICLE_STATUS = ArticleStatus.DRAFTING.value


class _Candidate:
    """1 回の preview/promote 内で共有する派生値。"""

    def __init__(
        self, *, article, run, body_markdown: str, meta_description: str
    ) -> None:
        self.article = article
        self.run = run
        self.body_markdown = body_markdown
        self.meta_description = meta_description
        self.body_hash = compute_text_hash(body_markdown)
        self.meta_hash = compute_text_hash(meta_description)
        self.candidate_content_hash = compute_candidate_content_hash(
            article_id=article.id if article is not None else 0,
            source_run_id=run.id if run is not None else 0,
            body_markdown=body_markdown,
            meta_description=meta_description,
        )


class ArticleDraftPromotionService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._runs = DraftGenerationRunRepository(session)
        self._repo = ArticleDraftPromotionRepository(session)

    # -- read (no DB write) --------------------------------------
    def preview(
        self,
        article_id: int,
        *,
        source_run_id: int,
        body_markdown: str,
        meta_description: str,
    ) -> DraftPromotionPreviewResponse:
        article = self._articles.get_by_id(article_id)
        run = self._runs.get_by_id(source_run_id)
        cand = _Candidate(
            article=article,
            run=run,
            body_markdown=body_markdown,
            meta_description=meta_description,
        )
        gates, report = self._evaluate(cand, article_id=article_id)
        return DraftPromotionPreviewResponse(
            article_id=article_id,
            source_run_id=source_run_id,
            body_hash=cand.body_hash,
            meta_hash=cand.meta_hash,
            candidate_content_hash=cand.candidate_content_hash,
            body_chars=len(body_markdown),
            meta_chars=len(meta_description),
            validation_report=report,
            source_run_status=(run.status if run is not None else "missing"),
            source_prompt_input_hash=(
                run.prompt_input_hash if run is not None else ""
            ),
            source_rendered_prompt_hash=(
                run.rendered_prompt_hash if run is not None else ""
            ),
            article_status=(str(article.status) if article is not None else "missing"),
            can_promote=all(gates.model_dump().values()),
            gates=gates,
        )

    # -- write (transaction owner) -----------------------------
    def promote(
        self,
        article_id: int,
        *,
        source_run_id: int,
        body_markdown: str,
        meta_description: str,
        expected_body_hash: str,
        expected_meta_hash: str,
        expected_candidate_content_hash: str,
        idempotency_key: str | None = None,
        human_review_notes: list | None = None,
        now: datetime | None = None,
    ) -> DraftPromotionCreateResponse:
        now = now or datetime.now(UTC)

        # -- idempotency prelookup -------------------------------
        if idempotency_key is not None:
            existing = self._repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.article_id == article_id
                    and existing.source_run_id == source_run_id
                    and existing.candidate_content_hash
                    == expected_candidate_content_hash
                ):
                    return self._response(existing, already=True)
                raise DraftPromotionStateError(
                    f"idempotency_key {idempotency_key!r} already used for a "
                    "different promotion identity"
                )

        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        run = self._runs.get_by_id(source_run_id)
        if run is None:
            raise EntityNotFoundError(_RUN, source_run_id)

        cand = _Candidate(
            article=article,
            run=run,
            body_markdown=body_markdown,
            meta_description=meta_description,
        )

        # -- 3-hash drift guard (Human が review した本文と別物なら拒否) -----
        if expected_body_hash != cand.body_hash:
            raise CandidateChangedError(
                "expected_body_hash", expected_body_hash, cand.body_hash
            )
        if expected_meta_hash != cand.meta_hash:
            raise CandidateChangedError(
                "expected_meta_hash", expected_meta_hash, cand.meta_hash
            )
        if expected_candidate_content_hash != cand.candidate_content_hash:
            raise CandidateChangedError(
                "expected_candidate_content_hash",
                expected_candidate_content_hash,
                cand.candidate_content_hash,
            )

        # -- gates (state / source run / validator) --------------
        gates, report = self._evaluate(cand, article_id=article_id)
        self._assert_gates(gates)

        # -- source run frozen artifact integrity (再 build しない) ----
        self._assert_source_run_integrity(run)

        # -- duplicate (same candidate already promoted) --------
        dup = self._repo.find_by_article_and_candidate_hash(
            article_id, cand.candidate_content_hash
        )
        if dup is not None:
            return self._response(dup, already=True)

        # -- single transaction --------------------------------
        try:
            entity = self._repo.add(
                article_id=article_id,
                source_run_id=source_run_id,
                source_prompt_input_hash=run.prompt_input_hash,
                source_rendered_prompt_hash=run.rendered_prompt_hash,
                body_markdown=body_markdown,
                meta_description=meta_description,
                body_hash=cand.body_hash,
                meta_hash=cand.meta_hash,
                candidate_content_hash=cand.candidate_content_hash,
                validation_report=report,
                human_review_notes=(
                    list(human_review_notes)
                    if human_review_notes is not None
                    else None
                ),
                idempotency_key=idempotency_key,
                promoted_at=to_storage_utc(now),
            )
            article.body = body_markdown
            article.meta_description = meta_description
            ensure_transition_allowed(
                _ARTICLE,
                ArticleStatus.DRAFTING,
                ArticleStatus.REVIEW,
                ARTICLE_TRANSITIONS,
            )
            article.status = ArticleStatus.REVIEW.value
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            if idempotency_key is not None:
                existing = self._repo.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return self._response(existing, already=True)
            existing = self._repo.find_by_article_and_candidate_hash(
                article_id, cand.candidate_content_hash
            )
            if existing is not None:
                return self._response(existing, already=True)
            raise
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return self._response(entity, already=False)

    # -- read helpers -----------------------------------------
    def list_for_article(self, article_id: int) -> list[ArticleDraftPromotion]:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        return self._repo.list_by_article(article_id)

    def get(self, article_id: int, promotion_id: int) -> ArticleDraftPromotion:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        row = self._repo.get_by_id(promotion_id)
        if row is None or row.article_id != article_id:
            raise EntityNotFoundError(_ENTITY, promotion_id)
        return row

    # -- internals -------------------------------------------
    def _evaluate(
        self, cand: _Candidate, *, article_id: int
    ) -> tuple[DraftPromotionGates, dict]:
        article, run = cand.article, cand.run
        body_ok = bool(cand.body_markdown.strip())
        meta_ok = bool(cand.meta_description.strip())
        candidate_parses = body_ok and meta_ok

        report: dict = {}
        val_pass = False
        promo_eligible = False
        if candidate_parses and run is not None:
            parsed = ParsedDraft(
                meta_description=cand.meta_description.strip(),
                body_markdown=cand.body_markdown,
                generation_notes=[],
            )
            report = validate_draft_output(parsed=parsed, package=run.prompt_package)
            val_pass = report.get("overall") == "pass"
            promo_eligible = report.get("promotion_eligible") is True

        run_prompt_hash_ok = False
        run_rendered_hash_ok = False
        if run is not None:
            try:
                run_prompt_hash_ok = (
                    compute_prompt_input_hash(run.prompt_package)
                    == run.prompt_input_hash
                )
                run_rendered_hash_ok = (
                    compute_rendered_prompt_hash(run.rendered_prompt)
                    == run.rendered_prompt_hash
                )
            except Exception:
                run_prompt_hash_ok = False
                run_rendered_hash_ok = False

        gates = DraftPromotionGates(
            article_exists=article is not None,
            article_status_ok=(
                article is not None
                and str(article.status) == _ALLOWED_ARTICLE_STATUS
            ),
            article_body_empty=(article is not None and article.body is None),
            article_meta_empty=(
                article is not None and article.meta_description is None
            ),
            source_run_exists=run is not None,
            source_run_belongs_to_article=(
                run is not None and run.article_id == article_id
            ),
            source_run_succeeded=(run is not None and run.status == RUN_SUCCEEDED),
            source_run_prompt_hash_ok=run_prompt_hash_ok,
            source_run_rendered_hash_ok=run_rendered_hash_ok,
            candidate_parses=candidate_parses,
            candidate_validation_pass=val_pass,
            candidate_promotion_eligible=promo_eligible,
        )
        return gates, report

    @staticmethod
    def _assert_gates(gates: DraftPromotionGates) -> None:
        g = gates.model_dump()
        if not g["article_exists"]:
            raise EntityNotFoundError(_ARTICLE, "?")
        if not g["source_run_exists"]:
            raise EntityNotFoundError(_RUN, "?")
        failed = [k for k, v in g.items() if not v]
        if failed:
            raise DraftPromotionStateError(
                "promotion gate(s) not satisfied: " + ", ".join(failed)
            )

    def _assert_source_run_integrity(self, run) -> None:
        if compute_prompt_input_hash(run.prompt_package) != run.prompt_input_hash:
            raise DraftGenerationNotReadyError(
                f"source run {run.id}: stored prompt_package hash mismatch"
            )
        if (
            compute_rendered_prompt_hash(run.rendered_prompt)
            != run.rendered_prompt_hash
        ):
            raise DraftGenerationNotReadyError(
                f"source run {run.id}: stored rendered_prompt hash mismatch"
            )
        snap = run.snapshot
        if snap is None or snap.content_hash != run.snapshot_content_hash:
            raise DraftGenerationNotReadyError(
                f"source run {run.id}: snapshot binding changed"
            )
        if snap.article_id != run.article_id:
            raise DraftGenerationNotReadyError(
                f"source run {run.id}: snapshot/article binding mismatch"
            )

    def _response(
        self, entity: ArticleDraftPromotion, *, already: bool
    ) -> DraftPromotionCreateResponse:
        return DraftPromotionCreateResponse(
            promotion=self.to_read(entity),
            article_status=ArticleStatus.REVIEW.value,
            already_promoted=already,
        )

    @staticmethod
    def to_read(entity: ArticleDraftPromotion) -> DraftPromotionRead:
        report = entity.validation_report or {}
        return DraftPromotionRead(
            id=entity.id,
            article_id=entity.article_id,
            source_run_id=entity.source_run_id,
            source_prompt_input_hash=entity.source_prompt_input_hash,
            source_rendered_prompt_hash=entity.source_rendered_prompt_hash,
            body_hash=entity.body_hash,
            meta_hash=entity.meta_hash,
            candidate_content_hash=entity.candidate_content_hash,
            body_chars=len(entity.body_markdown),
            meta_chars=len(entity.meta_description),
            validation_overall=report.get("overall"),
            promotion_eligible=report.get("promotion_eligible"),
            idempotency_key=entity.idempotency_key,
            promoted_at=entity.promoted_at,
            created_at=entity.created_at,
            body_markdown=entity.body_markdown,
            meta_description=entity.meta_description,
            validation_report=entity.validation_report,
            human_review_notes=entity.human_review_notes,
        )
