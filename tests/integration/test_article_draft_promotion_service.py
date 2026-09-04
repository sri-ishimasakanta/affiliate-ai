"""ArticleDraftPromotionService: preview / promote / 3-hash guard / rollback /
idempotency / source-run integrity。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import (
    compute_candidate_content_hash,
    compute_text_hash,
)
from app.exceptions import (
    CandidateChangedError,
    DraftGenerationNotReadyError,
    DraftPromotionStateError,
    EntityNotFoundError,
)
from app.models import ArticleDraftPromotion, DraftGenerationRun
from app.models.enums import ArticleStatus
from app.services.article_draft_promotion_service import ArticleDraftPromotionService
from tests.support.draft_promotion_fixture import article_of, promotable_scenario


def _svc(session: Session) -> ArticleDraftPromotionService:
    return ArticleDraftPromotionService(session)


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ArticleDraftPromotion))


def _hashes(ps, *, body=None, meta=None):
    body = ps.body_markdown if body is None else body
    meta = ps.meta_description if meta is None else meta
    return {
        "expected_body_hash": compute_text_hash(body),
        "expected_meta_hash": compute_text_hash(meta),
        "expected_candidate_content_hash": compute_candidate_content_hash(
            article_id=ps.article_id,
            source_run_id=ps.run_id,
            body_markdown=body,
            meta_description=meta,
        ),
    }


def _promote(session, ps, *, body=None, meta=None, **kw):
    return _svc(session).promote(
        ps.article_id,
        source_run_id=ps.run_id,
        body_markdown=ps.body_markdown if body is None else body,
        meta_description=ps.meta_description if meta is None else meta,
        **_hashes(ps, body=body, meta=meta),
        **kw,
    )


# -- preview ---------------------------------------------------------
def test_preview_is_read_only_and_can_promote(session: Session) -> None:
    ps = promotable_scenario(session)
    before_runs = session.scalar(select(func.count()).select_from(DraftGenerationRun))
    out = _svc(session).preview(
        ps.article_id,
        source_run_id=ps.run_id,
        body_markdown=ps.body_markdown,
        meta_description=ps.meta_description,
    )
    assert out.can_promote is True
    assert out.validation_report["overall"] == "pass"
    assert out.validation_report["promotion_eligible"] is True
    assert out.body_hash == compute_text_hash(ps.body_markdown)
    assert out.meta_hash == compute_text_hash(ps.meta_description)
    assert out.body_chars == len(ps.body_markdown)
    assert out.meta_chars == len(ps.meta_description)
    assert out.article_status == "drafting"
    assert out.source_run_status == "succeeded"
    # read-only
    assert _count(session) == 0
    assert session.scalar(
        select(func.count()).select_from(DraftGenerationRun)
    ) == before_runs
    art = article_of(session, ps.article_id)
    assert art.status == ArticleStatus.DRAFTING.value
    assert art.body is None and art.meta_description is None


def test_preview_warn_candidate_cannot_promote(session: Session) -> None:
    ps = promotable_scenario(session)
    # 20 <= len < 60 -> validator "warn" (not hard fail)
    short_meta = "業務効率化ツールのおすすめを目的別に比較し、選び方を分かりやすく解説します。"
    assert 20 <= len(short_meta) < 60
    out = _svc(session).preview(
        ps.article_id,
        source_run_id=ps.run_id,
        body_markdown=ps.body_markdown,
        meta_description=short_meta,
    )
    assert out.validation_report["overall"] == "warn"
    assert out.can_promote is False
    assert out.gates.candidate_validation_pass is False
    assert _count(session) == 0


def test_preview_rejects_run_of_other_article(session: Session) -> None:
    ps = promotable_scenario(session)
    other = promotable_scenario(session, n_tools=3, suffix="b")
    out = _svc(session).preview(
        other.article_id,
        source_run_id=ps.run_id,  # run belongs to ps.article_id
        body_markdown=ps.body_markdown,
        meta_description=ps.meta_description,
    )
    assert out.gates.source_run_belongs_to_article is False
    assert out.can_promote is False


# -- promotion happy path ------------------------------------------
def test_promote_writes_article_and_appends_row_in_one_tx(session: Session) -> None:
    ps = promotable_scenario(session)
    resp = _promote(session, ps, human_review_notes=["looks good"])

    assert resp.already_promoted is False
    assert resp.article_status == ArticleStatus.REVIEW.value
    assert _count(session) == 1

    art = article_of(session, ps.article_id)
    assert art.status == ArticleStatus.REVIEW.value
    assert art.body == ps.body_markdown
    assert art.meta_description == ps.meta_description

    row = session.get(ArticleDraftPromotion, resp.promotion.id)
    assert row.article_id == ps.article_id
    assert row.source_run_id == ps.run_id
    assert row.body_markdown == ps.body_markdown
    assert row.meta_description == ps.meta_description
    assert row.body_hash == compute_text_hash(ps.body_markdown)
    assert row.candidate_content_hash == _hashes(ps)["expected_candidate_content_hash"]
    assert row.validation_report["overall"] == "pass"
    assert row.human_review_notes == ["looks good"]
    assert row.source_prompt_input_hash == ps.run.prompt_input_hash

    # source run unchanged
    run = session.get(DraftGenerationRun, ps.run_id)
    assert run.status == "succeeded"
    assert run.parsed_body == ps.run.parsed_body
    assert run.parsed_meta_description == ps.run.parsed_meta_description
    assert run.validation_report == ps.run.validation_report


def test_promote_accepts_human_edited_candidate(session: Session) -> None:
    """採用本文は生成物と同一でなくてよい (Human 修正版)。"""
    ps = promotable_scenario(session)
    edited = ps.body_markdown + "\n\n<!-- human edit -->\n"
    resp = _promote(session, ps, body=edited)
    art = article_of(session, ps.article_id)
    assert art.body == edited
    row = session.get(ArticleDraftPromotion, resp.promotion.id)
    assert row.body_hash == compute_text_hash(edited)
    # not equal to the run's parsed_body
    assert edited != ps.run.parsed_body


# -- 3-hash drift guard ------------------------------------------
def test_drift_guard_wrong_body_hash(session: Session) -> None:
    ps = promotable_scenario(session)
    h = _hashes(ps)
    with pytest.raises(CandidateChangedError):
        _svc(session).promote(
            ps.article_id,
            source_run_id=ps.run_id,
            body_markdown=ps.body_markdown,
            meta_description=ps.meta_description,
            expected_body_hash="0" * 64,
            expected_meta_hash=h["expected_meta_hash"],
            expected_candidate_content_hash=h["expected_candidate_content_hash"],
        )
    assert _count(session) == 0
    assert article_of(session, ps.article_id).body is None


def test_drift_guard_wrong_meta_hash(session: Session) -> None:
    ps = promotable_scenario(session)
    h = _hashes(ps)
    with pytest.raises(CandidateChangedError):
        _svc(session).promote(
            ps.article_id,
            source_run_id=ps.run_id,
            body_markdown=ps.body_markdown,
            meta_description=ps.meta_description,
            expected_body_hash=h["expected_body_hash"],
            expected_meta_hash="0" * 64,
            expected_candidate_content_hash=h["expected_candidate_content_hash"],
        )
    assert _count(session) == 0
    assert article_of(session, ps.article_id).status == ArticleStatus.DRAFTING.value


def test_drift_guard_wrong_content_hash(session: Session) -> None:
    ps = promotable_scenario(session)
    h = _hashes(ps)
    with pytest.raises(CandidateChangedError):
        _svc(session).promote(
            ps.article_id,
            source_run_id=ps.run_id,
            body_markdown=ps.body_markdown,
            meta_description=ps.meta_description,
            expected_body_hash=h["expected_body_hash"],
            expected_meta_hash=h["expected_meta_hash"],
            expected_candidate_content_hash="0" * 64,
        )
    assert _count(session) == 0


def test_drift_guard_body_changed_since_review(session: Session) -> None:
    ps = promotable_scenario(session)
    stale = _hashes(ps)  # computed for original body
    with pytest.raises(CandidateChangedError):
        _svc(session).promote(
            ps.article_id,
            source_run_id=ps.run_id,
            body_markdown=ps.body_markdown + " changed",
            meta_description=ps.meta_description,
            **stale,
        )
    assert _count(session) == 0


# -- state / duplicate guards ----------------------------------
def test_promote_rejected_when_article_already_review(session: Session) -> None:
    ps = promotable_scenario(session)
    _promote(session, ps)
    with pytest.raises(DraftPromotionStateError):
        _promote(session, ps)
    assert _count(session) == 1


def test_promote_rejects_non_succeeded_run(session: Session) -> None:
    ps = promotable_scenario(session)
    run = session.get(DraftGenerationRun, ps.run_id)
    run.status = "running"  # force non-terminal
    session.flush()
    with pytest.raises(DraftPromotionStateError):
        _promote(session, ps)
    assert _count(session) == 0


def test_promote_rejects_run_from_other_article(session: Session) -> None:
    ps = promotable_scenario(session)
    other = promotable_scenario(session, n_tools=3, suffix="b")
    body, meta = other.body_markdown, other.meta_description
    cch = compute_candidate_content_hash(
        article_id=other.article_id,
        source_run_id=ps.run_id,
        body_markdown=body,
        meta_description=meta,
    )
    with pytest.raises(DraftPromotionStateError):
        _svc(session).promote(
            other.article_id,
            source_run_id=ps.run_id,  # belongs to ps.article_id
            body_markdown=body,
            meta_description=meta,
            expected_body_hash=compute_text_hash(body),
            expected_meta_hash=compute_text_hash(meta),
            expected_candidate_content_hash=cch,
        )
    assert _count(session) == 0


def test_promote_missing_article(session: Session) -> None:
    ps = promotable_scenario(session)
    with pytest.raises(EntityNotFoundError):
        _svc(session).promote(
            999999,
            source_run_id=ps.run_id,
            body_markdown=ps.body_markdown,
            meta_description=ps.meta_description,
            expected_body_hash="a" * 64,
            expected_meta_hash="b" * 64,
            expected_candidate_content_hash="c" * 64,
        )


def test_promote_missing_run(session: Session) -> None:
    ps = promotable_scenario(session)
    with pytest.raises(EntityNotFoundError):
        _svc(session).promote(
            ps.article_id,
            source_run_id=888888,
            body_markdown=ps.body_markdown,
            meta_description=ps.meta_description,
            expected_body_hash="a" * 64,
            expected_meta_hash="b" * 64,
            expected_candidate_content_hash="c" * 64,
        )


# -- idempotency ----------------------------------------------
def test_idempotency_same_key_same_candidate_returns_existing(session: Session) -> None:
    ps = promotable_scenario(session)
    r1 = _promote(session, ps, idempotency_key="k-1")
    r2 = _promote(session, ps, idempotency_key="k-1")
    assert r1.promotion.id == r2.promotion.id
    assert r2.already_promoted is True
    assert _count(session) == 1


def test_idempotency_same_key_different_candidate_conflicts(session: Session) -> None:
    ps = promotable_scenario(session)
    _promote(session, ps, idempotency_key="k-2")
    with pytest.raises(DraftPromotionStateError):
        _promote(session, ps, body=ps.body_markdown + " x", idempotency_key="k-2")
    assert _count(session) == 1


def test_multiple_null_idempotency_keys_allowed_via_separate_articles(
    session: Session,
) -> None:
    a = promotable_scenario(session)
    b = promotable_scenario(session, n_tools=3, suffix="b")
    _promote(session, a)
    _promote(session, b)
    assert _count(session) == 2
    rows = session.scalars(select(ArticleDraftPromotion)).all()
    assert all(r.idempotency_key is None for r in rows)


# -- source-run integrity -----------------------------------
def test_promote_rejects_corrupted_stored_prompt_hash(session: Session) -> None:
    ps = promotable_scenario(session)
    run = session.get(DraftGenerationRun, ps.run_id)
    run.prompt_input_hash = "0" * 64  # corrupt stored hash
    session.flush()
    with pytest.raises((DraftGenerationNotReadyError, DraftPromotionStateError)):
        _promote(session, ps)
    assert _count(session) == 0


def test_promote_rejects_snapshot_binding_change(session: Session) -> None:
    ps = promotable_scenario(session)
    run = session.get(DraftGenerationRun, ps.run_id)
    run.snapshot_content_hash = "0" * 64  # binding no longer matches snapshot
    session.flush()
    with pytest.raises((DraftGenerationNotReadyError, DraftPromotionStateError)):
        _promote(session, ps)
    assert _count(session) == 0


# -- atomic rollback --------------------------------------
def test_rollback_leaves_nothing_when_transition_would_fail(session: Session) -> None:
    ps = promotable_scenario(session)
    art = article_of(session, ps.article_id)
    art.status = ArticleStatus.APPROVED.value  # not drafting -> gate fails pre-tx
    session.flush()
    with pytest.raises(DraftPromotionStateError):
        _promote(session, ps)
    assert _count(session) == 0
    art = article_of(session, ps.article_id)
    assert art.status == ArticleStatus.APPROVED.value
    assert art.body is None


def test_source_run_never_mutated_by_promotion(session: Session) -> None:
    ps = promotable_scenario(session)
    run_before = session.get(DraftGenerationRun, ps.run_id)
    snap = {
        "status": run_before.status,
        "parsed_body": run_before.parsed_body,
        "parsed_meta_description": run_before.parsed_meta_description,
        "validation_report": run_before.validation_report,
        "raw_output": run_before.raw_output,
        "prompt_input_hash": run_before.prompt_input_hash,
        "rendered_prompt_hash": run_before.rendered_prompt_hash,
    }
    _promote(session, ps)
    run_after = session.get(DraftGenerationRun, ps.run_id)
    for k, v in snap.items():
        assert getattr(run_after, k) == v, k
