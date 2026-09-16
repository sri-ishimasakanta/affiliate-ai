"""ArticleLinkSubstitutionService / ArticleLinkSubstitutionMappingRepository —
creation / active uniqueness / lifecycle (remap/revoke) / cross-article guard /
idempotency。

D-D1.2: 旧 ``supersede_mapping(old_id, preexisting_replacement_id)`` API は削除
された (任意の無関係な mapping を replacement として指せてしまう危険な形だった)。
その代わりの atomic ``remap_occurrence`` の deep-dive カバレッジ (rollback /
same-occurrence 継承 / partial-unique 相互作用 / superseded_by 整合性) は
``tests/integration/test_article_link_substitution_remap.py`` に分離してある。
このファイルは D-D1 由来の一般的な CRUD-ish カバレッジを保持する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

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
    token: str = "AAAA0000tokenone00000",
    affiliate_program_id: int = 1,
) -> AffiliateLinkTarget:
    # AffiliateLinkTarget にも (article_id, affiliate_program_id) の partial
    # unique active index があるため、同一 article に複数 active target を作る
    # テストでは affiliate_program_id を変える必要がある。
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


# ==================== creation =========================================
def test_create_mapping_happy_path(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    m = _svc(session).create_mapping(
        article_id=art.id,
        occurrence_identity_hash="a" * 64,
        original_href=_HREF,
        affiliate_link_target_id=target.id,
    )
    assert m.status == ALSM_ACTIVE
    assert m.article_id == art.id
    assert m.affiliate_link_target_id == target.id
    assert m.superseded_by_id is None
    assert m.approved_at is not None


def test_original_href_retained_exactly_unmodified(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    weird_href = "https://Official.Example.TEST/Tool-A?x=1&y=2#frag "  # 大小文字/空白そのまま
    m = _svc(session).create_mapping(
        article_id=art.id,
        occurrence_identity_hash="a" * 64,
        original_href=weird_href,
        affiliate_link_target_id=target.id,
    )
    assert m.original_href == weird_href  # 1 文字も変更されていない


@pytest.mark.parametrize(
    "bad_hash",
    ["", "short", "g" * 64, "A" * 64, "a" * 63, "a" * 65, None],
)
def test_occurrence_identity_hash_shape_validation(session: Session, bad_hash) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    with pytest.raises(ArticleLinkSubstitutionMappingError, match="64-character hex"):
        _svc(session).create_mapping(
            article_id=art.id,
            occurrence_identity_hash=bad_hash,
            original_href=_HREF,
            affiliate_link_target_id=target.id,
        )


def test_create_mapping_missing_target_raises_not_found(session: Session) -> None:
    art = _seed_article(session)
    with pytest.raises(EntityNotFoundError):
        _svc(session).create_mapping(
            article_id=art.id,
            occurrence_identity_hash="a" * 64,
            original_href=_HREF,
            affiliate_link_target_id=999999,
        )


# ==================== active uniqueness =================================
def test_same_occurrence_cannot_have_two_active_mappings(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    svc = _svc(session)
    svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    with pytest.raises(ArticleLinkSubstitutionMappingError, match="already exists"):
        svc.create_mapping(
            article_id=art.id, occurrence_identity_hash="a" * 64,
            original_href=_HREF, affiliate_link_target_id=target.id,
        )


def test_active_partial_unique_index_enforced_at_db_level(session: Session) -> None:
    """service-level pre-check をバイパスして repository へ直接 2 件 active を
    insert しようとした場合、DB の partial unique index 自体が拒否する。"""

    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    repo = ArticleLinkSubstitutionMappingRepository(session)
    repo.add_active(
        article_id=art.id, occurrence_identity_hash="a" * 64, original_href=_HREF,
        affiliate_link_target_id=target.id, approved_at=datetime.now(UTC), idempotency_key=None,
    )
    session.commit()
    with pytest.raises(IntegrityError):
        repo.add_active(
            article_id=art.id, occurrence_identity_hash="a" * 64, original_href=_HREF,
            affiliate_link_target_id=target.id, approved_at=datetime.now(UTC),
            idempotency_key=None,
        )
    session.rollback()


def test_historical_rows_coexist_with_fresh_active_row_for_same_occurrence(
    session: Session,
) -> None:
    art = _seed_article(session)
    target1 = _seed_target(
        session, article_id=art.id, token="AAAA0000tokenone00000", affiliate_program_id=1
    )
    target2 = _seed_target(
        session, article_id=art.id, token="BBBB0000tokentwoo0000", affiliate_program_id=2
    )
    target3 = _seed_target(
        session, article_id=art.id, token="CCCC0000tokenthre0000", affiliate_program_id=3
    )
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )
    remapped = svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)
    svc.revoke_mapping(remapped.id)

    # occurrence "a"*64 に active row は今 0 件 (old=superseded, remapped=revoked)。
    # partial unique index は status='active' のみを見るため、create_mapping で
    # 同じ occurrence に対して独立に新しい active row を作れる。
    revived = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target3.id,
    )
    assert revived.status == ALSM_ACTIVE
    refreshed_old = svc.get(old.id)
    refreshed_remapped = svc.get(remapped.id)
    assert refreshed_old.status == ALSM_SUPERSEDED
    assert refreshed_remapped.status == ALSM_REVOKED


# ==================== lifecycle: remap (D-D1.2) ==========================
def test_active_to_superseded_via_remap(session: Session) -> None:
    art = _seed_article(session)
    target1 = _seed_target(
        session, article_id=art.id, token="AAAA0000tokenone00000", affiliate_program_id=1
    )
    target2 = _seed_target(
        session, article_id=art.id, token="BBBB0000tokentwoo0000", affiliate_program_id=2
    )
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


def test_active_to_revoked(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    svc = _svc(session)
    m = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    result = svc.revoke_mapping(m.id)
    assert result.status == ALSM_REVOKED
    assert result.superseded_by_id is None  # §6: revoked は superseded_by_id を持たない


def test_remap_then_remap_again_rejected(session: Session) -> None:
    art = _seed_article(session)
    target1 = _seed_target(
        session, article_id=art.id, token="AAAA0000tokenone00000", affiliate_program_id=1
    )
    target2 = _seed_target(
        session, article_id=art.id, token="BBBB0000tokentwoo0000", affiliate_program_id=2
    )
    target3 = _seed_target(
        session, article_id=art.id, token="CCCC0000tokenthre0000", affiliate_program_id=3
    )
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )
    first_new = svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)
    with pytest.raises(
        ArticleLinkSubstitutionMappingError, match="requires an active mapping"
    ):
        svc.remap_occurrence(old.id, new_affiliate_link_target_id=target3.id)

    # 1 回目の remap 結果は不変のまま。
    refreshed_old = svc.get(old.id)
    assert refreshed_old.superseded_by_id == first_new.id


def test_remap_then_revoke_rejected(session: Session) -> None:
    art = _seed_article(session)
    target1 = _seed_target(
        session, article_id=art.id, token="AAAA0000tokenone00000", affiliate_program_id=1
    )
    target2 = _seed_target(
        session, article_id=art.id, token="BBBB0000tokentwoo0000", affiliate_program_id=2
    )
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )
    svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)
    with pytest.raises(ArticleLinkSubstitutionMappingError, match="not allowed"):
        svc.revoke_mapping(old.id)


def test_revoke_then_revoke_again_rejected(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    svc = _svc(session)
    m = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    svc.revoke_mapping(m.id)
    with pytest.raises(ArticleLinkSubstitutionMappingError, match="not allowed"):
        svc.revoke_mapping(m.id)


def test_revoke_then_remap_rejected(session: Session) -> None:
    art = _seed_article(session)
    target1 = _seed_target(
        session, article_id=art.id, token="AAAA0000tokenone00000", affiliate_program_id=1
    )
    target2 = _seed_target(
        session, article_id=art.id, token="BBBB0000tokentwoo0000", affiliate_program_id=2
    )
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


def test_remap_requires_new_target_same_article_as_old_mapping(session: Session) -> None:
    art1 = _seed_article(session, slug="p1")
    art2 = _seed_article(session, slug="p2")
    target1 = _seed_target(session, article_id=art1.id, token="AAAA0000tokenone00000")
    target2 = _seed_target(session, article_id=art2.id, token="BBBB0000tokentwoo0000")
    svc = _svc(session)
    m1 = svc.create_mapping(
        article_id=art1.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )
    with pytest.raises(ArticleLinkSubstitutionMappingError, match="different article"):
        svc.remap_occurrence(m1.id, new_affiliate_link_target_id=target2.id)


# ==================== cross-article guard (create) ========================
def test_cross_article_target_rejected(session: Session) -> None:
    art1 = _seed_article(session, slug="p1")
    art2 = _seed_article(session, slug="p2")
    target1 = _seed_target(session, article_id=art1.id, token="AAAA0000tokenone00000")
    with pytest.raises(ArticleLinkSubstitutionMappingError, match="different article"):
        _svc(session).create_mapping(
            article_id=art2.id,
            occurrence_identity_hash="a" * 64,
            original_href=_HREF,
            affiliate_link_target_id=target1.id,
        )


# ==================== idempotency (create) =================================
def test_idempotency_same_key_same_identity_returns_existing(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    svc = _svc(session)
    a = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id, idempotency_key="k1",
    )
    b = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id, idempotency_key="k1",
    )
    assert a.id == b.id


def test_idempotency_same_key_different_identity_rejected(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    svc = _svc(session)
    svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id, idempotency_key="k1",
    )
    with pytest.raises(ArticleLinkSubstitutionMappingError, match="different mapping identity"):
        svc.create_mapping(
            article_id=art.id, occurrence_identity_hash="b" * 64,
            original_href=_HREF, affiliate_link_target_id=target.id, idempotency_key="k1",
        )


# ==================== no generic mutation path ============================
def test_repository_has_no_generic_update_method(session: Session) -> None:
    repo = ArticleLinkSubstitutionMappingRepository(session)
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")


def test_repository_dangerous_preexisting_replacement_supersede_removed() -> None:
    """D-D1.2: 任意の既存 mapping を replacement として指定できてしまう旧 API
    (``supersede(entity, superseded_by_id=<preexisting id>)``) が実際に削除されて
    いることを確認する。"""

    repo = ArticleLinkSubstitutionMappingRepository
    assert not hasattr(repo, "supersede")


def test_service_dangerous_preexisting_replacement_supersede_mapping_removed() -> None:
    svc = ArticleLinkSubstitutionService
    assert not hasattr(svc, "supersede_mapping")


def test_model_has_no_pii_or_destination_columns() -> None:
    cols = set(ArticleLinkSubstitutionMapping.__table__.columns.keys())
    forbidden = {
        "destination_url", "token", "full_token", "shared_secret", "secret",
        "signature", "ip", "email", "updated_at",
    }
    assert cols.isdisjoint(forbidden)


# ==================== D-F0: post-commit refresh elimination ====================
# 同じクラスの defect (D-D4.2/D-D5D.2 で既に一度ずつ修正済み) が
# ArticleLinkSubstitutionService の create_mapping/remap_occurrence/
# revoke_mapping に残っていた: commit 成功後に session.refresh() を呼んでおり、
# refresh 単体の失敗が「実際には durable に成功した mutation」を呼び出し側に
# 例外として見せてしまう危険があった。以下は (a) refresh が呼ばれれば raise する
# monkeypatch で成功パスが本当に refresh を呼ばないことを証明し、(b) 独立した
# 別セッション (同一 engine 経由) から読み直して durability そのものも証明する。


def _independent_factory(engine: Engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _make() -> Session:
        return factory()

    return _make


def _raise_if_refresh_called(*_a, **_kw):
    raise AssertionError("session.refresh() must not be called on the success path (D-F0)")


# -- create_mapping ---------------------------------------------------------
def test_create_mapping_success_never_calls_refresh(
    session: Session, monkeypatch
) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    monkeypatch.setattr(session, "refresh", _raise_if_refresh_called)

    m = _svc(session).create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    assert m.status == ALSM_ACTIVE
    assert m.id is not None
    assert m.created_at is not None


def test_create_mapping_durable_via_independent_session(
    session: Session, engine: Engine
) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    created = _svc(session).create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    mapping_id, expected_href = created.id, created.original_href

    indep = _independent_factory(engine)()
    try:
        row = indep.get(ArticleLinkSubstitutionMapping, mapping_id)
        assert row is not None
        assert row.status == ALSM_ACTIVE
        assert row.article_id == art.id
        assert row.affiliate_link_target_id == target.id
        assert row.original_href == expected_href
        assert row.occurrence_identity_hash == "a" * 64
        assert row.approved_at is not None
        assert row.created_at is not None
    finally:
        indep.close()


# -- remap_occurrence ---------------------------------------------------------
def test_remap_occurrence_success_never_calls_refresh(
    session: Session, monkeypatch
) -> None:
    art = _seed_article(session)
    target1 = _seed_target(
        session, article_id=art.id, token="AAAA0000tokenone00000", affiliate_program_id=1
    )
    target2 = _seed_target(
        session, article_id=art.id, token="BBBB0000tokentwoo0000", affiliate_program_id=2
    )
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )

    monkeypatch.setattr(session, "refresh", _raise_if_refresh_called)
    new = svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)

    assert new.status == ALSM_ACTIVE
    assert old.status == ALSM_SUPERSEDED
    assert old.superseded_by_id == new.id


def test_remap_occurrence_durable_via_independent_session(
    session: Session, engine: Engine
) -> None:
    art = _seed_article(session)
    target1 = _seed_target(
        session, article_id=art.id, token="AAAA0000tokenone00000", affiliate_program_id=1
    )
    target2 = _seed_target(
        session, article_id=art.id, token="BBBB0000tokentwoo0000", affiliate_program_id=2
    )
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )
    new = svc.remap_occurrence(old.id, new_affiliate_link_target_id=target2.id)
    old_id, new_id = old.id, new.id

    indep = _independent_factory(engine)()
    try:
        old_row = indep.get(ArticleLinkSubstitutionMapping, old_id)
        new_row = indep.get(ArticleLinkSubstitutionMapping, new_id)
        assert old_row.status == ALSM_SUPERSEDED
        assert old_row.superseded_by_id == new_id
        assert new_row.status == ALSM_ACTIVE
        assert new_row.superseded_by_id is None
        # remap は occurrence identity を継承する -- 独立セッションから見ても
        # article_id/occurrence_identity_hash/original_href が old と一致すること。
        assert new_row.article_id == old_row.article_id
        assert new_row.occurrence_identity_hash == old_row.occurrence_identity_hash
        assert new_row.original_href == old_row.original_href
        assert new_row.affiliate_link_target_id == target2.id
    finally:
        indep.close()


# -- revoke_mapping ---------------------------------------------------------
def test_revoke_mapping_success_never_calls_refresh(
    session: Session, monkeypatch
) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    svc = _svc(session)
    m = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )

    monkeypatch.setattr(session, "refresh", _raise_if_refresh_called)
    result = svc.revoke_mapping(m.id)

    assert result.status == ALSM_REVOKED
    assert result.superseded_by_id is None


def test_revoke_mapping_durable_via_independent_session(
    session: Session, engine: Engine
) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    svc = _svc(session)
    m = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    svc.revoke_mapping(m.id)
    mapping_id = m.id

    indep = _independent_factory(engine)()
    try:
        row = indep.get(ArticleLinkSubstitutionMapping, mapping_id)
        assert row.status == ALSM_REVOKED
        assert row.superseded_by_id is None
    finally:
        indep.close()


# -- guard-clause failure path must remain fully unaffected ------------------
def test_remap_guard_clause_failure_never_commits(
    session: Session, monkeypatch
) -> None:
    """D-F0 のスコープは success path の post-commit refresh 削除のみ --
    既存の failure 契約 (guard clause で早期に fail closed するときは commit
    されない) は無変更であること。mid-transaction rollback (mark_superseded_for_
    remap 成功後の失敗) は既存の test_article_link_substitution_remap.py の
    test_rollback_after_old_transition_but_before_new_insert /
    test_rollback_after_new_insert_but_before_link が既に検証済みで、この diff
    はその try/except ブロックを一切変更していない (両ファイル計 45 件が本
    fix 後も無変更で通過することを別途確認済み)。"""

    art = _seed_article(session)
    target1 = _seed_target(
        session, article_id=art.id, token="AAAA0000tokenone00000", affiliate_program_id=1
    )
    svc = _svc(session)
    old = svc.create_mapping(
        article_id=art.id, occurrence_identity_hash="a" * 64,
        original_href=_HREF, affiliate_link_target_id=target1.id,
    )

    commit_calls = {"n": 0}
    real_commit = session.commit

    def _counting_commit():
        commit_calls["n"] += 1
        return real_commit()

    monkeypatch.setattr(session, "commit", _counting_commit)

    # new target が存在しない -> try/except ブロックへ入る前の guard clause で
    # fail closed (mark_superseded_for_remap は一度も呼ばれない)。
    with pytest.raises(EntityNotFoundError):
        svc.remap_occurrence(old.id, new_affiliate_link_target_id=999999)

    assert commit_calls["n"] == 0  # 失敗時は commit されない
    refreshed_old = svc.get(old.id)
    assert refreshed_old.status == ALSM_ACTIVE  # 何も変更されていない
