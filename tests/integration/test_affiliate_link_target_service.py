"""AffiliateLinkTargetService — create / disable / supersede の control-plane 動作。

Google / WordPress へは一切通信しない。host policy はテストが明示注入する。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.affiliate.link_identity import compute_link_identity_hash
from app.affiliate.token import is_well_formed_token
from app.exceptions import AffiliateLinkTargetError, EntityNotFoundError
from app.models import (
    AffiliateLinkTarget,
    Article,
    ArticleAffiliateProgram,
)
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.services.affiliate_link_target_service import AffiliateLinkTargetService

_HOST = "aff.example.test"
_ALT_HOST = "go.example.test"
_TRACKING = f"https://{_HOST}/track?a8mat=ABC123&utm_source=x#frag"
_POLICY = {"a8": frozenset({_HOST, _ALT_HOST})}


def _article(session: Session, slug: str = "a1") -> Article:
    a = Article(title="t", slug=slug, keyword_id=None, body="# canonical body\n")
    session.add(a)
    session.flush()
    session.commit()
    return a


def _program(
    session: Session,
    *,
    name: str = "P",
    provider: str = "a8",
    tracking_url: str | None = _TRACKING,
    status: AffiliateProgramStatus = AffiliateProgramStatus.ACTIVE,
) -> int:
    p = AffiliateProgramRepository(session).create(
        name=name, provider=provider, tracking_url=tracking_url, status=status
    )
    session.commit()
    return p.id


def _link(session: Session, article_id: int, program_id: int) -> None:
    session.add(
        ArticleAffiliateProgram(
            article_id=article_id, affiliate_program_id=program_id
        )
    )
    session.commit()


def _svc(session: Session, policy=_POLICY) -> AffiliateLinkTargetService:
    return AffiliateLinkTargetService(session, host_policy=policy)


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateLinkTarget))


def _seed(session: Session, **program_kw):
    art = _article(session)
    pid = _program(session, **program_kw)
    _link(session, art.id, pid)
    return art.id, pid


# ==================== create: happy ==================================
def test_create_target_happy(session: Session) -> None:
    aid, pid = _seed(session)
    t = _svc(session).create_target(article_id=aid, affiliate_program_id=pid)

    assert t.status == "active"
    assert is_well_formed_token(t.token)
    # destination_url は Human 入力の exact 文字列 (無改変)
    assert t.destination_url == _TRACKING
    assert t.destination_host == _HOST
    assert t.link_identity_hash == compute_link_identity_hash(
        article_id=aid, affiliate_program_id=pid, destination_url=_TRACKING
    )
    assert t.disabled_at is None and t.superseded_by_id is None
    assert _count(session) == 1


def test_create_preserves_exact_query_and_fragment(session: Session) -> None:
    aid, pid = _seed(session)
    t = _svc(session).create_target(article_id=aid, affiliate_program_id=pid)
    assert "a8mat=ABC123" in t.destination_url
    assert "utm_source=x" in t.destination_url
    assert t.destination_url.endswith("#frag")


# ==================== create: gates =================================
def test_create_requires_article_affiliate_program_relation(session: Session) -> None:
    art = _article(session)
    pid = _program(session)
    # no _link()
    with pytest.raises(AffiliateLinkTargetError, match="ArticleAffiliateProgram"):
        _svc(session).create_target(article_id=art.id, affiliate_program_id=pid)
    assert _count(session) == 0


def test_create_missing_article_raises_not_found(session: Session) -> None:
    pid = _program(session)
    with pytest.raises(EntityNotFoundError):
        _svc(session).create_target(article_id=999999, affiliate_program_id=pid)
    assert _count(session) == 0


def test_create_requires_tracking_url(session: Session) -> None:
    aid, pid = _seed(session, tracking_url=None)
    with pytest.raises(AffiliateLinkTargetError, match="tracking_url"):
        _svc(session).create_target(article_id=aid, affiliate_program_id=pid)
    assert _count(session) == 0


def test_create_rejects_non_active_program(session: Session) -> None:
    aid, pid = _seed(session, status=AffiliateProgramStatus.PAUSED)
    with pytest.raises(AffiliateLinkTargetError, match="status"):
        _svc(session).create_target(article_id=aid, affiliate_program_id=pid)
    assert _count(session) == 0


def test_create_rejects_non_https_tracking_url(session: Session) -> None:
    aid, pid = _seed(session, tracking_url="http://aff.example.test/track")
    with pytest.raises(AffiliateLinkTargetError, match="destination validation"):
        _svc(session).create_target(article_id=aid, affiliate_program_id=pid)
    assert _count(session) == 0


def test_create_rejects_host_not_in_policy(session: Session) -> None:
    aid, pid = _seed(
        session, tracking_url="https://not-approved.example.test/track"
    )
    with pytest.raises(AffiliateLinkTargetError, match="not independently approved"):
        _svc(session).create_target(article_id=aid, affiliate_program_id=pid)
    assert _count(session) == 0


def test_create_fails_closed_when_provider_has_no_policy(session: Session) -> None:
    aid, pid = _seed(session, provider="moshimo")  # policy only has "a8"
    with pytest.raises(AffiliateLinkTargetError, match="not independently approved"):
        _svc(session).create_target(article_id=aid, affiliate_program_id=pid)
    assert _count(session) == 0


def test_create_fails_closed_with_empty_default_policy(session: Session) -> None:
    aid, pid = _seed(session)
    # policy 未注入 -> production DEFAULT (空) -> fail closed
    with pytest.raises(AffiliateLinkTargetError, match="not independently approved"):
        AffiliateLinkTargetService(session).create_target(
            article_id=aid, affiliate_program_id=pid
        )
    assert _count(session) == 0


def test_tracking_url_host_does_not_authorize_itself(session: Session) -> None:
    # tracking_url に出てくる host でも、policy に無ければ activation 不可。
    aid, pid = _seed(
        session,
        provider="brandnew",
        tracking_url="https://aff.brandnew.example.test/track",
    )
    with pytest.raises(AffiliateLinkTargetError, match="not independently approved"):
        _svc(session).create_target(article_id=aid, affiliate_program_id=pid)
    assert _count(session) == 0


# ==================== active uniqueness ============================
def test_only_one_active_target_per_article_program_service(session: Session) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    svc.create_target(article_id=aid, affiliate_program_id=pid)
    with pytest.raises(AffiliateLinkTargetError, match="already exists"):
        svc.create_target(article_id=aid, affiliate_program_id=pid)
    assert _count(session) == 1


def test_partial_unique_index_blocks_second_active_row_at_db(session: Session) -> None:
    aid, pid = _seed(session)
    _svc(session).create_target(article_id=aid, affiliate_program_id=pid)
    session.add(
        AffiliateLinkTarget(
            token="MANUALTOKEN_1234567890",
            article_id=aid,
            affiliate_program_id=pid,
            destination_url=_TRACKING,
            destination_host=_HOST,
            status="active",
            link_identity_hash="0" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ==================== idempotency =================================
def test_same_idempotency_key_same_identity_returns_existing(session: Session) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    a = svc.create_target(
        article_id=aid, affiliate_program_id=pid, idempotency_key="k1"
    )
    b = svc.create_target(
        article_id=aid, affiliate_program_id=pid, idempotency_key="k1"
    )
    assert a.id == b.id
    assert _count(session) == 1


def test_same_idempotency_key_different_identity_rejected(session: Session) -> None:
    art = _article(session)
    p1 = _program(session, name="P1")
    p2 = _program(session, name="P2", tracking_url=f"https://{_ALT_HOST}/t")
    _link(session, art.id, p1)
    _link(session, art.id, p2)
    svc = _svc(session)
    svc.create_target(
        article_id=art.id, affiliate_program_id=p1, idempotency_key="dup"
    )
    with pytest.raises(AffiliateLinkTargetError, match="different"):
        svc.create_target(
            article_id=art.id, affiliate_program_id=p2, idempotency_key="dup"
        )
    assert _count(session) == 1


# ==================== disable ===================================
def test_disable_transition(session: Session) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    t = svc.create_target(article_id=aid, affiliate_program_id=pid)
    out = svc.disable_target(t.id)
    assert out.status == "disabled"
    assert out.disabled_at is not None


def test_disabled_cannot_be_disabled_again(session: Session) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    t = svc.create_target(article_id=aid, affiliate_program_id=pid)
    svc.disable_target(t.id)
    with pytest.raises(AffiliateLinkTargetError, match="cannot be disabled"):
        svc.disable_target(t.id)


def test_recreate_after_disable_is_a_new_row_not_a_reactivation(session: Session) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    t1 = svc.create_target(article_id=aid, affiliate_program_id=pid)
    svc.disable_target(t1.id)
    t2 = svc.create_target(article_id=aid, affiliate_program_id=pid)
    assert t2.id != t1.id and t2.token != t1.token
    assert t2.status == "active"
    assert svc.get(t1.id).status == "disabled"  # 元行は disabled のまま
    assert _count(session) == 2


# ==================== supersede ================================
def test_supersede_creates_new_token_and_freezes_new_destination(
    session: Session,
) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)
    old_token, old_dest = old.token, old.destination_url

    # program がアフィリエイト URL を変更
    prog = AffiliateProgramRepository(session).get_by_id(pid)
    new_tracking = f"https://{_ALT_HOST}/track?a8mat=NEW999"
    prog.tracking_url = new_tracking
    session.commit()

    old_after, new = svc.supersede_target(old.id)

    assert new.token != old_token
    assert new.status == "active"
    assert new.destination_url == new_tracking  # exact 凍結
    assert new.destination_host == _ALT_HOST
    # old は destination をそのまま保持し superseded になる
    assert old_after.destination_url == old_dest
    assert old_after.status == "superseded"
    assert old_after.disabled_at is not None
    assert old_after.superseded_by_id == new.id
    assert _count(session) == 2
    assert [t.id for t in svc.list_active_for_article(aid)] == [new.id]


def test_supersede_identical_destination_rejected(session: Session) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)
    with pytest.raises(AffiliateLinkTargetError, match="identical"):
        svc.supersede_target(old.id)
    assert svc.get(old.id).status == "active"
    assert _count(session) == 1


def test_supersede_rolls_back_and_leaves_old_active_on_failure(
    session: Session, monkeypatch
) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=NEW999"
    session.commit()

    def _boom_add(**_kw):
        raise IntegrityError("stmt", {}, Exception("simulated unique conflict"))

    monkeypatch.setattr(svc._repo, "add", _boom_add)

    with pytest.raises(AffiliateLinkTargetError, match="uniqueness conflict"):
        svc.supersede_target(old.id)

    session.expire_all()
    reloaded = svc.get(old.id)
    assert reloaded.status == "active"  # begin_supersede が rollback された
    assert reloaded.disabled_at is None
    assert reloaded.superseded_by_id is None
    assert _count(session) == 1


def test_supersede_non_active_rejected(session: Session) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    t = svc.create_target(article_id=aid, affiliate_program_id=pid)
    svc.disable_target(t.id)
    with pytest.raises(AffiliateLinkTargetError, match="cannot be superseded"):
        svc.supersede_target(t.id)


# ==================== no destructive / mutating API ================
def test_repository_has_no_generic_mutation_api() -> None:
    from app.repositories.affiliate_link_target_repository import (
        AffiliateLinkTargetRepository,
    )

    for banned in ("update", "delete", "set_destination", "update_destination"):
        assert not hasattr(AffiliateLinkTargetRepository, banned), banned


def test_service_has_no_destination_update_or_delete_api() -> None:
    for banned in (
        "update_target",
        "update_destination",
        "set_destination",
        "delete_target",
        "reactivate_target",
    ):
        assert not hasattr(AffiliateLinkTargetService, banned), banned


def test_affiliate_modules_make_no_network_or_wordpress_google_calls() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app"
    forbidden_import_markers = (
        "import httpx",
        "import requests",
        "from app.wordpress",
        "from app.search_console",
        "googleapiclient",
        "google.oauth2",
        "google.auth",
    )
    for rel in (
        "affiliate/token.py",
        "affiliate/destination_safety.py",
        "affiliate/destination_policy.py",
        "affiliate/link_identity.py",
        "models/affiliate_link_target.py",
        "repositories/affiliate_link_target_repository.py",
        "services/affiliate_link_target_service.py",
    ):
        src = (root / rel).read_text(encoding="utf-8")
        for marker in forbidden_import_markers:
            assert marker not in src, f"{rel}: {marker}"


def test_article_canonical_body_unchanged_by_target_operations(session: Session) -> None:
    aid, pid = _seed(session)
    before = session.get(Article, aid).body
    svc = _svc(session)
    t = svc.create_target(article_id=aid, affiliate_program_id=pid)
    svc.disable_target(t.id)
    session.expire_all()
    assert session.get(Article, aid).body == before == "# canonical body\n"
