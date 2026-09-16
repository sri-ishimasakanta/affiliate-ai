"""ArticlePublicationArtifactInspectionService — D-D4 READ-ONLY Human inspection:
independent re-verification + token/secret masking + historical independence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.affiliate.projection import (
    PROJECTION_STATUS_ACTIVE,
    PROJECTION_VERSION_ACTIVE,
    projection_from_target,
)
from app.affiliate.projection_push_acknowledgement import (
    serialize_manifest,
    token_fingerprint,
)
from app.config.settings import Settings
from app.exceptions import EntityNotFoundError
from app.models import AffiliateLinkTarget, Article
from app.repositories.affiliate_target_projection_push_run_repository import (
    AffiliateTargetProjectionPushRunRepository,
)
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.services.article_link_occurrence_preview_service import (
    ArticleLinkOccurrencePreviewService,
)
from app.services.article_link_substitution_service import (
    ArticleLinkSubstitutionService,
)
from app.services.article_publication_artifact_inspection_service import (
    CURRENT_CANONICAL_ARTICLE_NOT_FOUND,
    CURRENT_CANONICAL_DRIFT,
    CURRENT_CANONICAL_MATCH,
    ArticlePublicationArtifactInspectionService,
)
from app.services.article_publication_artifact_persistence_service import (
    ArticlePublicationArtifactPersistenceService,
)
from app.services.article_publication_preparation_service import (
    ArticlePublicationPreparationService,
)

_HREF = "https://official.example.test/tool-a"
_REAL_TOKEN = "REALSECRETTOKENFORTEST"


def _settings() -> Settings:
    return Settings(wordpress_base_url="https://bizfluxlab.com")


def _seed_article(session: Session, *, slug: str = "p1") -> Article:
    art = Article(title="t", slug=slug, keyword_id=None, body=f"[tool]({_HREF})\n")
    session.add(art)
    session.commit()
    return art


def _seed_target(
    session: Session, *, article_id: int, token: str = _REAL_TOKEN
) -> AffiliateLinkTarget:
    target = AffiliateLinkTarget(
        token=token, article_id=article_id, affiliate_program_id=1,
        destination_url="https://aff.example.test/x", destination_host="aff.example.test",
        status="active", link_identity_hash="1" * 64,
    )
    session.add(target)
    session.commit()
    return target


def _matching_manifest_entry(target: AffiliateLinkTarget) -> dict:
    proj = projection_from_target(target)
    return {
        "affiliate_link_target_id": target.id,
        "token_fingerprint": token_fingerprint(target.token),
        "link_identity_hash": target.link_identity_hash,
        "status": PROJECTION_STATUS_ACTIVE,
        "projection_version": PROJECTION_VERSION_ACTIVE,
        "entry_hash": proj.projection_entry_hash,
    }


def _succeed_run(session: Session, *, manifest: list[dict]) -> None:
    repo = AffiliateTargetProjectionPushRunRepository(session)
    run = repo.add_running(
        snapshot_scope="full", runtime_origin="https://bizfluxlab.com",
        requested_snapshot_hash="a" * 64, requested_target_count=len(manifest),
        request_manifest_json=serialize_manifest(manifest), started_at=datetime.now(UTC),
    )
    repo.mark_succeeded(
        run, http_status=200, response_projection_snapshot_hash="a" * 64,
        received_count=len(manifest), inserted_count=len(manifest), updated_count=0,
        unchanged_count=0, finished_at=datetime.now(UTC),
    )
    session.commit()


def _persisted_artifact_with_substitution(session: Session):
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    preview = ArticleLinkOccurrencePreviewService(session, settings=_settings()).preview(art.id)
    occ0 = preview.occurrences[0]
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ0.occurrence_identity_hash,
        original_href=occ0.original_href, affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
    return art, target, artifact


def _inspect(session: Session, artifact_id: int):
    return ArticlePublicationArtifactInspectionService(session).inspect(artifact_id)


# ==================== §33: inspection security =================================
def test_inspection_never_exposes_full_token(session: Session) -> None:
    _art, target, artifact = _persisted_artifact_with_substitution(session)
    insp = _inspect(session, artifact.id)

    assert len(insp.manifest_summary) == 1
    entry = insp.manifest_summary[0]
    assert _REAL_TOKEN not in entry.replacement_href_masked
    assert entry.replacement_href_masked == "https://bizfluxlab.com/go/<masked>"
    assert entry.token_fingerprint == token_fingerprint(target.token)
    assert _REAL_TOKEN not in entry.token_fingerprint

    # dataclass repr も含め、どこにも full token が出てこないこと。
    full_repr = repr(insp)
    assert _REAL_TOKEN not in full_repr
    assert "aff.example.test" not in full_repr  # destination_host も出さない
    assert "AFFILIATE_RUNTIME_SHARED_SECRET" not in full_repr


def test_inspection_does_not_expose_destination_url_or_secret(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    insp = _inspect(session, artifact.id)
    full_repr = repr(insp)
    assert "https://aff.example.test/x" not in full_repr
    assert "shared_secret" not in full_repr.lower()
    assert "hmac" not in full_repr.lower()


# ==================== §34: inspection integrity =================================
def test_inspection_valid_artifact_all_checks_true(session: Session) -> None:
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    insp = _inspect(session, artifact.id)
    assert insp.artifact_hash_valid is True
    assert insp.tracked_html_hash_valid is True
    assert insp.manifest_valid is True
    assert insp.strict_html_validation_valid is True
    assert insp.current_canonical_status == CURRENT_CANONICAL_MATCH


def test_inspection_detects_tracked_html_hash_mismatch(session: Session) -> None:
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    artifact.tracked_html_hash = "f" * 64
    session.commit()

    insp = _inspect(session, artifact.id)
    assert insp.tracked_html_hash_valid is False


def test_inspection_detects_artifact_hash_mismatch(session: Session) -> None:
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    # artifact_hash 自体を書き換える (unique constraint に注意して別値にする)。
    artifact.artifact_hash = "e" * 64
    session.commit()

    insp = _inspect(session, artifact.id)
    assert insp.artifact_hash_valid is False
    # 他のチェックは独立している -- manifest/tracked_html 自体は壊れていない。
    assert insp.tracked_html_hash_valid is True
    assert insp.manifest_valid is True


def test_inspection_detects_manifest_corruption(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)

    artifact.substitution_manifest_json = "not-valid-json{{{"
    session.commit()

    insp = _inspect(session, artifact.id)
    assert insp.manifest_valid is False
    assert insp.artifact_hash_valid is False  # manifest 不正なので再計算もできない
    assert insp.strict_html_validation_valid is False
    assert insp.manifest_summary == []


def test_inspection_detects_strict_reverse_validation_failure(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)

    # tracked_html から target="_blank" を剥がして、manifest との整合を壊す
    # (D-D3 の reverse_tracked_html が矛盾を検出して例外を出すはず)。
    manifest = json.loads(artifact.substitution_manifest_json)
    replacement_href = manifest[0]["replacement_href"]
    tampered_html = artifact.tracked_html.replace(
        f'href="{replacement_href}" target="_blank"', f'href="{replacement_href}"'
    )
    assert tampered_html != artifact.tracked_html
    artifact.tracked_html = tampered_html
    session.commit()

    insp = _inspect(session, artifact.id)
    assert insp.strict_html_validation_valid is False


def test_inspection_current_canonical_match_vs_drift(session: Session) -> None:
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    insp_match = _inspect(session, artifact.id)
    assert insp_match.current_canonical_status == CURRENT_CANONICAL_MATCH
    assert insp_match.artifact_hash_valid is True  # drift とは独立

    art.body = "totally different body now\n"
    session.commit()

    insp_drift = _inspect(session, artifact.id)
    assert insp_drift.current_canonical_status == CURRENT_CANONICAL_DRIFT
    # artifact 自体の凍結整合性は drift の影響を受けない (§20)。
    assert insp_drift.artifact_hash_valid is True
    assert insp_drift.tracked_html_hash_valid is True
    assert insp_drift.manifest_valid is True
    assert insp_drift.strict_html_validation_valid is True


def test_inspection_missing_artifact_raises(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _inspect(session, 999999)


# ==================== §19: historical independence (no current-state calls) =
def test_inspection_does_not_consult_current_mapping_or_target_state(
    session: Session,
) -> None:
    art, target, artifact = _persisted_artifact_with_substitution(session)

    # mapping/target の現在の lifecycle を変化させる。
    from app.repositories.affiliate_link_target_repository import (
        AffiliateLinkTargetRepository,
    )

    AffiliateLinkTargetRepository(session).mark_disabled(target, disabled_at=datetime.now(UTC))
    session.commit()

    insp = _inspect(session, artifact.id)
    # historical artifact 自体の整合性検証は現在の target 状態に一切影響されない。
    assert insp.artifact_hash_valid is True
    assert insp.tracked_html_hash_valid is True
    assert insp.manifest_valid is True
    assert insp.strict_html_validation_valid is True
    assert insp.current_canonical_status == CURRENT_CANONICAL_MATCH


def test_inspection_reports_article_not_found_status(session: Session) -> None:
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)

    # Article は D-D4 でも削除しないが、frozen artifact が article_id を
    # 参照できない状況を安全にシミュレートするために repository を直接使う。
    repo = ArticlePublicationArtifactRepository(session)
    forged = repo.add(
        article_id=999999,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        artifact_schema_version=prepared.artifact_schema_version,
        substitution_manifest_json=prepared.substitution_manifest_json,
        artifact_hash="d" * 64,
        tracked_html=prepared.tracked_html,
        tracked_html_hash=prepared.tracked_html_hash,
        substitution_count=prepared.substitution_count,
        approved_at=None,
        approved_artifact_hash=None,
        generated_at=datetime.now(UTC),
    )
    session.commit()

    insp = _inspect(session, forged.id)
    assert insp.current_canonical_status == CURRENT_CANONICAL_ARTICLE_NOT_FOUND
    assert insp.article_title == ""
