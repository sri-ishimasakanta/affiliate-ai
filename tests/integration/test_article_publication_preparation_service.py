"""ArticlePublicationPreparationService — D-D3 READ-ONLY tracked HTML preparation。

D-D2/D-D1A の eligibility 判定ロジックを再実装せず再利用していることを、
service レベルの配線として確認する (個々の reason-code 導出そのものの網羅的な
検証は D-D2 の test_article_link_occurrence_preview_service.py が既に持つ)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.affiliate.projection import (
    PROJECTION_STATUS_ACTIVE,
    PROJECTION_VERSION_ACTIVE,
    PROJECTION_VERSION_DISABLED,
    projection_from_target,
)
from app.affiliate.projection_push_acknowledgement import (
    serialize_manifest,
    token_fingerprint,
)
from app.config.settings import Settings
from app.models import AffiliateLinkTarget, Article
from app.repositories.affiliate_link_target_repository import (
    AffiliateLinkTargetRepository,
)
from app.repositories.affiliate_target_projection_push_run_repository import (
    AffiliateTargetProjectionPushRunRepository,
)
from app.repositories.article_link_substitution_mapping_repository import (
    ArticleLinkSubstitutionMappingRepository,
)
from app.services.article_link_occurrence_preview_service import (
    ArticleLinkOccurrencePreviewService,
)
from app.services.article_link_substitution_service import (
    ArticleLinkSubstitutionService,
)
from app.services.article_publication_preparation_service import (
    ArticlePublicationPreparationService,
)
from app.wordpress.publication_artifact import compute_tracked_html_hash
from app.wordpress.publication_substitution import validate_tracked_html

_ORIGIN = "https://bizfluxlab.com"
_HREF = "https://official.example.test/tool-a"


def _settings(base_url: str | None = _ORIGIN) -> Settings:
    return Settings(wordpress_base_url=base_url)


def _svc(session: Session, base_url: str | None = _ORIGIN) -> ArticlePublicationPreparationService:
    return ArticlePublicationPreparationService(session, settings=_settings(base_url))


def _seed_article(session: Session, *, slug: str = "p1", body: str | None = None) -> Article:
    art = Article(title="t", slug=slug, keyword_id=None, body=body or f"[tool]({_HREF})\n")
    session.add(art)
    session.commit()
    return art


def _seed_target(
    session: Session,
    *,
    article_id: int,
    token: str = "AAAA0000tokenone00000",
    affiliate_program_id: int = 1,
    status: str = "active",
) -> AffiliateLinkTarget:
    target = AffiliateLinkTarget(
        token=token, article_id=article_id, affiliate_program_id=affiliate_program_id,
        destination_url="https://aff.example.test/x", destination_host="aff.example.test",
        status=status, link_identity_hash="1" * 64,
    )
    session.add(target)
    session.commit()
    return target


def _first_occurrence_hash(session: Session, article: Article) -> str:
    preview = ArticleLinkOccurrencePreviewService(session, settings=_settings()).preview(article.id)
    assert len(preview.occurrences) == 1
    return preview.occurrences[0].occurrence_identity_hash


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


def _succeed_run(session: Session, *, manifest: list[dict], runtime_origin: str = _ORIGIN):
    repo = AffiliateTargetProjectionPushRunRepository(session)
    run = repo.add_running(
        snapshot_scope="full", runtime_origin=runtime_origin, requested_snapshot_hash="a" * 64,
        requested_target_count=len(manifest),
        request_manifest_json=serialize_manifest(manifest), started_at=datetime.now(UTC),
    )
    repo.mark_succeeded(
        run, http_status=200, response_projection_snapshot_hash="a" * 64,
        received_count=len(manifest), inserted_count=len(manifest), updated_count=0,
        unchanged_count=0, finished_at=datetime.now(UTC),
    )
    session.commit()
    return run


def _mapped_active_eligible(session: Session):
    """1 occurrence が eligible な mapping+target+ack を持つ状態を作る。"""

    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    occ_hash = _first_occurrence_hash(session, art)
    mapping = ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    return art, target, mapping


# ==================== zero substitution (§16) ================================
def test_zero_eligible_mappings_is_valid(session: Session) -> None:
    art = _seed_article(session)
    result = _svc(session).prepare(art.id)
    assert result.substitution_manifest == []
    assert result.substitution_count == 0
    assert result.tracked_html == result.canonical_html
    assert result.tracked_html_hash == compute_tracked_html_hash(result.canonical_html)


# ==================== happy path =============================================
def test_one_eligible_mapping_substitutes(session: Session) -> None:
    art, target, mapping = _mapped_active_eligible(session)
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 1
    assert result.tracked_html != result.canonical_html
    entry = result.substitution_manifest[0]
    assert entry["mapping_id"] == mapping.id
    assert entry["affiliate_link_target_id"] == target.id
    assert entry["token"] == target.token
    assert entry["target_projection_version"] == PROJECTION_VERSION_ACTIVE
    validate_tracked_html(
        canonical_html=result.canonical_html,
        external_links=[_HREF],
        canonical_body_hash=result.canonical_body_hash,
        renderer_version=result.renderer_version,
        tracked_html=result.tracked_html,
        manifest=result.substitution_manifest,
    )


# ==================== §31: stale current state ===============================
def test_no_active_mapping_excluded(session: Session) -> None:
    art = _seed_article(session)
    _seed_target(session, article_id=art.id)
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_mapping_superseded_excluded(session: Session) -> None:
    art, target, mapping = _mapped_active_eligible(session)
    other_target = _seed_target(
        session, article_id=art.id, token="BBBB0000tokentwoo0000", affiliate_program_id=2
    )
    ArticleLinkSubstitutionService(session).remap_occurrence(
        mapping.id, new_affiliate_link_target_id=other_target.id
    )
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_mapping_revoked_excluded(session: Session) -> None:
    art, target, mapping = _mapped_active_eligible(session)
    ArticleLinkSubstitutionService(session).revoke_mapping(mapping.id)
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_target_missing_excluded(session: Session) -> None:
    art = _seed_article(session)
    occ_hash = _first_occurrence_hash(session, art)
    repo = ArticleLinkSubstitutionMappingRepository(session)
    repo.add_active(
        article_id=art.id, occurrence_identity_hash=occ_hash, original_href=_HREF,
        affiliate_link_target_id=999999, approved_at=datetime.now(UTC), idempotency_key=None,
    )
    session.commit()
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_cross_article_target_excluded(session: Session) -> None:
    art1 = _seed_article(session, slug="p1")
    art2 = _seed_article(session, slug="p2")
    other_target = _seed_target(session, article_id=art2.id, token="CCCC0000tokenthre0000")
    occ_hash = _first_occurrence_hash(session, art1)
    repo = ArticleLinkSubstitutionMappingRepository(session)
    repo.add_active(
        article_id=art1.id, occurrence_identity_hash=occ_hash, original_href=_HREF,
        affiliate_link_target_id=other_target.id, approved_at=datetime.now(UTC),
        idempotency_key=None,
    )
    session.commit()
    result = _svc(session).prepare(art1.id)
    assert result.substitution_count == 0


def test_target_disabled_excluded(session: Session) -> None:
    art, target, mapping = _mapped_active_eligible(session)
    AffiliateLinkTargetRepository(session).mark_disabled(target, disabled_at=datetime.now(UTC))
    session.commit()
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_no_authoritative_ack_excluded(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    occ_hash = _first_occurrence_hash(session, art)
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_ambiguous_ack_excluded(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    occ_hash = _first_occurrence_hash(session, art)
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    repo = AffiliateTargetProjectionPushRunRepository(session)
    repo.add_running(
        snapshot_scope="full", runtime_origin=_ORIGIN, requested_snapshot_hash="a" * 64,
        requested_target_count=1,
        request_manifest_json=serialize_manifest([_matching_manifest_entry(target)]),
        started_at=datetime.now(UTC),
    )
    session.commit()
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_target_absent_from_snapshot_excluded(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    other_target = _seed_target(
        session, article_id=art.id, token="DDDD0000tokenfour0000", affiliate_program_id=2
    )
    occ_hash = _first_occurrence_hash(session, art)
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(other_target)])
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_token_fingerprint_mismatch_excluded(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    occ_hash = _first_occurrence_hash(session, art)
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    entry = _matching_manifest_entry(target)
    entry["token_fingerprint"] = "f" * 64
    _succeed_run(session, manifest=[entry])
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_link_identity_hash_mismatch_excluded(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    occ_hash = _first_occurrence_hash(session, art)
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    entry = _matching_manifest_entry(target)
    entry["link_identity_hash"] = "9" * 64
    _succeed_run(session, manifest=[entry])
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_entry_hash_mismatch_excluded(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    occ_hash = _first_occurrence_hash(session, art)
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    entry = _matching_manifest_entry(target)
    entry["entry_hash"] = "8" * 64
    _succeed_run(session, manifest=[entry])
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


def test_projection_version_mismatch_excluded(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    occ_hash = _first_occurrence_hash(session, art)
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    entry = _matching_manifest_entry(target)
    entry["projection_version"] = PROJECTION_VERSION_DISABLED
    _succeed_run(session, manifest=[entry])
    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 0


# ==================== §32: historical verification ===========================
def test_historical_verification_survives_downstream_lifecycle_changes(
    session: Session,
) -> None:
    art, target, mapping = _mapped_active_eligible(session)
    prepared = _svc(session).prepare(art.id)
    assert prepared.substitution_count == 1

    frozen_canonical_html = prepared.canonical_html
    frozen_tracked_html = prepared.tracked_html
    frozen_manifest = prepared.substitution_manifest
    frozen_artifact_hash = prepared.artifact_hash

    # --- 周辺 control-plane 状態を準備後に変化させる ----------------------
    AffiliateLinkTargetRepository(session).mark_disabled(target, disabled_at=datetime.now(UTC))
    session.commit()

    other_target = _seed_target(
        session, article_id=art.id, token="EEEE0000tokenfive0000", affiliate_program_id=2
    )
    remapped = ArticleLinkSubstitutionService(session).remap_occurrence(
        mapping.id, new_affiliate_link_target_id=other_target.id
    )
    ArticleLinkSubstitutionService(session).revoke_mapping(remapped.id)

    _succeed_run(session, manifest=[_matching_manifest_entry(other_target)], runtime_origin=_ORIGIN)

    # --- 既に準備済みの frozen tracked_html/manifest は、現在の DB 状態を一切
    #     参照せずに strict 検証できる。
    validate_tracked_html(
        canonical_html=frozen_canonical_html,
        external_links=[_HREF],
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        tracked_html=frozen_tracked_html,
        manifest=frozen_manifest,
    )

    from app.wordpress.publication_artifact import compute_artifact_hash

    recomputed_artifact_hash = compute_artifact_hash(
        artifact_schema_version=prepared.artifact_schema_version,
        article_id=art.id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        manifest=frozen_manifest,
    )
    assert recomputed_artifact_hash == frozen_artifact_hash

    # --- 一方、"今" prepare() を呼び直すと、現在の状態 (0 active mapping) を
    #     反映して 0 substitution になる (current eligibility の再評価)。
    fresh = _svc(session).prepare(art.id)
    assert fresh.substitution_count == 0


# ==================== §25: same target, two different occurrences ===========
def test_same_target_two_different_occurrences_allowed(session: Session) -> None:
    art = _seed_article(
        session,
        body=(
            "[a](https://official.example.test/a) "
            "[b](https://official.example.test/b)\n"
        ),
    )
    target = _seed_target(session, article_id=art.id)

    preview = ArticleLinkOccurrencePreviewService(session, settings=_settings()).preview(art.id)
    assert len(preview.occurrences) == 2

    svc = ArticleLinkSubstitutionService(session)
    svc.create_mapping(
        article_id=art.id, occurrence_identity_hash=preview.occurrences[0].occurrence_identity_hash,
        original_href=preview.occurrences[0].original_href, affiliate_link_target_id=target.id,
    )
    svc.create_mapping(
        article_id=art.id, occurrence_identity_hash=preview.occurrences[1].occurrence_identity_hash,
        original_href=preview.occurrences[1].original_href, affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])

    result = _svc(session).prepare(art.id)
    assert result.substitution_count == 2
    target_ids = {e["affiliate_link_target_id"] for e in result.substitution_manifest}
    assert target_ids == {target.id}
    mapping_ids = {e["mapping_id"] for e in result.substitution_manifest}
    assert len(mapping_ids) == 2  # 別々の mapping (同じ target への 2 つの独立した mapping)
