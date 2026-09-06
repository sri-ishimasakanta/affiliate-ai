"""scripts/preview_affiliate_target_projection.py — dry-run projection builder。

DB write なし。WordPress request なし。実 Google request なし。
"""

from __future__ import annotations

import contextlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AffiliateLinkTarget, Article, ArticleAffiliateProgram
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.services.affiliate_link_target_service import AffiliateLinkTargetService
from scripts.preview_affiliate_target_projection import EXIT_OK, run

_HOST = "aff.example.test"
_TRACKING = f"https://{_HOST}/track?a8mat=SECRETTRACKID123&utm_source=n#frag"
_POLICY = {"a8": frozenset({_HOST})}


def _sf(session: Session):
    return lambda: contextlib.nullcontext(session)


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateLinkTarget))


def _seed_target(session: Session) -> AffiliateLinkTarget:
    art = Article(title="t", slug="p1", keyword_id=None, body="# body\n")
    session.add(art)
    session.flush()
    session.commit()
    pid = AffiliateProgramRepository(session).create(
        name="P", provider="a8", tracking_url=_TRACKING,
        status=AffiliateProgramStatus.ACTIVE,
    ).id
    session.add(ArticleAffiliateProgram(article_id=art.id, affiliate_program_id=pid))
    session.commit()
    return AffiliateLinkTargetService(session, host_policy=_POLICY).create_target(
        article_id=art.id, affiliate_program_id=pid
    )


def test_dry_run_zero_targets_is_valid(session: Session, capsys) -> None:
    code = run(session_factory=_sf(session))
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "control_plane_target_count = 0" in out
    assert "projected_target_count    = 0" in out
    assert "projection_snapshot_hash  = " in out
    assert _count(session) == 0  # no DB write


def test_dry_run_zero_targets_hash_is_deterministic(session: Session, capsys) -> None:
    run(session_factory=_sf(session))
    first = capsys.readouterr().out
    run(session_factory=_sf(session))
    second = capsys.readouterr().out
    h1 = [ln for ln in first.splitlines() if "projection_snapshot_hash" in ln][0]
    h2 = [ln for ln in second.splitlines() if "projection_snapshot_hash" in ln][0]
    assert h1 == h2


def test_dry_run_with_targets_masks_tokens_and_hides_secrets(
    session: Session, capsys
) -> None:
    t = _seed_target(session)
    before = _count(session)
    code = run(session_factory=_sf(session))
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "control_plane_target_count = 1" in out
    assert "active_count            = 1" in out
    # full token は出さない (先頭 4 文字のみ)
    assert t.token not in out
    assert f"token={t.token[:4]}…" in out
    # secret / destination は一切出さない
    assert "SECRETTRACKID123" not in out
    assert "a8mat" not in out
    assert _TRACKING not in out
    assert "http://" not in out and "https://" not in out
    assert _count(session) == before  # still no DB write


def test_dry_run_script_has_no_wordpress_or_network_imports() -> None:
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "preview_affiliate_target_projection.py"
    ).read_text(encoding="utf-8")
    for marker in ("import httpx", "import requests", "from app.wordpress", "RedirectResponse"):
        assert marker not in src
