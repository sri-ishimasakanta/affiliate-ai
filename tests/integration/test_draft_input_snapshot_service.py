"""DraftInputSnapshotService の preview / freeze gate / drift / idempotency / tx。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.exceptions import (
    DraftInputNotReadyError,
    EntityNotFoundError,
    SnapshotInputChangedError,
)
from app.models import (
    AffiliateProgram,
    Article,
    ArticleAffiliateProgram,
    ArticleFact,
    DraftInputSnapshot,
)
from app.models.draft_input_snapshot import (
    BUILDER_VERSION,
    PLAN_SNAPSHOT_ORIGIN,
    SNAPSHOT_VERSION,
)
from app.repositories.draft_input_snapshot_repository import (
    DraftInputSnapshotRepository,
)
from app.services.draft_input_snapshot_service import DraftInputSnapshotService
from tests.support.draft_input_fixture import build_scenario


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(DraftInputSnapshot))


def _svc(session: Session) -> DraftInputSnapshotService:
    return DraftInputSnapshotService(session)


# -- preview --------------------------------------------------------


def test_preview_is_read_only(session: Session) -> None:
    sc = build_scenario(session, n_tools=7)
    out = _svc(session).preview(sc.article_id, now=sc.now)
    assert _count(session) == 0
    assert out.content_hash
    assert out.snapshot_version == SNAPSHOT_VERSION
    assert out.gate_status.can_freeze is True
    assert out.gate_status.failed_gates == []
    assert out.payload["readiness"]["drafting_allowed"] is True


# -- freeze success / idempotency --------------------------------


def test_freeze_success(session: Session) -> None:
    sc = build_scenario(session, n_tools=7)
    preview = _svc(session).preview(sc.article_id, now=sc.now)
    resp = _svc(session).freeze(
        sc.article_id, preview.content_hash, now=sc.now
    )
    assert resp.already_frozen is False
    assert _count(session) == 1
    snap = resp.snapshot
    assert snap.content_hash == preview.content_hash
    assert snap.snapshot_version == SNAPSHOT_VERSION
    assert snap.builder_version == BUILDER_VERSION
    assert snap.plan_snapshot_origin == PLAN_SNAPSHOT_ORIGIN
    # 親行の非正規化フィールドは payload と一致
    assert snap.primary_affiliate_program_id == sc.primary_program_id
    assert snap.comparison_program_ids == sorted(sc.program_ids)
    assert snap.drafting_allowed_at_freeze is True
    assert snap.payload["selection"]["primary_affiliate_program_id"] == (
        sc.primary_program_id
    )
    # Article は planned のまま / body None
    art = session.get(Article, sc.article_id)
    assert art.status == "planned"
    assert art.body is None


def test_freeze_is_idempotent_for_same_hash(session: Session) -> None:
    sc = build_scenario(session, n_tools=5)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    first = _svc(session).freeze(sc.article_id, h, now=sc.now)
    second = _svc(session).freeze(sc.article_id, h, now=sc.now)
    assert first.already_frozen is False
    assert second.already_frozen is True
    assert second.snapshot.id == first.snapshot.id
    assert _count(session) == 1


def test_recommended_missing_and_unknown_do_not_block_freeze(session: Session) -> None:
    sc = build_scenario(session, n_tools=3, with_unknown=True)
    preview = _svc(session).preview(sc.article_id, now=sc.now)
    resp = _svc(session).freeze(sc.article_id, preview.content_hash, now=sc.now)
    assert resp.already_frozen is False
    tool0 = resp.snapshot.payload["tools"][0]
    assert "ai_features" in tool0["do_not_claim"]
    assert any(c["state"] == "not_researched" for c in tool0["cells"])


# -- drift guard --------------------------------------------------


def test_freeze_rejects_hash_mismatch(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    with pytest.raises(SnapshotInputChangedError):
        _svc(session).freeze(sc.article_id, "0" * 64, now=sc.now)
    assert _count(session) == 0


def test_freeze_rejects_after_input_drift(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    stale_hash = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    # preview 後に fact を変更
    row = session.scalars(
        select(ArticleFact).where(ArticleFact.fact_key == "official_url").limit(1)
    ).one()
    row.fact_value = "https://example.com/changed"
    session.commit()
    with pytest.raises(SnapshotInputChangedError):
        _svc(session).freeze(sc.article_id, stale_hash, now=sc.now)
    assert _count(session) == 0


def test_freeze_rejects_after_primary_drift(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    stale_hash = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    links = session.scalars(
        select(ArticleAffiliateProgram)
        .where(ArticleAffiliateProgram.article_id == sc.article_id)
        .order_by(ArticleAffiliateProgram.id)
    ).all()
    links[0].is_primary = False
    links[1].is_primary = True
    session.commit()
    with pytest.raises(SnapshotInputChangedError):
        _svc(session).freeze(sc.article_id, stale_hash, now=sc.now)
    assert _count(session) == 0


def test_unrelated_change_keeps_hash_and_allows_freeze(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    # 参照されない Source 追加は semantic hash を変えない
    from app.models import Source

    art = session.get(Article, sc.article_id)
    session.add(
        Source(
            article_id=art.id, source_type="official_help",
            source_url="https://example.com/unrelated", title="x",
            checked_at=sc.now - timedelta(days=1),
        )
    )
    session.commit()
    resp = _svc(session).freeze(sc.article_id, h, now=sc.now)
    assert resp.already_frozen is False
    assert _count(session) == 1


# -- freeze gates (all -> DraftInputNotReadyError, row 0) ---------


@pytest.mark.parametrize(
    "mutate,gate",
    [
        (lambda s, sc: setattr(s.get(Article, sc.article_id), "status", "drafting"),
         "article_not_planned"),
        (lambda s, sc: setattr(s.get(Article, sc.article_id), "body", "x"),
         "article_body_present"),
        (lambda s, sc: setattr(s.get(Article, sc.article_id), "meta_description", "x"),
         "article_meta_description_present"),
        (lambda s, sc: setattr(s.get(Article, sc.article_id), "published_url", "x"),
         "article_published_url_present"),
        (lambda s, sc: setattr(s.get(Article, sc.article_id), "wordpress_post_id", 7),
         "article_wordpress_post_id_present"),
    ],
)
def test_freeze_gate_article_fields(session: Session, mutate, gate) -> None:
    sc = build_scenario(session, n_tools=3)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    mutate(session, sc)
    session.commit()
    with pytest.raises(DraftInputNotReadyError) as exc:
        _svc(session).freeze(sc.article_id, h, now=sc.now)
    assert gate in str(exc.value)
    assert _count(session) == 0


def test_freeze_gate_primary_zero(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    for link in session.scalars(
        select(ArticleAffiliateProgram).where(
            ArticleAffiliateProgram.article_id == sc.article_id
        )
    ):
        link.is_primary = False
    session.commit()
    with pytest.raises(DraftInputNotReadyError) as exc:
        _svc(session).freeze(sc.article_id, h, now=sc.now)
    assert "primary_not_exactly_one" in str(exc.value)
    assert _count(session) == 0


def test_freeze_gate_primary_multiple(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    links = list(session.scalars(
        select(ArticleAffiliateProgram).where(
            ArticleAffiliateProgram.article_id == sc.article_id
        )
    ))
    links[1].is_primary = True
    session.commit()
    with pytest.raises(DraftInputNotReadyError) as exc:
        _svc(session).freeze(sc.article_id, h, now=sc.now)
    assert "primary_not_exactly_one" in str(exc.value)
    assert _count(session) == 0


def test_freeze_gate_inactive_affiliate(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    prog = session.get(AffiliateProgram, sc.program_ids[1])
    prog.status = "paused"
    session.commit()
    with pytest.raises(DraftInputNotReadyError) as exc:
        _svc(session).freeze(sc.article_id, h, now=sc.now)
    assert "inactive_affiliate_program" in str(exc.value)
    assert _count(session) == 0


def test_freeze_gate_factpack_not_ready(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    row = session.scalars(
        select(ArticleFact).where(ArticleFact.fact_key == "official_url").limit(1)
    ).one()
    session.delete(row)
    session.commit()
    with pytest.raises(DraftInputNotReadyError) as exc:
        _svc(session).freeze(sc.article_id, h, now=sc.now)
    assert "factpack_drafting_not_allowed" in str(exc.value)
    assert _count(session) == 0


def test_freeze_gate_required_fact_stale(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    row = session.scalars(
        select(ArticleFact).where(ArticleFact.fact_key == "pricing_summary").limit(1)
    ).one()
    row.checked_at = sc.now - timedelta(days=400)
    session.commit()
    with pytest.raises(DraftInputNotReadyError) as exc:
        _svc(session).freeze(sc.article_id, h, now=sc.now)
    msg = str(exc.value)
    assert "required_fact_stale" in msg or "freshness_not_within_policy" in msg
    assert _count(session) == 0


def test_freeze_gate_article_plan_build_failure(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    # keyword を消すと ArticlePlan build 不能
    session.query(ArticleFact).filter(ArticleFact.article_id == sc.article_id)
    art = session.get(Article, sc.article_id)
    art.keyword_id = None
    session.commit()
    with pytest.raises(DraftInputNotReadyError):
        _svc(session).freeze(sc.article_id, "a" * 64, now=sc.now)
    assert _count(session) == 0


# -- transaction / repository -------------------------------------


def test_repository_never_commits(session: Session) -> None:
    sc = build_scenario(session, n_tools=2)
    result = DraftInputSnapshotService(session)._builder.build(
        sc.article_id, now=sc.now
    )
    DraftInputSnapshotRepository(session).append(
        article_id=sc.article_id,
        snapshot_version=result.snapshot_version,
        builder_version=result.builder_version,
        plan_snapshot_origin=result.plan_snapshot_origin,
        primary_affiliate_program_id=result.primary_affiliate_program_id,
        comparison_program_ids=result.comparison_program_ids,
        drafting_allowed_at_freeze=result.drafting_allowed_at_freeze,
        payload=result.payload,
        content_hash=result.content_hash,
        frozen_at=sc.now,
    )
    session.rollback()
    assert _count(session) == 0


def test_freeze_rolls_back_on_commit_failure(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sc = build_scenario(session, n_tools=3)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash

    def boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", boom)
    with pytest.raises(RuntimeError):
        _svc(session).freeze(sc.article_id, h, now=sc.now)
    monkeypatch.undo()
    assert _count(session) == 0


# -- list / get / cascade ---------------------------------------


def test_list_returns_summary_and_get_returns_payload(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    frozen = _svc(session).freeze(sc.article_id, h, now=sc.now)

    listing = _svc(session).list_for_article(sc.article_id)
    assert len(listing) == 1
    assert not hasattr(listing[0], "payload")
    assert listing[0].content_hash == h

    detail = _svc(session).get(sc.article_id, frozen.snapshot.id)
    assert detail.payload["snapshot_version"] == SNAPSHOT_VERSION


def test_get_rejects_snapshot_from_other_article(session: Session) -> None:
    sc = build_scenario(session, n_tools=2)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    snap = _svc(session).freeze(sc.article_id, h, now=sc.now).snapshot
    other = build_scenario(session, n_tools=2, suffix="b")
    with pytest.raises(EntityNotFoundError):
        _svc(session).get(other.article_id, snap.id)


def test_article_delete_cascades_snapshot(session: Session) -> None:
    sc = build_scenario(session, n_tools=2)
    h = _svc(session).preview(sc.article_id, now=sc.now).content_hash
    _svc(session).freeze(sc.article_id, h, now=sc.now)
    assert _count(session) == 1
    art = session.get(Article, sc.article_id)
    session.delete(art)
    session.commit()
    assert _count(session) == 0
