"""scripts/onboard_make_affiliate.py -- D-F2C/D の PLAN / EXECUTE 動作。

Google / WordPress へは一切通信しない。実 Make affiliate URL は一切使わない --
ここで使う ``FAKE_URL`` 系の値は明白に偽物 (``FAKE_TEST_CODE_NOT_REAL`` 等)。

安全上の注意: ``scripts.onboard_make_affiliate.cmd_onboard`` / ``main`` は
production の ``app.config.database.SessionLocal`` を直接呼ぶ。このテストで
CLI wiring (``main`` / ``cmd_onboard``) を経由するテストは、必ず
``monkeypatch`` で ``m.SessionLocal`` をテスト用インメモリ engine に差し替えて
から呼ぶ -- production DB ファイルには絶対に触れない。それ以外の大半のテストは
``_preflight`` / ``execute_onboarding`` をテスト用 ``session`` fixture で
直接呼び出す (CLI wiring を経由しない、最も安全な経路)。
"""

from __future__ import annotations

import getpass
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import scripts.onboard_make_affiliate as m
from app.affiliate.link_identity import compute_link_identity_hash
from app.article.fact_freshness import to_storage_utc
from app.models import AffiliateLinkTarget, AffiliateProgram, Article, ArticleAffiliateProgram
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_link_target_repository import AffiliateLinkTargetRepository
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.services.affiliate_link_target_service import AffiliateLinkTargetService

FAKE_URL = "https://www.make.com/en/register?pc=FAKE_TEST_CODE_NOT_REAL"
FAKE_URL_ALT = "https://www.make.com/en/register?pc=ALT_FAKE_TEST_CODE_NOT_REAL"


def _article(session: Session, slug: str = "a1") -> Article:
    a = Article(title="t", slug=slug, keyword_id=None, body="# body\n")
    session.add(a)
    session.flush()
    session.commit()
    return a


def _program(
    session: Session,
    *,
    name: str = "Make",
    provider: str | None = "direct",
    tracking_url: str | None = None,
    status: AffiliateProgramStatus = AffiliateProgramStatus.ACTIVE,
) -> AffiliateProgram:
    p = AffiliateProgramRepository(session).create(
        name=name, provider=provider, tracking_url=tracking_url, status=status
    )
    session.commit()
    return p


def _link(session: Session, article_id: int, program_id: int) -> None:
    session.add(
        ArticleAffiliateProgram(article_id=article_id, affiliate_program_id=program_id)
    )
    session.commit()


def _seed(session: Session, **program_kw) -> tuple[int, int]:
    art = _article(session)
    prog = _program(session, **program_kw)
    _link(session, art.id, prog.id)
    return art.id, prog.id


def _count_targets(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateLinkTarget))


def _raise_if_called(*_a, **_kw):
    raise AssertionError("must not be called on a read-only PLAN path")


# ==================== PLAN ==========================================
def test_plan_direct_provider_no_target(session: Session) -> None:
    aid, pid = _seed(session, provider="direct", tracking_url=None)
    result = m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    assert result.current_provider == "direct"
    assert result.provider_update_would_occur is True
    assert result.tracking_url_would_change is True
    assert result.active_target_exists is False
    assert result.action == m._ACTION_UPDATE_AND_CREATE
    assert result.would_execute is False


def test_plan_make_provider_no_target(session: Session) -> None:
    aid, pid = _seed(session, provider="make", tracking_url=FAKE_URL)
    result = m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    assert result.provider_update_would_occur is False
    assert result.tracking_url_would_change is False
    assert result.active_target_exists is False
    assert result.action == m._ACTION_CREATE_ONLY


def test_plan_exact_existing_matching_target(session: Session) -> None:
    aid, pid = _seed(session, provider="direct", tracking_url=None)
    m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    result = m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    assert result.action == m._ACTION_REPLAY
    assert result.active_target_exists is True
    assert result.existing_target_id is not None
    assert result.provider_update_would_occur is False
    assert result.tracking_url_would_change is False


def test_plan_conflicting_existing_target(session: Session) -> None:
    aid, pid = _seed(session, provider="make", tracking_url=FAKE_URL_ALT)
    # 無関係な (別 identity の) active target を直接作る -- 今回の onboarding
    # 対象 URL (FAKE_URL) とは違う destination。
    AffiliateLinkTargetService(session).create_target(
        article_id=aid, affiliate_program_id=pid
    )

    result = m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    assert result.action == m._ACTION_CONFLICT
    assert result.active_target_exists is True


def test_plan_wrong_program_name(session: Session) -> None:
    aid, pid = _seed(session, name="Not Make")
    with pytest.raises(m.OnboardingPreflightError, match="name"):
        m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)


def test_plan_inactive_program(session: Session) -> None:
    aid, pid = _seed(session, status=AffiliateProgramStatus.PAUSED)
    with pytest.raises(m.OnboardingPreflightError, match="not active"):
        m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)


def test_plan_unexpected_provider_fails_closed(session: Session) -> None:
    aid, pid = _seed(session, provider="a8")
    with pytest.raises(m.OnboardingPreflightError, match="provider"):
        m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)


def test_plan_missing_article_affiliate_program_relationship(session: Session) -> None:
    art = _article(session)
    prog = _program(session)
    # _link() を呼ばない -- 関係を意図的に作らない。
    with pytest.raises(m.OnboardingPreflightError, match="relationship"):
        m._preflight(session, article_id=art.id, program_id=prog.id, hidden_url=FAKE_URL)


def test_plan_wrong_host(session: Session) -> None:
    aid, pid = _seed(session)
    bad_url = "https://evil.example.test/register?pc=FAKE_TEST_CODE_NOT_REAL"
    with pytest.raises(m.AffiliateDestinationError, match="host"):
        m._preflight(session, article_id=aid, program_id=pid, hidden_url=bad_url)


def test_plan_http_scheme_rejected(session: Session) -> None:
    aid, pid = _seed(session)
    bad_url = "http://www.make.com/register?pc=FAKE_TEST_CODE_NOT_REAL"
    with pytest.raises(m.AffiliateDestinationError, match="https"):
        m._preflight(session, article_id=aid, program_id=pid, hidden_url=bad_url)


def test_plan_missing_pc_parameter_rejected(session: Session) -> None:
    aid, pid = _seed(session)
    bad_url = "https://www.make.com/register?other=1"
    with pytest.raises(m.AffiliateDestinationError, match="pc"):
        m._preflight(session, article_id=aid, program_id=pid, hidden_url=bad_url)


def test_plan_fragment_present_rejected(session: Session) -> None:
    aid, pid = _seed(session)
    bad_url = "https://www.make.com/register?pc=FAKE_TEST_CODE_NOT_REAL#frag"
    with pytest.raises(m.AffiliateDestinationError, match="fragment"):
        m._preflight(session, article_id=aid, program_id=pid, hidden_url=bad_url)


def test_plan_zero_commit(session: Session, monkeypatch) -> None:
    aid, pid = _seed(session)
    monkeypatch.setattr(session, "commit", _raise_if_called)
    result = m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)
    assert result.action == m._ACTION_UPDATE_AND_CREATE


def test_plan_zero_flush(session: Session, monkeypatch) -> None:
    aid, pid = _seed(session)
    monkeypatch.setattr(session, "flush", _raise_if_called)
    result = m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)
    assert result.action == m._ACTION_UPDATE_AND_CREATE


def test_plan_safe_output_redaction(session: Session, capsys) -> None:
    aid, pid = _seed(session)
    result = m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)
    m._print_plan(result)
    out = capsys.readouterr().out
    assert FAKE_URL not in out
    assert "FAKE_TEST_CODE_NOT_REAL" not in out
    assert "pc=" not in out


# ==================== EXECUTE =========================================
def test_execute_fresh_onboarding_direct_to_make(session: Session) -> None:
    aid, pid = _seed(session, provider="direct", tracking_url=None)
    result = m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    assert result.program_updated is True
    assert result.target_status == "active"
    assert result.target_superseded_by_id is None
    assert _count_targets(session) == 1

    program = session.get(AffiliateProgram, pid)
    assert program.provider == "make"
    assert program.tracking_url == FAKE_URL


def test_execute_retry_same_invocation_no_duplicate(session: Session) -> None:
    aid, pid = _seed(session, provider="direct", tracking_url=None)
    first = m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)
    second = m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    assert second.target_id == first.target_id
    assert second.program_updated is False  # 既に provider=make + tracking_url 一致
    assert _count_targets(session) == 1


def test_execute_partial_success_resume(session: Session, monkeypatch) -> None:
    aid, pid = _seed(session, provider="direct", tracking_url=None)

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated target creation failure")

    monkeypatch.setattr(AffiliateLinkTargetService, "create_target", _boom)
    with pytest.raises(RuntimeError):
        m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    # program 更新は既に commit 済みで durable -- rollback されていない。
    program = session.get(AffiliateProgram, pid)
    assert program.provider == "make"
    assert program.tracking_url == FAKE_URL
    assert _count_targets(session) == 0

    monkeypatch.undo()
    result = m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    assert result.program_updated is False  # 既に make + 一致 URL -- 再更新しない
    assert result.target_status == "active"
    assert _count_targets(session) == 1


def test_execute_different_url_with_existing_target_fails_closed(session: Session) -> None:
    aid, pid = _seed(session, provider="direct", tracking_url=None)
    m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    with pytest.raises(m.OnboardingPreflightError, match="does not match"):
        m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL_ALT)

    assert _count_targets(session) == 1
    program = session.get(AffiliateProgram, pid)
    assert program.tracking_url == FAKE_URL  # 既存 target のための URL は変更されない


def test_execute_unexpected_provider_fails_closed(session: Session) -> None:
    aid, pid = _seed(session, provider="a8")
    with pytest.raises(m.OnboardingPreflightError, match="provider"):
        m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    assert _count_targets(session) == 0
    program = session.get(AffiliateProgram, pid)
    assert program.provider == "a8"  # 一切変更されない


def test_execute_conflicting_target_fails_closed_no_mutation(session: Session) -> None:
    aid, pid = _seed(session, provider="make", tracking_url=FAKE_URL_ALT)
    unrelated = AffiliateLinkTargetService(session).create_target(
        article_id=aid, affiliate_program_id=pid
    )

    with pytest.raises(m.OnboardingPreflightError, match="does not match"):
        m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    assert _count_targets(session) == 1
    reloaded = AffiliateLinkTargetService(session).get(unrelated.id)
    assert reloaded.status == "active"  # 既存 target は一切触らない (supersede しない)


def test_execute_never_touches_program_when_tracking_url_diverged_from_forged_target(
    session: Session,
) -> None:
    """自己レビューで発見した修正の回帰テスト。

    最初の実装は「この deterministic idempotency_key に一致する target が
    存在する」だけで REPLAY と判定していたが、``create_target()`` は
    destination を常に **現在の** ``program.tracking_url`` から再計算する
    (この CLI の hidden_url を直接は見ない) ため、program.tracking_url が
    target 作成時から out-of-band で乖離していると、真の replay は成立せず
    ``create_target()`` 自身が生の "different target identity" 例外を
    投げてしまっていた。修正後は ``tracking_url_would_change`` も replay
    条件に含め、この場合は PLAN の時点で CONFLICT と正しく予測し、
    EXECUTE は ``create_target()`` を一切呼ばずに CLI 境界で安全に
    fail closed する (§15: target が既存の限り tracking_url を書き換えない
    ことに加え、program も target も一切 mutate しない)。"""

    aid, pid = _seed(session, provider="make", tracking_url=FAKE_URL_ALT)
    link_hash = compute_link_identity_hash(
        article_id=aid, affiliate_program_id=pid, destination_url=FAKE_URL
    )
    idempotency_key = m._compute_idempotency_key(
        article_id=aid, affiliate_program_id=pid, link_identity_hash=link_hash
    )
    forged = AffiliateLinkTargetRepository(session).add(
        token="FORGEDREPLAYTOKEN0002BB",
        article_id=aid,
        affiliate_program_id=pid,
        destination_url=FAKE_URL,
        destination_host="www.make.com",
        status="active",
        link_identity_hash=link_hash,
        idempotency_key=idempotency_key,
        created_at=to_storage_utc(datetime.now(UTC)),
    )
    session.commit()

    plan = m._preflight(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)
    assert plan.action == m._ACTION_CONFLICT
    assert plan.tracking_url_would_change is True  # 乖離を正しく検出している

    with pytest.raises(m.OnboardingPreflightError, match="does not match"):
        m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)

    program = session.get(AffiliateProgram, pid)
    assert program.tracking_url == FAKE_URL_ALT  # 一切書き換えられない
    reloaded = AffiliateLinkTargetService(session).get(forged.id)
    assert reloaded.status == "active"  # forged target も一切触らない
    assert _count_targets(session) == 1


def test_execute_safe_output_no_url_no_pc_no_token(session: Session, capsys) -> None:
    aid, pid = _seed(session, provider="direct", tracking_url=None)
    result = m.execute_onboarding(session, article_id=aid, program_id=pid, hidden_url=FAKE_URL)
    m._print_execute(result)
    out = capsys.readouterr().out

    assert FAKE_URL not in out
    assert "FAKE_TEST_CODE_NOT_REAL" not in out
    assert "pc=" not in out
    target = AffiliateLinkTargetService(session).get(result.target_id)
    assert target.token not in out


# ==================== idempotency key design ===========================
def test_idempotency_key_deterministic_for_same_url(session: Session) -> None:
    aid, pid = _seed(session)
    h = "a" * 64
    k1 = m._compute_idempotency_key(article_id=aid, affiliate_program_id=pid, link_identity_hash=h)
    k2 = m._compute_idempotency_key(article_id=aid, affiliate_program_id=pid, link_identity_hash=h)
    assert k1 == k2
    assert FAKE_URL not in k1
    assert "FAKE_TEST_CODE_NOT_REAL" not in k1


def test_idempotency_key_differs_for_different_identity(session: Session) -> None:
    k1 = m._compute_idempotency_key(
        article_id=1, affiliate_program_id=1, link_identity_hash="a" * 64
    )
    k2 = m._compute_idempotency_key(
        article_id=1, affiliate_program_id=1, link_identity_hash="b" * 64
    )
    assert k1 != k2


# ==================== CLI wiring (no --url argument, hidden input) ======
def test_cli_has_no_url_style_argument() -> None:
    for flag in ("--url", "--tracking-url", "--affiliate-url"):
        with pytest.raises(SystemExit):
            m._parse_args(["--article-id", "1", "--program-id", "1", flag, "x"])


def test_cli_default_mode_is_plan_not_execute() -> None:
    args = m._parse_args(["--article-id", "1", "--program-id", "1"])
    assert args.execute is False


def test_cli_reads_hidden_url_via_getpass(monkeypatch) -> None:
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": FAKE_URL)
    assert m._read_hidden_url() == FAKE_URL


def test_cli_end_to_end_plan_uses_isolated_session_and_redacts_output(
    engine: Engine, monkeypatch, capsys
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup_session:
        aid, pid = _seed(setup_session, provider="direct", tracking_url=None)

    # CLI wiring は production SessionLocal を直接呼ぶ -- テスト用 in-memory
    # engine に必ず差し替えてから main() を呼ぶ (production DB に触れない)。
    monkeypatch.setattr(m, "SessionLocal", factory)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": FAKE_URL)

    exit_code = m.main(["--article-id", str(aid), "--program-id", str(pid)])

    assert exit_code == m.EXIT_OK
    out = capsys.readouterr().out
    assert "no DB write performed" in out
    assert FAKE_URL not in out
    assert "FAKE_TEST_CODE_NOT_REAL" not in out

    with factory() as verify_session:
        assert _count_targets(verify_session) == 0  # PLAN は書き込まない


def test_exception_redaction_hides_fake_url_on_failure(
    engine: Engine, monkeypatch, capsys
) -> None:
    """§19: 実際に近い偽の pc 値を含む例外が発生しても、CLI 出力には現れない。"""

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup_session:
        aid, pid = _seed(setup_session, provider="direct", tracking_url=None)

    def _leaky_failure(*_a, **_kw):
        # SQLAlchemy IntegrityError は既定で bound parameters (実 URL を含みうる)
        # を str(exc) に含める -- ここではそれを模した「危険な」例外を発生させる。
        raise RuntimeError(f"simulated leak containing {FAKE_URL}")

    monkeypatch.setattr(AffiliateLinkTargetService, "create_target", _leaky_failure)
    monkeypatch.setattr(m, "SessionLocal", factory)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": FAKE_URL)

    exit_code = m.main(["--article-id", str(aid), "--program-id", str(pid), "--execute"])

    assert exit_code == m.EXIT_FAILED
    out = capsys.readouterr().out
    assert FAKE_URL not in out
    assert "FAKE_TEST_CODE_NOT_REAL" not in out
    assert "message withheld" in out
