"""scripts.manage_article_publication_artifact — D-D4 CLI plumbing.

write モード (persist/approve) は明示フラグなしでは refuse されること、
plan/inspect が read-only であること、full token が一切出力に出ないことを
in-memory DB に対して検証する。production には一切触れない。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.affiliate.projection import (  # noqa: E402
    PROJECTION_STATUS_ACTIVE,
    PROJECTION_VERSION_ACTIVE,
    projection_from_target,
)
from app.affiliate.projection_push_acknowledgement import (  # noqa: E402
    serialize_manifest,
    token_fingerprint,
)
from app.config.database import build_engine  # noqa: E402
from app.config.settings import Settings  # noqa: E402
from app.models import (  # noqa: E402
    AffiliateLinkTarget,
    Article,
    ArticlePublicationArtifact,
    Base,
)
from app.repositories.affiliate_target_projection_push_run_repository import (  # noqa: E402
    AffiliateTargetProjectionPushRunRepository,
)
from app.services.article_link_occurrence_preview_service import (  # noqa: E402
    ArticleLinkOccurrencePreviewService,
)
from app.services.article_link_substitution_service import (  # noqa: E402
    ArticleLinkSubstitutionService,
)
from scripts.manage_article_publication_artifact import main  # noqa: E402

_HREF = "https://official.example.test/tool-a"
_REAL_TOKEN = "SUPERSECRETREALTOKENAA"


@pytest.fixture
def cli_engine() -> Engine:
    engine = build_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def cli_session_factory(cli_engine: Engine):
    factory = sessionmaker(bind=cli_engine, autoflush=False, expire_on_commit=False)

    def _make() -> Session:
        return factory()

    return _make


def _seed_article(cli_session_factory) -> int:
    with cli_session_factory() as session:
        art = Article(title="t", slug="p1", keyword_id=None, body=f"[tool]({_HREF})\n")
        session.add(art)
        session.commit()
        return art.id


def _count_artifacts(cli_session_factory) -> int:
    with cli_session_factory() as session:
        return session.scalar(select(func.count()).select_from(ArticlePublicationArtifact))


def _patch_session_local(monkeypatch, cli_session_factory) -> None:
    monkeypatch.setattr(
        "scripts.manage_article_publication_artifact.SessionLocal", cli_session_factory
    )


# ==================== plan (read-only) ========================================
def test_plan_is_read_only(cli_session_factory, monkeypatch, capsys) -> None:
    article_id = _seed_article(cli_session_factory)
    _patch_session_local(monkeypatch, cli_session_factory)

    code = main(["plan", "--article-id", str(article_id)])
    assert code == 0
    out = capsys.readouterr().out
    assert "substitution_count          = 0" in out
    assert "no DB write" in out
    assert _count_artifacts(cli_session_factory) == 0


# ==================== persist (write, gated) ==================================
def test_persist_without_execute_is_refused(cli_session_factory, monkeypatch, capsys) -> None:
    article_id = _seed_article(cli_session_factory)
    _patch_session_local(monkeypatch, cli_session_factory)

    code = main(["persist", "--article-id", str(article_id)])
    assert code == 4
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert _count_artifacts(cli_session_factory) == 0


def test_persist_with_execute_writes(cli_session_factory, monkeypatch, capsys) -> None:
    article_id = _seed_article(cli_session_factory)
    _patch_session_local(monkeypatch, cli_session_factory)

    code = main(["persist", "--article-id", str(article_id), "--execute"])
    assert code == 0
    out = capsys.readouterr().out
    assert "artifact_id" in out
    assert _count_artifacts(cli_session_factory) == 1


# ==================== inspect (read-only, masked) ==============================
def test_inspect_masks_full_token(cli_session_factory, monkeypatch, capsys) -> None:
    settings = Settings(wordpress_base_url="https://bizfluxlab.com")
    with cli_session_factory() as session:
        art = Article(title="t", slug="p2", keyword_id=None, body=f"[tool]({_HREF})\n")
        session.add(art)
        session.commit()
        target = AffiliateLinkTarget(
            token=_REAL_TOKEN,
            article_id=art.id,
            affiliate_program_id=1,
            destination_url="https://aff.example.test/x",
            destination_host="aff.example.test",
            status="active",
            link_identity_hash="1" * 64,
        )
        session.add(target)
        session.commit()
        preview = ArticleLinkOccurrencePreviewService(session, settings=settings).preview(art.id)
        occ0 = preview.occurrences[0]
        ArticleLinkSubstitutionService(session).create_mapping(
            article_id=art.id,
            occurrence_identity_hash=occ0.occurrence_identity_hash,
            original_href=occ0.original_href,
            affiliate_link_target_id=target.id,
        )
        proj = projection_from_target(target)
        manifest = [
            {
                "affiliate_link_target_id": target.id,
                "token_fingerprint": token_fingerprint(target.token),
                "link_identity_hash": target.link_identity_hash,
                "status": PROJECTION_STATUS_ACTIVE,
                "projection_version": PROJECTION_VERSION_ACTIVE,
                "entry_hash": proj.projection_entry_hash,
            }
        ]
        run_repo = AffiliateTargetProjectionPushRunRepository(session)
        run = run_repo.add_running(
            snapshot_scope="full",
            runtime_origin="https://bizfluxlab.com",
            requested_snapshot_hash="a" * 64,
            requested_target_count=1,
            request_manifest_json=serialize_manifest(manifest),
            started_at=datetime.now(UTC),
        )
        run_repo.mark_succeeded(
            run,
            http_status=200,
            response_projection_snapshot_hash="a" * 64,
            received_count=1,
            inserted_count=1,
            updated_count=0,
            unchanged_count=0,
            finished_at=datetime.now(UTC),
        )
        session.commit()
        article_id = art.id

    _patch_session_local(monkeypatch, cli_session_factory)
    persist_code = main(["persist", "--article-id", str(article_id), "--execute"])
    assert persist_code == 0
    capsys.readouterr()

    with cli_session_factory() as session:
        artifact_id = session.scalar(select(ArticlePublicationArtifact.id))

    inspect_code = main(["inspect", "--artifact-id", str(artifact_id)])
    assert inspect_code == 0
    out = capsys.readouterr().out
    assert _REAL_TOKEN not in out
    assert "aff.example.test" not in out
    assert "https://bizfluxlab.com/go/<masked>" in out
    assert "artifact_hash_valid          = True" in out


def test_inspect_missing_artifact_not_found(cli_session_factory, monkeypatch, capsys) -> None:
    _patch_session_local(monkeypatch, cli_session_factory)
    code = main(["inspect", "--artifact-id", "999999"])
    assert code == 1
    out = capsys.readouterr().out
    assert "NOT FOUND" in out


# ==================== approve (write, gated) ===================================
def test_approve_without_flag_is_refused(cli_session_factory, monkeypatch, capsys) -> None:
    article_id = _seed_article(cli_session_factory)
    _patch_session_local(monkeypatch, cli_session_factory)
    main(["persist", "--article-id", str(article_id), "--execute"])
    capsys.readouterr()

    with cli_session_factory() as session:
        artifact = session.execute(select(ArticlePublicationArtifact)).scalar_one()
        artifact_id, artifact_hash = artifact.id, artifact.artifact_hash

    code = main(
        [
            "approve",
            "--artifact-id",
            str(artifact_id),
            "--expected-artifact-hash",
            artifact_hash,
        ]
    )
    assert code == 4
    out = capsys.readouterr().out
    assert "REFUSED" in out

    with cli_session_factory() as session:
        artifact = session.get(ArticlePublicationArtifact, artifact_id)
        assert artifact.approved_at is None


def test_approve_with_flag_and_correct_hash_writes(
    cli_session_factory, monkeypatch, capsys
) -> None:
    article_id = _seed_article(cli_session_factory)
    _patch_session_local(monkeypatch, cli_session_factory)
    main(["persist", "--article-id", str(article_id), "--execute"])
    capsys.readouterr()

    with cli_session_factory() as session:
        artifact = session.execute(select(ArticlePublicationArtifact)).scalar_one()
        artifact_id, artifact_hash = artifact.id, artifact.artifact_hash

    code = main(
        [
            "approve",
            "--artifact-id",
            str(artifact_id),
            "--expected-artifact-hash",
            artifact_hash,
            "--approve",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Approved" in out

    with cli_session_factory() as session:
        artifact = session.get(ArticlePublicationArtifact, artifact_id)
        assert artifact.approved_at is not None


def test_approve_wrong_hash_rejected(cli_session_factory, monkeypatch, capsys) -> None:
    article_id = _seed_article(cli_session_factory)
    _patch_session_local(monkeypatch, cli_session_factory)
    main(["persist", "--article-id", str(article_id), "--execute"])
    capsys.readouterr()

    with cli_session_factory() as session:
        artifact = session.execute(select(ArticlePublicationArtifact)).scalar_one()
        artifact_id = artifact.id

    code = main(
        [
            "approve",
            "--artifact-id",
            str(artifact_id),
            "--expected-artifact-hash",
            "f" * 64,
            "--approve",
        ]
    )
    assert code == 2
    out = capsys.readouterr().out
    assert "REJECTED" in out

    with cli_session_factory() as session:
        artifact = session.get(ArticlePublicationArtifact, artifact_id)
        assert artifact.approved_at is None
