"""AffiliateLinkTargetService — create / disable / supersede の control-plane 動作。

Google / WordPress へは一切通信しない。host policy はテストが明示注入する。
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

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


def test_create_fails_closed_with_production_default_policy_for_unapproved_pair(
    session: Session,
) -> None:
    aid, pid = _seed(session)
    # policy 未注入 -> production DEFAULT_DESTINATION_HOST_POLICY を使う。
    # D-F1 時点で production policy は "make" -> "www.make.com" の 1 件のみを
    # 承認しているが、この provider ("a8") / host (aff.example.test) はどちらも
    # それと一致しないため、依然として fail closed であること。
    with pytest.raises(AffiliateLinkTargetError, match="not independently approved"):
        AffiliateLinkTargetService(session).create_target(
            article_id=aid, affiliate_program_id=pid
        )
    assert _count(session) == 0


def test_create_passes_host_policy_gate_for_make_under_production_default_policy(
    session: Session,
) -> None:
    """D-F1 §13: production DEFAULT_DESTINATION_HOST_POLICY に "make" ->
    "www.make.com" を追加したことで、この exact (provider, host) の組み合わせ
    だけが独立ホスト承認ゲートを通過できるようになったことを、実際の
    production default policy (注入なし) に対して証明する。

    tracking_url は完全に架空のテスト用 pc 値であり、実際の Make affiliate
    code ではない。production 行は一切作成しない (in-memory DB のみ)。
    """

    fake_tracking_url = "https://www.make.com/en/hq/product?pc=FAKE_TEST_CODE_NOT_REAL"
    aid, pid = _seed(session, provider="make", tracking_url=fake_tracking_url)

    target = AffiliateLinkTargetService(session).create_target(
        article_id=aid, affiliate_program_id=pid
    )
    assert target.destination_host == "www.make.com"
    assert target.destination_url == fake_tracking_url  # 無改変で凍結
    assert _count(session) == 1


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


# ==================== D-F2B: post-commit refresh elimination ====================
# 同じクラスの defect (D-D4.2/D-D5D.2/D-F0 で既に修正済み) が
# AffiliateLinkTargetService の create_target/disable_target/supersede_target に
# 残っていた: commit 成功後に session.refresh() を呼んでおり、refresh 単体の
# 失敗が「実際には durable に成功した mutation」を呼び出し側に例外として見せて
# しまう危険があった。以下は (a) refresh が呼ばれれば raise する monkeypatch で
# 成功パスが本当に refresh を呼ばないことを証明し、(b) 独立した別セッション
# (同一 engine 経由) から読み直して durability そのものも証明する。


def _independent_factory(engine: Engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _make() -> Session:
        return factory()

    return _make


def _raise_if_refresh_called(*_a, **_kw):
    raise AssertionError("session.refresh() must not be called on the success path (D-F2B)")


# -- create_target ------------------------------------------------------------
def test_create_target_success_never_calls_refresh(
    session: Session, monkeypatch
) -> None:
    aid, pid = _seed(session)
    monkeypatch.setattr(session, "refresh", _raise_if_refresh_called)

    target = _svc(session).create_target(article_id=aid, affiliate_program_id=pid)

    assert target.status == "active"
    assert target.id is not None
    assert target.created_at is not None
    assert is_well_formed_token(target.token)


def test_create_target_durable_via_independent_session(
    session: Session, engine: Engine
) -> None:
    aid, pid = _seed(session)
    created = _svc(session).create_target(article_id=aid, affiliate_program_id=pid)
    target_id, expected_token = created.id, created.token

    indep = _independent_factory(engine)()
    try:
        row = indep.get(AffiliateLinkTarget, target_id)
        assert row is not None
        assert row.status == "active"
        assert row.article_id == aid
        assert row.affiliate_program_id == pid
        assert row.token == expected_token
        assert row.link_identity_hash == created.link_identity_hash
        assert row.created_at is not None
    finally:
        indep.close()


# -- disable_target -------------------------------------------------------------
def test_disable_target_success_never_calls_refresh(
    session: Session, monkeypatch
) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    t = svc.create_target(article_id=aid, affiliate_program_id=pid)

    monkeypatch.setattr(session, "refresh", _raise_if_refresh_called)
    result = svc.disable_target(t.id)

    assert result.status == "disabled"
    assert result.disabled_at is not None


def test_disable_target_durable_via_independent_session(
    session: Session, engine: Engine
) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    t = svc.create_target(article_id=aid, affiliate_program_id=pid)
    svc.disable_target(t.id)
    target_id = t.id

    indep = _independent_factory(engine)()
    try:
        row = indep.get(AffiliateLinkTarget, target_id)
        assert row.status == "disabled"
        assert row.disabled_at is not None
    finally:
        indep.close()


# -- supersede_target -----------------------------------------------------------
def test_supersede_target_success_never_calls_refresh(
    session: Session, monkeypatch
) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=REFRESHCHECK"
    session.commit()

    monkeypatch.setattr(session, "refresh", _raise_if_refresh_called)
    old_after, new = svc.supersede_target(old.id)

    assert old_after.status == "superseded"
    assert old_after.superseded_by_id == new.id
    assert new.status == "active"


def test_supersede_target_durable_via_independent_session(
    session: Session, engine: Engine
) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=DURABLECHECK"
    session.commit()

    old_after, new = svc.supersede_target(old.id)
    old_id, new_id = old_after.id, new.id

    indep = _independent_factory(engine)()
    try:
        old_row = indep.get(AffiliateLinkTarget, old_id)
        new_row = indep.get(AffiliateLinkTarget, new_id)
        assert old_row.status == "superseded"
        assert old_row.superseded_by_id == new_id
        assert new_row.status == "active"
        assert new_row.superseded_by_id is None
        assert new_row.article_id == old_row.article_id
        assert new_row.affiliate_program_id == old_row.affiliate_program_id
    finally:
        indep.close()


# -- supersede_target idempotent-replay path (D-F2B.1: genuine reachable state) --
# D-F2B は idempotency-key チェックが transition guard の**後**にあったため、
# 「前回の supersede が実際に成功し old が既に superseded になっている」自然な
# 再試行では replay 分岐に到達できず、forged (実際には発生し得ない) fixture
# でしか exercise できなかった。D-F2B.1 は idempotency 解決を transition guard
# より先に動かす (かつ old.status == superseded / old.superseded_by_id ==
# existing.id という historical linkage を要求する) ことでこれを修正した。
# 以下は forged fixture を使わず、実際に 1 回目の supersede_target() を成功
# させてから同じ引数で 2 回目を呼ぶ、genuinely reachable な状態のみを使う。


def _raise_if_commit_called(*_a, **_kw):
    raise AssertionError("session.commit() must not be called on a genuine replay (D-F2B.1)")


def test_supersede_genuine_retry_returns_same_old_and_new_ids(session: Session) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=RETRYCHECK"
    session.commit()

    old_1, new_1 = svc.supersede_target(old.id, idempotency_key="retry-key")
    old_2, new_2 = svc.supersede_target(old.id, idempotency_key="retry-key")

    assert old_2.id == old_1.id
    assert new_2.id == new_1.id
    assert old_2.status == "superseded"
    assert old_2.superseded_by_id == new_2.id
    assert new_2.status == "active"


def test_supersede_genuine_retry_creates_no_new_row(session: Session) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=RETRYCHECK"
    session.commit()

    svc.supersede_target(old.id, idempotency_key="retry-key")
    before = _count(session)
    svc.supersede_target(old.id, idempotency_key="retry-key")

    assert _count(session) == before == 2  # old + new のみ (third row は作られない)


def test_supersede_genuine_retry_does_not_commit(session: Session, monkeypatch) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=RETRYCHECK"
    session.commit()
    svc.supersede_target(old.id, idempotency_key="retry-key")

    monkeypatch.setattr(session, "commit", _raise_if_commit_called)
    old_2, new_2 = svc.supersede_target(old.id, idempotency_key="retry-key")

    assert old_2.status == "superseded"
    assert new_2.status == "active"


def test_supersede_genuine_retry_does_not_refresh(session: Session, monkeypatch) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=RETRYCHECK"
    session.commit()
    svc.supersede_target(old.id, idempotency_key="retry-key")

    monkeypatch.setattr(session, "refresh", _raise_if_refresh_called)
    old_2, new_2 = svc.supersede_target(old.id, idempotency_key="retry-key")

    assert old_2.status == "superseded"
    assert new_2.status == "active"


def test_supersede_genuine_retry_does_not_mint_new_token(
    session: Session, monkeypatch
) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=RETRYCHECK"
    session.commit()
    _old_1, new_1 = svc.supersede_target(old.id, idempotency_key="retry-key")

    def _raise_if_mint_called():
        raise AssertionError(
            "_mint_token() must not be called on a genuine replay (D-F2B.1)"
        )

    monkeypatch.setattr(svc, "_mint_token", _raise_if_mint_called)
    _old_2, new_2 = svc.supersede_target(old.id, idempotency_key="retry-key")

    assert new_2.token == new_1.token


def test_supersede_same_key_different_destination_is_conflict(session: Session) -> None:
    # 同じ idempotency_key を、program の destination が変わった**後**に
    # 再利用するのは「同じ request の replay」ではない -- conflict のまま。
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=FIRSTDEST"
    session.commit()
    svc.supersede_target(old.id, idempotency_key="retry-key")

    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=SECONDDEST"
    session.commit()

    with pytest.raises(AffiliateLinkTargetError, match="different"):
        svc.supersede_target(old.id, idempotency_key="retry-key")


def test_supersede_already_superseded_different_key_still_rejected(
    session: Session,
) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=FIRSTDEST"
    session.commit()
    svc.supersede_target(old.id, idempotency_key="first-key")

    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=THIRDDEST"
    session.commit()
    with pytest.raises(AffiliateLinkTargetError, match="cannot be superseded"):
        svc.supersede_target(old.id, idempotency_key="never-used-key")


def test_supersede_already_superseded_no_key_still_rejected(session: Session) -> None:
    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=FIRSTDEST"
    session.commit()
    svc.supersede_target(old.id, idempotency_key="first-key")

    with pytest.raises(AffiliateLinkTargetError, match="cannot be superseded"):
        svc.supersede_target(old.id)


def test_supersede_active_old_with_unrelated_idempotency_key_is_conflict(
    session: Session,
) -> None:
    """old が active のまま、無関係な行が同じ idempotency_key を持つ場合は
    conflict のまま (§5.F / §16) -- identity が一致しても
    old.superseded_by_id がその行を指していない限り replay として返さない。"""

    aid, pid = _seed(session)
    svc = _svc(session)
    old = svc.create_target(article_id=aid, affiliate_program_id=pid)

    art2 = _article(session, slug="a2")
    pid2 = _program(session, name="P2")
    _link(session, art2.id, pid2)
    unrelated = svc.create_target(
        article_id=art2.id, affiliate_program_id=pid2, idempotency_key="shared-key"
    )
    assert unrelated.id != old.id

    prog = AffiliateProgramRepository(session).get_by_id(pid)
    prog.tracking_url = f"https://{_ALT_HOST}/track?a8mat=UNRELATED"
    session.commit()

    with pytest.raises(AffiliateLinkTargetError, match="different"):
        svc.supersede_target(old.id, idempotency_key="shared-key")
    assert svc.get(old.id).status == "active"
