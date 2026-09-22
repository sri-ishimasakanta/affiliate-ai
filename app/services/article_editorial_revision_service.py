"""ArticleEditorialRevision の preview / revise オーケストレーション。

採用済み (promote 済み) の Article 本文を **改訂** するための最小の正規経路。
``ArticleDraftPromotionService`` の first-promotion semantics は一切弱めない:
promotion が存在しない Article はここでも改訂できず、改訂は必ず「現在の canonical
本文」を起点にする。

- :meth:`preview` は完全 read-only (hash + validator を計算するだけ、DB write 0)。
- :meth:`revise` が **transaction owner**:
  gate 検証 → 3-hash drift guard → validator 再実行 → immutable な revision 行を
  append → Article.body / meta 書き込み を 1 transaction で行う。途中失敗は full
  rollback。Repository は commit しない。
- Article.status は **変更しない**。改訂は状態遷移ではない (published は published の
  まま)。WordPress 側の同期は既存の publication artifact + content update path が担う。
- 同一内容の再適用は no-op (``already_applied=True`` を返し、行を作らない)。
- cross-article revision は不可能: base promotion は article_id 一致を必須とし、
  revision_content_hash も article_id を含む。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.draft_output_contract import ParsedDraft
from app.article.draft_output_validators import validate_draft_output
from app.article.draft_promotion_canonical import compute_text_hash
from app.article.editorial_revision_canonical import compute_revision_content_hash
from app.article.fact_freshness import to_storage_utc
from app.article.schemas import (
    EditorialRevisionCreateResponse,
    EditorialRevisionGates,
    EditorialRevisionPreviewResponse,
    EditorialRevisionRead,
)
from app.exceptions import (
    CanonicalContentChangedError,
    EditorialRevisionStateError,
    EntityNotFoundError,
)
from app.models import ArticleEditorialRevision
from app.models.enums import ArticleStatus
from app.repositories.article_draft_promotion_repository import (
    ArticleDraftPromotionRepository,
)
from app.repositories.article_editorial_revision_repository import (
    ArticleEditorialRevisionRepository,
)
from app.repositories.article_repository import ArticleRepository

_ARTICLE = "Article"
_ENTITY = "ArticleEditorialRevision"

# 改訂を許す Article status。idea/planned/drafting は promotion 前なので対象外
# (それらは通常の生成 -> promotion 経路を使う)。archived/rewrite は編集途中の状態を
# 持ちうるため、この最小経路では扱わない。
_REVISABLE_STATUSES = frozenset(
    {
        ArticleStatus.REVIEW.value,
        ArticleStatus.APPROVED.value,
        ArticleStatus.PUBLISHED.value,
    }
)


class _Candidate:
    """1 回の preview/revise 内で共有する派生値。"""

    def __init__(self, *, article, body_markdown: str, meta_description: str) -> None:
        self.article = article
        self.body_markdown = body_markdown
        self.meta_description = meta_description
        self.body_hash = compute_text_hash(body_markdown)
        self.meta_hash = compute_text_hash(meta_description)
        self.revision_content_hash = compute_revision_content_hash(
            article_id=article.id if article is not None else 0,
            body_markdown=body_markdown,
            meta_description=meta_description,
        )
        self.current_body_hash = compute_text_hash(
            (article.body or "") if article is not None else ""
        )
        self.current_meta_hash = compute_text_hash(
            (article.meta_description or "") if article is not None else ""
        )

    @property
    def is_noop(self) -> bool:
        return (
            self.body_hash == self.current_body_hash
            and self.meta_hash == self.current_meta_hash
        )


class ArticleEditorialRevisionService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._promotions = ArticleDraftPromotionRepository(session)
        self._repo = ArticleEditorialRevisionRepository(session)

    # -- read (no DB write) --------------------------------------
    def preview(
        self,
        article_id: int,
        *,
        body_markdown: str,
        meta_description: str,
        revision_reason: str = "",
        published_update_intent: str | None = None,
    ) -> EditorialRevisionPreviewResponse:
        article = self._articles.get_by_id(article_id)
        base = self._base_promotion(article_id) if article is not None else None
        cand = _Candidate(
            article=article,
            body_markdown=body_markdown,
            meta_description=meta_description,
        )
        gates, report = self._evaluate(
            cand,
            base=base,
            revision_reason=revision_reason,
            published_update_intent=published_update_intent,
        )
        return EditorialRevisionPreviewResponse(
            article_id=article_id,
            base_promotion_id=(base.id if base is not None else None),
            article_status=(str(article.status) if article is not None else "missing"),
            current_body_hash=cand.current_body_hash,
            current_meta_hash=cand.current_meta_hash,
            body_hash=cand.body_hash,
            meta_hash=cand.meta_hash,
            revision_content_hash=cand.revision_content_hash,
            body_chars=len(body_markdown),
            meta_chars=len(meta_description),
            is_noop=cand.is_noop,
            validation_report=report,
            can_revise=all(gates.model_dump().values()),
            gates=gates,
        )

    # -- write (transaction owner) -----------------------------
    def revise(
        self,
        article_id: int,
        *,
        body_markdown: str,
        meta_description: str,
        expected_current_body_hash: str,
        expected_current_meta_hash: str,
        expected_revision_content_hash: str,
        revision_reason: str,
        published_update_intent: str | None = None,
        idempotency_key: str | None = None,
        editor_notes: list | None = None,
        now: datetime | None = None,
    ) -> EditorialRevisionCreateResponse:
        now = now or datetime.now(UTC)

        # -- idempotency prelookup -------------------------------
        if idempotency_key is not None:
            existing = self._repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.article_id == article_id
                    and existing.revision_content_hash == expected_revision_content_hash
                ):
                    return self._response(existing, already=True)
                raise EditorialRevisionStateError(
                    f"idempotency_key {idempotency_key!r} already used for a "
                    "different revision identity"
                )

        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError(_ARTICLE, article_id)

        base = self._base_promotion(article_id)
        cand = _Candidate(
            article=article,
            body_markdown=body_markdown,
            meta_description=meta_description,
        )

        # -- 3-hash drift guard (編集者が見ていた本文と別物なら拒否) --------
        if expected_current_body_hash != cand.current_body_hash:
            raise CanonicalContentChangedError(
                "expected_current_body_hash",
                expected_current_body_hash,
                cand.current_body_hash,
            )
        if expected_current_meta_hash != cand.current_meta_hash:
            raise CanonicalContentChangedError(
                "expected_current_meta_hash",
                expected_current_meta_hash,
                cand.current_meta_hash,
            )
        if expected_revision_content_hash != cand.revision_content_hash:
            raise CanonicalContentChangedError(
                "expected_revision_content_hash",
                expected_revision_content_hash,
                cand.revision_content_hash,
            )

        # -- gates -----------------------------------------------
        gates, report = self._evaluate(
            cand,
            base=base,
            revision_reason=revision_reason,
            published_update_intent=published_update_intent,
        )
        self._assert_gates(gates)

        # -- no-op: 内容が同一なら行を作らない ---------------------
        if cand.is_noop:
            previous = self._repo.get_latest(article_id)
            if previous is not None and previous.revision_content_hash == (
                cand.revision_content_hash
            ):
                return self._response(previous, already=True)
            raise EditorialRevisionStateError(
                "revision is a no-op: submitted body/meta are identical to the "
                "current canonical content"
            )

        # -- duplicate (同じ内容が過去に適用済み) -------------------
        dup = self._repo.find_by_article_and_content_hash(
            article_id, cand.revision_content_hash
        )
        if dup is not None:
            return self._response(dup, already=True)

        # -- single transaction ----------------------------------
        try:
            entity = self._repo.add(
                article_id=article_id,
                base_promotion_id=base.id,
                revision_reason=revision_reason,
                article_status_at_revision=str(article.status),
                published_update_intent=published_update_intent,
                previous_body_hash=cand.current_body_hash,
                previous_meta_hash=cand.current_meta_hash,
                body_markdown=body_markdown,
                meta_description=meta_description,
                body_hash=cand.body_hash,
                meta_hash=cand.meta_hash,
                revision_content_hash=cand.revision_content_hash,
                validation_report=report,
                editor_notes=(list(editor_notes) if editor_notes is not None else None),
                idempotency_key=idempotency_key,
                revised_at=to_storage_utc(now),
            )
            article.body = body_markdown
            article.meta_description = meta_description
            # status は変更しない (改訂は状態遷移ではない)。
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            if idempotency_key is not None:
                existing = self._repo.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return self._response(existing, already=True)
            existing = self._repo.find_by_article_and_content_hash(
                article_id, cand.revision_content_hash
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
    def list_for_article(self, article_id: int) -> list[ArticleEditorialRevision]:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        return self._repo.list_by_article(article_id)

    def get(self, article_id: int, revision_id: int) -> ArticleEditorialRevision:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        row = self._repo.get_by_id(revision_id)
        if row is None or row.article_id != article_id:
            raise EntityNotFoundError(_ENTITY, revision_id)
        return row

    # -- internals -------------------------------------------
    def _base_promotion(self, article_id: int):
        """この Article 自身の最新 promotion。cross-article 参照は構造的に起きない。"""

        rows = self._promotions.list_by_article(article_id)
        return rows[0] if rows else None

    def _evaluate(
        self,
        cand: _Candidate,
        *,
        base,
        revision_reason: str,
        published_update_intent: str | None,
    ) -> tuple[EditorialRevisionGates, dict]:
        article = cand.article
        body_ok = bool(cand.body_markdown.strip())
        meta_ok = bool(cand.meta_description.strip())
        candidate_parses = body_ok and meta_ok

        status = str(article.status) if article is not None else ""
        is_published = status == ArticleStatus.PUBLISHED.value

        report: dict = {}
        val_pass = False
        if candidate_parses and base is not None:
            package = getattr(base.source_run, "prompt_package", None) or {}
            parsed = ParsedDraft(
                meta_description=cand.meta_description.strip(),
                body_markdown=cand.body_markdown,
                generation_notes=[],
            )
            report = validate_draft_output(parsed=parsed, package=package)
            val_pass = report.get("overall") == "pass"

        gates = EditorialRevisionGates(
            article_exists=article is not None,
            article_has_promotion=base is not None,
            article_status_revisable=status in _REVISABLE_STATUSES,
            article_body_present=bool((article.body or "").strip())
            if article is not None
            else False,
            article_meta_present=bool((article.meta_description or "").strip())
            if article is not None
            else False,
            revision_reason_present=bool(revision_reason.strip()),
            published_update_intent_ok=(
                bool((published_update_intent or "").strip()) if is_published else True
            ),
            candidate_parses=candidate_parses,
            candidate_validation_pass=val_pass,
        )
        return gates, report

    @staticmethod
    def _assert_gates(gates: EditorialRevisionGates) -> None:
        failed = [name for name, ok in gates.model_dump().items() if not ok]
        if failed:
            raise EditorialRevisionStateError("failed gates: " + ", ".join(failed))

    @staticmethod
    def _response(
        entity: ArticleEditorialRevision, *, already: bool
    ) -> EditorialRevisionCreateResponse:
        return EditorialRevisionCreateResponse(
            revision=EditorialRevisionRead.model_validate(entity),
            article_status=str(entity.article.status),
            already_applied=already,
        )
