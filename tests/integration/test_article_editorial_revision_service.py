"""ArticleEditorialRevisionService: 採用後の改訂経路。

first-promotion semantics を弱めずに、promote 済み Article を安全に改訂できることと、
drift / no-op / cross-article / published 意図 の各ガードを検証する。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.editorial_revision_canonical import compute_revision_content_hash
from app.exceptions import (
    CanonicalContentChangedError,
    EditorialRevisionStateError,
    EntityNotFoundError,
)
from app.models import Article, ArticleDraftPromotion, ArticleEditorialRevision
from app.models.enums import ArticleStatus
from app.services.article_editorial_revision_service import (
    ArticleEditorialRevisionService,
)
from tests.support.draft_promotion_fixture import (
    article_of,
    promotable_scenario,
    promoted_scenario,
)

_LINK = "\n\n関連記事: [CRMおすすめ](https://bizfluxlab.com/crm-tools/)\n"


def _svc(session: Session) -> ArticleEditorialRevisionService:
    return ArticleEditorialRevisionService(session)


def _revise(session: Session, article_id: int, body: str, meta: str, **over):
    kwargs = dict(
        body_markdown=body,
        meta_description=meta,
        expected_current_body_hash=compute_text_hash(
            article_of(session, article_id).body or ""
        ),
        expected_current_meta_hash=compute_text_hash(
            article_of(session, article_id).meta_description or ""
        ),
        expected_revision_content_hash=compute_revision_content_hash(
            article_id=article_id, body_markdown=body, meta_description=meta
        ),
        revision_reason="内部リンクを追加",
    )
    kwargs.update(over)
    return _svc(session).revise(article_id, **kwargs)


def test_revision_updates_canonical_content_and_keeps_status(session: Session) -> None:
    ps = promoted_scenario(session)
    art = article_of(session, ps.article_id)
    assert str(art.status) == ArticleStatus.REVIEW.value
    new_body = ps.body_markdown + _LINK

    out = _revise(session, ps.article_id, new_body, ps.meta_description)

    assert out.already_applied is False
    assert out.article_status == ArticleStatus.REVIEW.value
    art = article_of(session, ps.article_id)
    assert art.body == new_body
    assert art.meta_description == ps.meta_description
    # status は改訂で動かない
    assert str(art.status) == ArticleStatus.REVIEW.value
    rev = out.revision
    assert rev.previous_body_hash == compute_text_hash(ps.body_markdown)
    assert rev.body_hash == compute_text_hash(new_body)
    assert rev.validation_report["overall"] == "pass"


def test_revision_requires_an_existing_promotion(session: Session) -> None:
    """promote されていない Article は改訂できない (first-promotion を迂回させない)。"""
    ps = promotable_scenario(session)
    art = article_of(session, ps.article_id)
    assert art.body is None

    with pytest.raises(EditorialRevisionStateError) as e:
        _revise(session, ps.article_id, ps.body_markdown, ps.meta_description)
    assert "article_has_promotion" in str(e.value)

    assert session.scalar(select(func.count()).select_from(ArticleEditorialRevision)) == 0
    assert article_of(session, ps.article_id).body is None


def test_revision_rejects_stale_expected_hashes(session: Session) -> None:
    ps = promoted_scenario(session)
    new_body = ps.body_markdown + _LINK

    with pytest.raises(CanonicalContentChangedError):
        _svc(session).revise(
            ps.article_id,
            body_markdown=new_body,
            meta_description=ps.meta_description,
            expected_current_body_hash=compute_text_hash("まったく別の本文"),
            expected_current_meta_hash=compute_text_hash(ps.meta_description),
            expected_revision_content_hash=compute_revision_content_hash(
                article_id=ps.article_id,
                body_markdown=new_body,
                meta_description=ps.meta_description,
            ),
            revision_reason="stale",
        )
    assert article_of(session, ps.article_id).body == ps.body_markdown
    assert session.scalar(select(func.count()).select_from(ArticleEditorialRevision)) == 0


def test_identical_content_is_a_noop(session: Session) -> None:
    ps = promoted_scenario(session)
    with pytest.raises(EditorialRevisionStateError) as e:
        _revise(session, ps.article_id, ps.body_markdown, ps.meta_description)
    assert "no-op" in str(e.value)
    assert session.scalar(select(func.count()).select_from(ArticleEditorialRevision)) == 0


def test_reapplying_the_same_revision_is_idempotent(session: Session) -> None:
    ps = promoted_scenario(session)
    new_body = ps.body_markdown + _LINK
    first = _revise(session, ps.article_id, new_body, ps.meta_description,
                    idempotency_key="rev-1")
    again = _revise(session, ps.article_id, new_body, ps.meta_description,
                    idempotency_key="rev-1")
    assert again.already_applied is True
    assert again.revision.id == first.revision.id
    assert session.scalar(select(func.count()).select_from(ArticleEditorialRevision)) == 1


def test_published_article_requires_explicit_update_intent(session: Session) -> None:
    ps = promoted_scenario(session)
    art = article_of(session, ps.article_id)
    art.status = ArticleStatus.PUBLISHED.value
    session.commit()
    new_body = ps.body_markdown + _LINK

    with pytest.raises(EditorialRevisionStateError) as e:
        _revise(session, ps.article_id, new_body, ps.meta_description)
    assert "published_update_intent_ok" in str(e.value)
    assert article_of(session, ps.article_id).body == ps.body_markdown

    out = _revise(session, ps.article_id, new_body, ps.meta_description,
                  published_update_intent="公開済み記事へ内部リンクを追加する")
    assert out.already_applied is False
    assert out.article_status == ArticleStatus.PUBLISHED.value
    assert article_of(session, ps.article_id).body == new_body
    assert out.revision.article_status_at_revision == ArticleStatus.PUBLISHED.value


def test_revision_rejects_content_failing_validators(session: Session) -> None:
    ps = promoted_scenario(session)
    with pytest.raises(EditorialRevisionStateError) as e:
        _revise(session, ps.article_id, "# H1 は禁止\n短い本文", ps.meta_description)
    assert "candidate_validation_pass" in str(e.value)
    assert article_of(session, ps.article_id).body == ps.body_markdown


def test_revision_is_bound_to_its_own_article(session: Session) -> None:
    """別記事の promotion を base にすることは構造的にできない。"""
    a = promoted_scenario(session, suffix="-a")
    b = promoted_scenario(session, suffix="-b")
    _revise(session, a.article_id, a.body_markdown + _LINK, a.meta_description)

    rev = session.scalars(
        select(ArticleEditorialRevision).where(
            ArticleEditorialRevision.article_id == a.article_id
        )
    ).one()
    base = session.get(ArticleDraftPromotion, rev.base_promotion_id)
    assert base.article_id == a.article_id
    # b 側は一切変わらない
    assert article_of(session, b.article_id).body == b.body_markdown
    # 同じ本文でも article_id が違えば revision_content_hash は別物
    assert compute_revision_content_hash(
        article_id=a.article_id, body_markdown=a.body_markdown,
        meta_description=a.meta_description
    ) != compute_revision_content_hash(
        article_id=b.article_id, body_markdown=a.body_markdown,
        meta_description=a.meta_description
    )


def test_revision_history_is_append_only(session: Session) -> None:
    ps = promoted_scenario(session)
    b1 = ps.body_markdown + _LINK
    b2 = b1 + "\n\n追記。\n"
    _revise(session, ps.article_id, b1, ps.meta_description)
    _revise(session, ps.article_id, b2, ps.meta_description)

    rows = _svc(session).list_for_article(ps.article_id)
    assert len(rows) == 2
    newest, older = rows[0], rows[1]
    assert newest.body_hash == compute_text_hash(b2)
    assert newest.previous_body_hash == compute_text_hash(b1)
    assert older.previous_body_hash == compute_text_hash(ps.body_markdown)
    assert article_of(session, ps.article_id).body == b2


def test_preview_is_read_only(session: Session) -> None:
    ps = promoted_scenario(session)
    new_body = ps.body_markdown + _LINK
    before = session.scalar(select(func.count()).select_from(ArticleEditorialRevision))

    out = _svc(session).preview(
        ps.article_id, body_markdown=new_body, meta_description=ps.meta_description,
        revision_reason="内部リンク追加")

    assert out.can_revise is True
    assert out.is_noop is False
    assert out.current_body_hash == compute_text_hash(ps.body_markdown)
    assert session.scalar(select(func.count()).select_from(ArticleEditorialRevision)) == before
    assert article_of(session, ps.article_id).body == ps.body_markdown


def test_get_rejects_a_revision_from_another_article(session: Session) -> None:
    a = promoted_scenario(session, suffix="-a")
    b = promoted_scenario(session, suffix="-b")
    out = _revise(session, a.article_id, a.body_markdown + _LINK, a.meta_description)
    with pytest.raises(EntityNotFoundError):
        _svc(session).get(b.article_id, out.revision.id)


def test_unknown_article_raises(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _svc(session).revise(
            999_999,
            body_markdown="x", meta_description="y",
            expected_current_body_hash=compute_text_hash(""),
            expected_current_meta_hash=compute_text_hash(""),
            expected_revision_content_hash=compute_revision_content_hash(
                article_id=999_999, body_markdown="x", meta_description="y"),
            revision_reason="r")


def test_promotion_remains_first_promotion_only(session: Session) -> None:
    """改訂を入れても promotion 経路は 2 度目を受け付けない。"""
    from app.article.draft_promotion_canonical import compute_candidate_content_hash
    from app.exceptions import DraftPromotionStateError
    from app.services.article_draft_promotion_service import (
        ArticleDraftPromotionService,
    )

    ps = promoted_scenario(session)
    _revise(session, ps.article_id, ps.body_markdown + _LINK, ps.meta_description)
    body = ps.body_markdown + "\n\n別の追記。\n"
    with pytest.raises(DraftPromotionStateError):
        ArticleDraftPromotionService(session).promote(
            ps.article_id,
            source_run_id=ps.run_id,
            body_markdown=body,
            meta_description=ps.meta_description,
            expected_body_hash=compute_text_hash(body),
            expected_meta_hash=compute_text_hash(ps.meta_description),
            expected_candidate_content_hash=compute_candidate_content_hash(
                article_id=ps.article_id, source_run_id=ps.run_id,
                body_markdown=body, meta_description=ps.meta_description),
        )
    assert session.scalar(
        select(func.count()).select_from(ArticleDraftPromotion).where(
            ArticleDraftPromotion.article_id == ps.article_id)) == 1


def test_article_delete_cascades_revisions(session: Session) -> None:
    ps = promoted_scenario(session)
    _revise(session, ps.article_id, ps.body_markdown + _LINK, ps.meta_description)
    session.delete(session.get(Article, ps.article_id))
    session.commit()
    assert session.scalar(select(func.count()).select_from(ArticleEditorialRevision)) == 0


def test_a_revised_article_still_passes_the_publication_hash_check(
    session: Session,
) -> None:
    """改訂後も公開前 validator の hash 照合が通る。

    promotion の hash と比べると、正規の改訂経路を通った記事が必ず fail する。
    """
    from app.services.wordpress_preview_service import WordPressPreviewService

    ps = promoted_scenario(session)
    before = WordPressPreviewService(session).preview(ps.article_id)
    assert "body_hash_matches_promotion" not in {
        c["id"] for c in before.validation_report["checks"] if c["level"] == "fail"
    }

    _revise(session, ps.article_id, ps.body_markdown + _LINK, ps.meta_description)

    after = WordPressPreviewService(session).preview(ps.article_id)
    failed = {c["id"] for c in after.validation_report["checks"] if c["level"] == "fail"}
    assert "body_hash_matches_promotion" not in failed
    assert "meta_hash_matches_promotion" not in failed
