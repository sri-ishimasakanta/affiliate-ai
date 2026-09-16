"""scripts/manage_article_link_mapping.py -- D-F4 の PLAN / EXECUTE 動作。

Google / WordPress へは一切通信しない。fixtures は既存の
``test_article_link_occurrence_preview_service.py`` と同じパターンを使う。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import scripts.manage_article_link_mapping as m
from app.affiliate.projection import projection_from_target
from app.affiliate.projection_push_acknowledgement import (
    serialize_manifest,
    token_fingerprint,
)
from app.config.settings import Settings
from app.models import (
    AffiliateLinkTarget,
    AffiliateTargetProjectionPushRun,
    Article,
    ArticleLinkSubstitutionMapping,
)
from app.repositories.affiliate_target_projection_push_run_repository import (
    AffiliateTargetProjectionPushRunRepository,
)
from app.services.article_link_substitution_service import ArticleLinkSubstitutionService

_ORIGIN = "https://bizfluxlab.com"
_HREF_A = "https://official.example.test/pricing"
_HREF_B = "https://official.example.test/"


def _article(session: Session, *, slug: str = "a1", body: str | None = None) -> Article:
    body = body or f"[pricing]({_HREF_A}) and [site]({_HREF_B})\n"
    art = Article(title="t", slug=slug, keyword_id=None, body=body)
    session.add(art)
    session.commit()
    return art


def _target(
    session: Session,
    *,
    article_id: int,
    token: str = "AAAA0000tokenone00000",
    status: str = "active",
    affiliate_program_id: int = 1,
) -> AffiliateLinkTarget:
    t = AffiliateLinkTarget(
        token=token,
        article_id=article_id,
        affiliate_program_id=affiliate_program_id,
        destination_url="https://aff.example.test/x",
        destination_host="aff.example.test",
        status=status,
        link_identity_hash="1" * 64,
    )
    session.add(t)
    session.commit()
    return t


def _count_mappings(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(ArticleLinkSubstitutionMapping)
    )


def _fake_settings(base_url: str | None = _ORIGIN) -> Settings:
    return Settings(wordpress_base_url=base_url)


def _succeed_run(
    session: Session, *, manifest: list[dict], runtime_origin: str = _ORIGIN
) -> AffiliateTargetProjectionPushRun:
    repo = AffiliateTargetProjectionPushRunRepository(session)
    run = repo.add_running(
        snapshot_scope="full",
        runtime_origin=runtime_origin,
        requested_snapshot_hash="a" * 64,
        requested_target_count=len(manifest),
        request_manifest_json=serialize_manifest(manifest),
        started_at=datetime.now(UTC),
    )
    repo.mark_succeeded(
        run,
        http_status=200,
        response_projection_snapshot_hash="a" * 64,
        received_count=len(manifest),
        inserted_count=len(manifest),
        updated_count=0,
        unchanged_count=0,
        finished_at=datetime.now(UTC),
    )
    session.commit()
    return run


def _matching_manifest_entry(target: AffiliateLinkTarget) -> dict:
    proj = projection_from_target(target)
    return {
        "affiliate_link_target_id": target.id,
        "token_fingerprint": token_fingerprint(target.token),
        "link_identity_hash": target.link_identity_hash,
        "status": "active",
        "projection_version": 1,
        "entry_hash": proj.projection_entry_hash,
    }


def _raise_if_called(*_a, **_kw):
    raise AssertionError("must not be called on this path")


# ==================== PLAN ==========================================
def test_plan_valid_occurrence_resolves_correctly(session: Session) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id)
    result = m._preflight(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target.id
    )

    assert result.original_href == _HREF_B
    assert result.occurrence_ordinal == 1
    assert len(result.occurrence_identity_hash) == 64
    assert result.target_id == target.id
    assert result.target_status == "active"
    assert result.mapping_exists is False
    assert result.action == m._ACTION_CREATE
    assert result.would_execute is False


def test_plan_zero_commit(session: Session, monkeypatch) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id)
    monkeypatch.setattr(session, "commit", _raise_if_called)
    result = m._preflight(
        session, article_id=art.id, occurrence_ordinal=0, target_id=target.id
    )
    assert result.action == m._ACTION_CREATE


def test_plan_zero_flush(session: Session, monkeypatch) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id)
    monkeypatch.setattr(session, "flush", _raise_if_called)
    result = m._preflight(
        session, article_id=art.id, occurrence_ordinal=0, target_id=target.id
    )
    assert result.action == m._ACTION_CREATE


def test_plan_wrong_ordinal_fails_closed(session: Session) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id)
    with pytest.raises(m.MappingPreflightError, match="ordinal"):
        m._preflight(session, article_id=art.id, occurrence_ordinal=99, target_id=target.id)


def test_plan_stale_occurrence_after_body_change_uses_fresh_identity(
    session: Session,
) -> None:
    art = _article(session, body=f"[pricing]({_HREF_A})\n")
    target = _target(session, article_id=art.id)
    first = m._preflight(
        session, article_id=art.id, occurrence_ordinal=0, target_id=target.id
    )
    m.execute_mapping(
        session, article_id=art.id, occurrence_ordinal=0, target_id=target.id
    )

    # article body changes -- canonical_body_hash (and therefore ordinal 0's
    # occurrence_identity_hash) changes even though it's still "ordinal 0".
    art.body = f"[pricing v2]({_HREF_A}?v=2)\n"
    session.commit()

    second = m._preflight(
        session, article_id=art.id, occurrence_ordinal=0, target_id=target.id
    )
    assert second.occurrence_identity_hash != first.occurrence_identity_hash
    assert second.mapping_exists is False  # 古い mapping は新しい hash に紐付かない
    assert second.action == m._ACTION_CREATE
    assert _count_mappings(session) == 1  # 古い (今は stale な) mapping はそのまま


def test_plan_target_wrong_article_fails_closed(session: Session) -> None:
    art1 = _article(session, slug="p1")
    art2 = _article(session, slug="p2")
    other_target = _target(session, article_id=art2.id, token="BBBB0000tokentwoo0000")
    with pytest.raises(m.MappingPreflightError, match="belongs to article"):
        m._preflight(
            session, article_id=art1.id, occurrence_ordinal=0, target_id=other_target.id
        )


def test_plan_target_inactive_fails_closed(session: Session) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id, status="disabled")
    with pytest.raises(m.MappingPreflightError, match="not active"):
        m._preflight(session, article_id=art.id, occurrence_ordinal=0, target_id=target.id)


def test_plan_target_projection_available_false_when_no_push_run(
    session: Session, monkeypatch
) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id)
    monkeypatch.setattr(m, "get_settings", lambda: _fake_settings())
    result = m._preflight(
        session, article_id=art.id, occurrence_ordinal=0, target_id=target.id
    )
    assert result.target_projection_available is False


def test_plan_target_projection_available_true_when_matching_succeeded_run(
    session: Session, monkeypatch
) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id)
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    monkeypatch.setattr(m, "get_settings", lambda: _fake_settings())

    result = m._preflight(
        session, article_id=art.id, occurrence_ordinal=0, target_id=target.id
    )
    assert result.target_projection_available is True


# ==================== EXECUTE =========================================
def test_execute_fresh_mapping_creates_active_mapping(session: Session) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id)
    result = m.execute_mapping(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target.id
    )

    assert result.mapping_status == "active"
    assert result.target_id == target.id
    assert result.occurrence_ordinal == 1
    assert _count_mappings(session) == 1

    mapping = ArticleLinkSubstitutionService(session).get(result.mapping_id)
    assert mapping.original_href == _HREF_B
    assert mapping.approved_at is not None


def test_execute_retry_same_invocation_no_duplicate(session: Session) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id)
    first = m.execute_mapping(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target.id
    )
    second = m.execute_mapping(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target.id
    )

    assert second.mapping_id == first.mapping_id
    assert _count_mappings(session) == 1


def test_execute_different_target_conflict_fails_closed(session: Session) -> None:
    art = _article(session)
    target_a = _target(session, article_id=art.id, token="AAAA0000tokenone00000")
    target_b = _target(
        session,
        article_id=art.id,
        token="BBBB0000tokentwoo0000",
        affiliate_program_id=2,
    )
    m.execute_mapping(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target_a.id
    )

    plan = m._preflight(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target_b.id
    )
    assert plan.action == m._ACTION_CONFLICT

    with pytest.raises(m.MappingPreflightError, match="does not match"):
        m.execute_mapping(
            session, article_id=art.id, occurrence_ordinal=1, target_id=target_b.id
        )
    assert _count_mappings(session) == 1


def test_execute_target_inactive_fails_closed_no_mutation(session: Session) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id, status="disabled")
    with pytest.raises(m.MappingPreflightError, match="not active"):
        m.execute_mapping(
            session, article_id=art.id, occurrence_ordinal=0, target_id=target.id
        )
    assert _count_mappings(session) == 0


def test_execute_after_revoke_replays_revoked_mapping_without_reactivating(
    session: Session,
) -> None:
    """§9.D: superseded/revoked な historical mapping を蘇らせたり status
    遷移規則を迂回したりしない -- create_mapping() 自身の idempotency 契約
    (status を見ずに identity だけで replay する) を CLI が正直にそのまま
    反映し、mapping_status を隠さず表示する。"""

    art = _article(session)
    target = _target(session, article_id=art.id)
    created = m.execute_mapping(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target.id
    )
    ArticleLinkSubstitutionService(session).revoke_mapping(created.mapping_id)

    plan = m._preflight(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target.id
    )
    assert plan.action == m._ACTION_REPLAY
    assert plan.existing_mapping_status == "revoked"

    replayed = m.execute_mapping(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target.id
    )
    assert replayed.mapping_id == created.mapping_id
    assert replayed.mapping_status == "revoked"  # 静かに active化しない
    assert _count_mappings(session) == 1


def test_execute_never_calls_remap_or_revoke(session: Session, monkeypatch) -> None:
    monkeypatch.setattr(
        ArticleLinkSubstitutionService, "remap_occurrence", _raise_if_called
    )
    monkeypatch.setattr(
        ArticleLinkSubstitutionService, "revoke_mapping", _raise_if_called
    )
    art = _article(session)
    target = _target(session, article_id=art.id)
    m.execute_mapping(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target.id
    )
    m.execute_mapping(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target.id
    )


# ==================== idempotency key design ===========================
def test_idempotency_key_deterministic(session: Session) -> None:
    k1 = m._compute_idempotency_key(
        article_id=1, occurrence_identity_hash="a" * 64, affiliate_link_target_id=1
    )
    k2 = m._compute_idempotency_key(
        article_id=1, occurrence_identity_hash="a" * 64, affiliate_link_target_id=1
    )
    assert k1 == k2


def test_idempotency_key_differs_for_different_target(session: Session) -> None:
    k1 = m._compute_idempotency_key(
        article_id=1, occurrence_identity_hash="a" * 64, affiliate_link_target_id=1
    )
    k2 = m._compute_idempotency_key(
        article_id=1, occurrence_identity_hash="a" * 64, affiliate_link_target_id=2
    )
    assert k1 != k2


# ==================== safe output =======================================
def test_plan_safe_output_no_token_no_destination_url(session: Session, capsys) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id)
    result = m._preflight(
        session, article_id=art.id, occurrence_ordinal=0, target_id=target.id
    )
    m._print_plan(result)
    out = capsys.readouterr().out
    assert target.token not in out
    assert target.destination_url not in out


def test_execute_safe_output_no_token_no_destination_url(session: Session, capsys) -> None:
    art = _article(session)
    target = _target(session, article_id=art.id)
    result = m.execute_mapping(
        session, article_id=art.id, occurrence_ordinal=1, target_id=target.id
    )
    m._print_execute(result)
    out = capsys.readouterr().out
    assert target.token not in out
    assert target.destination_url not in out


# ==================== CLI wiring ========================================
def test_cli_default_mode_is_plan() -> None:
    args = m._parse_args(
        ["--article-id", "1", "--occurrence-ordinal", "0", "--target-id", "1"]
    )
    assert args.execute is False


def test_cli_execute_flag_explicit() -> None:
    args = m._parse_args(
        [
            "--article-id", "1", "--occurrence-ordinal", "0", "--target-id", "1",
            "--execute",
        ]
    )
    assert args.execute is True


def test_cli_has_no_href_or_hash_style_argument() -> None:
    base = ["--article-id", "1", "--occurrence-ordinal", "0", "--target-id", "1"]
    for flag in ("--original-href", "--occurrence-identity-hash", "--href", "--hash"):
        with pytest.raises(SystemExit):
            m._parse_args([*base, flag, "x"])


def test_exception_redaction_for_unexpected_errors(
    engine: Engine, monkeypatch, capsys
) -> None:
    """§19 相当の防御: mapping 作成では token/destination_url は insert
    対象にすら入らないため直接の漏洩経路はないが、想定外の例外
    (生の SQLAlchemyError 等) が CLI 境界でどう扱われるかを明示的に固定する
    -- allowlist にない例外は type 名だけを出し、str(exc) は redact する。"""

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup_session:
        art = _article(setup_session)
        target = _target(setup_session, article_id=art.id)
        aid, tid = art.id, target.id

    def _leaky_failure(*_a, **_kw):
        raise RuntimeError(f"simulated leak containing token {target.token}")

    monkeypatch.setattr(ArticleLinkSubstitutionService, "create_mapping", _leaky_failure)
    monkeypatch.setattr(m, "SessionLocal", factory)

    exit_code = m.main(
        [
            "--article-id", str(aid), "--occurrence-ordinal", "1",
            "--target-id", str(tid), "--execute",
        ]
    )
    assert exit_code == m.EXIT_FAILED
    out = capsys.readouterr().out
    assert target.token not in out
    assert "message withheld" in out


def test_cli_end_to_end_execute_uses_isolated_session(
    engine: Engine, monkeypatch, capsys
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup_session:
        art = _article(setup_session)
        target = _target(setup_session, article_id=art.id)
        aid, tid = art.id, target.id

    monkeypatch.setattr(m, "SessionLocal", factory)

    exit_code = m.main(
        [
            "--article-id", str(aid), "--occurrence-ordinal", "1",
            "--target-id", str(tid), "--execute",
        ]
    )
    assert exit_code == m.EXIT_OK
    out = capsys.readouterr().out
    assert "mapping_status" in out

    with factory() as verify_session:
        assert _count_mappings(verify_session) == 1
