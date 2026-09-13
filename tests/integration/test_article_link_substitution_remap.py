"""D-D1.2: ``ArticleLinkSubstitutionService.remap_occurrence`` — atomic
same-occurrence mapping remap.

Covers:
  - all-or-nothing rollback at each failure point in the 3-step primitive
    sequence (mark_superseded_for_remap / add_active / link_superseded_by)
  - same-occurrence inheritance (article_id / occurrence_identity_hash /
    original_href are never re-accepted from the caller)
  - interaction with the (article_id, occurrence_identity_hash) WHERE
    status='active' partial unique index
  - superseded_by_id integrity (self-supersede impossible, set-once, revoked
    rows cannot gain it, no cycles across a remap chain)
  - remap idempotency
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.exceptions import ArticleLinkSubstitutionMappingError, EntityNotFoundError
from app.models import AffiliateLinkTarget, Article, ArticleLinkSubstitutionMapping
from app.models.article_link_substitution_mapping import (
    ALSM_ACTIVE,
    ALSM_REVOKED,
    ALSM_SUPERSEDED,
)
from app.repositories.article_link_substitution_mapping_repository import (
    ArticleLinkSubstitutionMappingRepository,
)
from app.services.article_link_substitution_service import (
    ArticleLinkSubstitutionService,
)

_HREF = "https://official.example.test/tool-a"


def _svc(session: Session) -> ArticleLinkSubstitutionService:
    return ArticleLinkSubstitutionService(session)


def _seed_article(session: Session, slug: str = "p1") -> Article:
    art = Article(title="t", slug=slug, keyword_id=None, body="# b\n")
    session.add(art)
    session.commit()
    return art


def _seed_target(
    session: Session,
    *,
    article_id: int,
    token: str | None = None,
    affiliate_program_id: int = 1,
) -> AffiliateLinkTarget:
    # token は全体で unique 制約を持つため、呼び出し側が明示しない限り
    # article_id/affiliate_program_id から一意なデフォルト値を作る。
    token = token or f"TOK{article_id:04d}{affiliate_program_id:04d}00000000000"
    target = AffiliateLinkTarget(
        token=token,
        article_id=article_id,
        affiliate_program_id=affiliate_program_id,
        destination_url="https://aff.example.test/x",
        destination_host="aff.example.test",
        status="active",
        link_identity_hash="1" * 64,
    )
    session.add(target)
    session.commit()
    return target


def _all_mappings(session: Session) -> list[ArticleLinkSubstitutionMapping]:
    return list(session.execute(select(ArticleLinkSubstitutionMapping)).scalars().all())


# ==================== A: failure before old transition (§15.A) ============
def test_rollback_before_old_transition_missing_new_target(session: Session) -> None:
    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )

    with pytest.raises(EntityNotFoundError):
        svc.remap_occurrence(old.id, new_affiliate_link_target_id=999999)

    refreshed_old = svc.get(old.id)
    assert refreshed_old.status == ALSM_ACTIVE
    assert refreshed_old.superseded_by_id is None
    assert len(_all_mappings(session)) == 1


def test_rollback_before_old_transition_cross_article_target(session: Session) -> None:
    art1 = _seed_article(session, slug="p1")
    art2 = _seed_article(session, slug="p2")
    target1 = _seed_target(session, article_id=art1.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art2.id, affiliate_program_id=1)
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art1.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )

    with pytest.raises(ArticleLinkSubstitutionMappingError, match="different article"):
        svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)

    refreshed_old = svc.get(old.id)
    assert refreshed_old.status == ALSM_ACTIVE
    assert refreshed_old.superseded_by_id is None
    assert len(_all_mappings(session)) == 1


def test_rollback_before_old_transition_not_active(session: Session) -> None:
    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art.id, affiliate_program_id=2)
    svc = _svc(session)
    m = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )
    svc.revoke_mapping(m.id)

    with pytest.raises(
        ArticleLinkSubstitutionMappingError, match="requires an active mapping"
    ):
        svc.remap_occurrence(m.id, new_affiliate_link_target_id=target2.id)

    refreshed = svc.get(m.id)
    assert refreshed.status == ALSM_REVOKED
    assert refreshed.superseded_by_id is None
    assert len(_all_mappings(session)) == 1


# ==================== B/C: failure mid-transaction (§15.B/C) ==============
def test_rollback_after_old_transition_but_before_new_insert(session: Session) -> None:
    """mark_superseded_for_remap は成功したが add_active が失敗するケース。"""

    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art.id, affiliate_program_id=2)
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )

    with patch.object(
        ArticleLinkSubstitutionMappingRepository,
        "add_active",
        side_effect=RuntimeError("boom-before-insert"),
    ):
        with pytest.raises(RuntimeError, match="boom-before-insert"):
            svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)

    refreshed_old = svc.get(old.id)
    assert refreshed_old.status == ALSM_ACTIVE  # rollback で active に戻る
    assert refreshed_old.superseded_by_id is None
    all_mappings = _all_mappings(session)
    assert len(all_mappings) == 1
    assert all_mappings[0].id == old.id


def test_rollback_after_new_insert_but_before_link(session: Session) -> None:
    """add_active は成功したが link_superseded_by が失敗するケース。"""

    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art.id, affiliate_program_id=2)
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )

    with patch.object(
        ArticleLinkSubstitutionMappingRepository,
        "link_superseded_by",
        side_effect=RuntimeError("boom-before-link"),
    ):
        with pytest.raises(RuntimeError, match="boom-before-link"):
            svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)

    refreshed_old = svc.get(old.id)
    assert refreshed_old.status == ALSM_ACTIVE  # rollback で active に戻る
    assert refreshed_old.superseded_by_id is None
    all_mappings = _all_mappings(session)
    assert len(all_mappings) == 1  # new row は absent (rollback で消える)
    assert all_mappings[0].id == old.id


# ==================== D: successful atomic remap (§15.D) ===================
def test_successful_atomic_remap_full_end_state(session: Session) -> None:
    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art.id, affiliate_program_id=2)
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )

    new = svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)

    refreshed_old = svc.get(old.id)
    assert refreshed_old.status == ALSM_SUPERSEDED
    assert refreshed_old.superseded_by_id == new.id
    assert new.status == ALSM_ACTIVE
    assert new.superseded_by_id is None

    active_rows = svc.list_active_for_article(art.id)
    assert [m.id for m in active_rows] == [new.id]

    all_mappings = _all_mappings(session)
    assert len(all_mappings) == 2
    superseded_rows = [m for m in all_mappings if m.status == ALSM_SUPERSEDED]
    assert [m.id for m in superseded_rows] == [old.id]


# ==================== same-occurrence inheritance (§16) =====================
def test_remap_inherits_article_id_occurrence_hash_and_original_href_exactly(
    session: Session,
) -> None:
    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art.id, affiliate_program_id=2)
    svc = _svc(session)
    weird_href = "https://Official.Example.TEST/Tool-A?x=1&y=2#frag "
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=weird_href, affiliate_link_target_id=target1.id,
    )

    new = svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)

    assert new.article_id == old.article_id == art.id
    assert new.occurrence_identity_hash == old.occurrence_identity_hash == "a" * 64
    assert new.original_href == old.original_href == weird_href
    # target だけが変わる。
    assert old.affiliate_link_target_id == target1.id
    assert new.affiliate_link_target_id == target2.id


def test_remap_occurrence_signature_has_no_occurrence_override_parameters() -> None:
    """呼び出し側が article_id / occurrence_identity_hash / original_href を
    渡す余地が構造的に存在しないことをシグネチャで固定する — 「別 occurrence への
    remap」は API 上表現不可能であることの証明。"""

    params = set(inspect.signature(ArticleLinkSubstitutionService.remap_occurrence).parameters)
    assert params == {
        "self",
        "old_mapping_id",
        "new_affiliate_link_target_id",
        "idempotency_key",
        "approved_at",
    }
    assert "article_id" not in params
    assert "occurrence_identity_hash" not in params
    assert "original_href" not in params


# ==================== partial unique index interaction (§17) ===============
def test_one_active_row_before_remap(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    svc = _svc(session)
    svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    assert len(svc.list_active_for_article(art.id)) == 1


def test_direct_second_active_insert_for_same_occurrence_still_fails(
    session: Session,
) -> None:
    """通常の (remap を経由しない) 外部操作は partial unique index に阻まれる。"""

    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    repo = ArticleLinkSubstitutionMappingRepository(session)
    repo.add_active(
        article_id=art.id, occurrence_identity_hash="a" * 64, original_href=_HREF,
        affiliate_link_target_id=target.id, approved_at=datetime.now(UTC),
        idempotency_key=None,
    )
    session.commit()
    with pytest.raises(IntegrityError):
        repo.add_active(
            article_id=art.id, occurrence_identity_hash="a" * 64, original_href=_HREF,
            affiliate_link_target_id=target.id, approved_at=datetime.now(UTC),
            idempotency_key=None,
        )
    session.rollback()


def test_remap_succeeds_because_old_transitions_before_new_insert(
    session: Session,
) -> None:
    """remap は old を先に superseded にしてから新 active row を挿入するので、
    直接 insert と違って partial unique index に阻まれない。"""

    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art.id, affiliate_program_id=2)
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )
    new = svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)
    assert new.status == ALSM_ACTIVE


def test_after_commit_exactly_one_active_and_one_superseded_historical_row(
    session: Session,
) -> None:
    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art.id, affiliate_program_id=2)
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )
    svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)

    by_status: dict[str, list[ArticleLinkSubstitutionMapping]] = {}
    for m in _all_mappings(session):
        by_status.setdefault(m.status, []).append(m)

    assert len(by_status.get(ALSM_ACTIVE, [])) == 1
    assert len(by_status.get(ALSM_SUPERSEDED, [])) == 1
    assert ALSM_REVOKED not in by_status


# ==================== superseded_by integrity (§18) =========================
def test_self_supersede_impossible_via_repository_guard(session: Session) -> None:
    """service API では自己参照を構築しようがないが (new row は常に fresh な
    autoincrement id)、repository の narrow primitive にも defense-in-depth の
    guard があることを直接確認する。"""

    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    repo = ArticleLinkSubstitutionMappingRepository(session)
    m = repo.add_active(
        article_id=art.id, occurrence_identity_hash="a" * 64, original_href=_HREF,
        affiliate_link_target_id=target.id, approved_at=datetime.now(UTC),
        idempotency_key=None,
    )
    session.commit()
    repo.mark_superseded_for_remap(m)
    with pytest.raises(ArticleLinkSubstitutionMappingError, match="itself"):
        repo.link_superseded_by(m, new_id=m.id)
    session.rollback()


def test_superseded_by_id_set_exactly_once_at_repository_level(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    repo = ArticleLinkSubstitutionMappingRepository(session)
    old = repo.add_active(
        article_id=art.id, occurrence_identity_hash="a" * 64, original_href=_HREF,
        affiliate_link_target_id=target.id, approved_at=datetime.now(UTC),
        idempotency_key=None,
    )
    replacement1 = repo.add_active(
        article_id=art.id, occurrence_identity_hash="b" * 64, original_href=_HREF,
        affiliate_link_target_id=target.id, approved_at=datetime.now(UTC),
        idempotency_key=None,
    )
    replacement2 = repo.add_active(
        article_id=art.id, occurrence_identity_hash="c" * 64, original_href=_HREF,
        affiliate_link_target_id=target.id, approved_at=datetime.now(UTC),
        idempotency_key=None,
    )
    session.commit()

    repo.mark_superseded_for_remap(old)
    repo.link_superseded_by(old, new_id=replacement1.id)
    with pytest.raises(ArticleLinkSubstitutionMappingError, match="already has"):
        repo.link_superseded_by(old, new_id=replacement2.id)

    assert old.superseded_by_id == replacement1.id  # 1 回目の値が不変
    session.rollback()


def test_revoked_row_cannot_gain_superseded_by_id(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    repo = ArticleLinkSubstitutionMappingRepository(session)
    m = repo.add_active(
        article_id=art.id, occurrence_identity_hash="a" * 64, original_href=_HREF,
        affiliate_link_target_id=target.id, approved_at=datetime.now(UTC),
        idempotency_key=None,
    )
    other = repo.add_active(
        article_id=art.id, occurrence_identity_hash="b" * 64, original_href=_HREF,
        affiliate_link_target_id=target.id, approved_at=datetime.now(UTC),
        idempotency_key=None,
    )
    session.commit()

    repo.revoke(m)
    with pytest.raises(ArticleLinkSubstitutionMappingError, match="must be superseded"):
        repo.link_superseded_by(m, new_id=other.id)
    assert m.superseded_by_id is None
    session.rollback()


def test_remap_chain_has_no_cycle(session: Session) -> None:
    """A -> remap -> B -> remap -> C という chain を作り、各 superseded_by_id が
    常に「後から作られた新しい行」だけを前方参照し、循環が生じないことを確認する。"""

    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art.id, affiliate_program_id=2)
    target3 = _seed_target(session, article_id=art.id, affiliate_program_id=3)
    svc = _svc(session)

    a = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )
    b = svc.remap_occurrence(a.id, new_affiliate_link_target_id=target2.id)
    c = svc.remap_occurrence(b.id, new_affiliate_link_target_id=target3.id)

    refreshed_a = svc.get(a.id)
    refreshed_b = svc.get(b.id)
    refreshed_c = svc.get(c.id)

    assert refreshed_a.status == ALSM_SUPERSEDED
    assert refreshed_a.superseded_by_id == b.id
    assert refreshed_b.status == ALSM_SUPERSEDED
    assert refreshed_b.superseded_by_id == c.id
    assert refreshed_c.status == ALSM_ACTIVE
    assert refreshed_c.superseded_by_id is None

    # 常に id が増える方向にしか superseded_by_id は張られない -> 循環不可能。
    assert refreshed_a.id < refreshed_b.id < refreshed_c.id
    # occurrence identity は chain 全体で不変。
    assert {
        refreshed_a.occurrence_identity_hash,
        refreshed_b.occurrence_identity_hash,
        refreshed_c.occurrence_identity_hash,
    } == {"a" * 64}


# ==================== remap idempotency (§14) ===============================
def test_remap_idempotency_same_key_same_identity_returns_existing(
    session: Session,
) -> None:
    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art.id, affiliate_program_id=2)
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )

    a = svc.remap_occurrence(
        old.id, new_affiliate_link_target_id=target2.id, idempotency_key="remap-k1"
    )
    b = svc.remap_occurrence(
        old.id, new_affiliate_link_target_id=target2.id, idempotency_key="remap-k1"
    )
    assert a.id == b.id
    # retry がもう 1 行 replacement を作っていないこと (old + 1 replacement のみ)。
    assert len(_all_mappings(session)) == 2


def test_remap_idempotency_same_key_different_identity_rejected(
    session: Session,
) -> None:
    art = _seed_article(session)
    target1 = _seed_target(session, article_id=art.id, affiliate_program_id=1)
    target2 = _seed_target(session, article_id=art.id, affiliate_program_id=2)
    target3 = _seed_target(session, article_id=art.id, affiliate_program_id=3)
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )
    svc.remap_occurrence(
        old.id, new_affiliate_link_target_id=target2.id, idempotency_key="remap-k2"
    )

    other_old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="b" * 64,
        original_href=_HREF, affiliate_link_target_id=target3.id,
    )
    with pytest.raises(
        ArticleLinkSubstitutionMappingError, match="different remap identity"
    ):
        svc.remap_occurrence(
            other_old.id, new_affiliate_link_target_id=target2.id,
            idempotency_key="remap-k2",
        )
