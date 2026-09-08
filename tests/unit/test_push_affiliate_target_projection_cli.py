"""scripts/push_affiliate_target_projection.py — plan (default) + --execute (mocked)。"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AffiliateLinkTarget, Article, ArticleAffiliateProgram
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.services.affiliate_link_target_service import AffiliateLinkTargetService
from scripts.push_affiliate_target_projection import EXIT_NOT_CONFIGURED, EXIT_OK, run

_SECRET = "synthetic-cli-secret"
_BASE = "https://runtime.example.test"
_EMPTY_HASH = "019ac81aeaceee4153c5e477492bb27965210e24764d1ba1cbe50ac67e617b4d"
_HOST = "aff.example.test"
_TRACKING = f"https://{_HOST}/track?a8mat=SECRETTRACKID999#f"
_POLICY = {"a8": frozenset({_HOST})}


def _settings(*, base=_BASE, secret=_SECRET, verify=True):
    return SimpleNamespace(
        wordpress_base_url=base,
        wordpress_verify_tls=verify,
        affiliate_runtime_shared_secret=secret,
        affiliate_runtime_push_configured=bool(base and secret),
    )


def _sf(session: Session):
    return lambda: contextlib.nullcontext(session)


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateLinkTarget))


def _seed_one_target(session: Session):
    art = Article(title="t", slug="p1", keyword_id=None, body="# b\n")
    session.add(art)
    session.flush()
    session.commit()
    pid = AffiliateProgramRepository(session).create(
        name="P", provider="a8", tracking_url=_TRACKING,
        status=AffiliateProgramStatus.ACTIVE,
    ).id
    session.add(ArticleAffiliateProgram(article_id=art.id, affiliate_program_id=pid))
    session.commit()
    AffiliateLinkTargetService(session, host_policy=_POLICY).create_target(
        article_id=art.id, affiliate_program_id=pid
    )


# ==================== plan (default) =============================
def test_plan_zero_targets_makes_no_http_and_shows_empty_hash(
    session: Session, capsys
) -> None:
    boom = httpx.MockTransport(
        lambda req: (_ for _ in ()).throw(AssertionError("no HTTP in plan"))
    )
    code = run(
        execute=False,
        settings=_settings(),
        session_factory=_sf(session),
        transport=boom,
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "execute                  = false" in out
    assert f"projection_snapshot_hash = {_EMPTY_HASH}" in out
    assert "eligible_target_count    = 0" in out
    assert "runtime.example.test/wp-json/affiliate-ai/v1/target-projections" in out
    assert "plan only: 0 WordPress requests" in out


def test_plan_output_excludes_secret_signature_and_destination(
    session: Session, capsys
) -> None:
    _seed_one_target(session)
    run(execute=False, settings=_settings(), session_factory=_sf(session))
    out = capsys.readouterr().out
    for banned in (
        _SECRET,
        "SECRETTRACKID999",
        "a8mat",
        _TRACKING,
        "X-BFL-Signature",
    ):
        assert banned not in out
    assert "eligible_target_count    = 1" in out
    assert "token=" in out  # masked prefix only
    # full token must not appear
    tok = session.scalars(select(AffiliateLinkTarget)).one().token
    assert tok not in out


# ==================== --execute (mocked) ========================
def test_execute_makes_exactly_one_request_and_reports_counts(
    session: Session, capsys
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "projection_snapshot_hash": _EMPTY_HASH,
                "received_count": 0,
                "inserted_count": 0,
                "updated_count": 0,
                "unchanged_count": 0,
            },
        )

    code = run(
        execute=True,
        settings=_settings(),
        session_factory=_sf(session),
        transport=httpx.MockTransport(handler),
        now=1_760_000_000,
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert len(calls) == 1
    assert "http_status              = 200" in out
    assert "empty-snapshot push accepted" in out
    assert _SECRET not in out
    assert "X-BFL-Signature" not in out


def test_execute_not_configured_without_secret(session: Session, capsys) -> None:
    boom = httpx.MockTransport(
        lambda req: (_ for _ in ()).throw(AssertionError("no HTTP"))
    )
    code = run(
        execute=True,
        settings=_settings(secret=None),
        session_factory=_sf(session),
        transport=boom,
    )
    out = capsys.readouterr().out
    assert code == EXIT_NOT_CONFIGURED
    assert "NOT CONFIGURED" in out


def test_execute_server_conflict_maps_to_safe_exit(session: Session, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "conflict_stale_version"}})

    code = run(
        execute=True,
        settings=_settings(),
        session_factory=_sf(session),
        transport=httpx.MockTransport(handler),
        now=1_760_000_000,
    )
    out = capsys.readouterr().out
    assert code == 3  # EXIT_PUSH_ERROR
    assert "PUSH FAILED" in out
    assert "server_code = conflict_stale_version" in out
    assert _SECRET not in out
