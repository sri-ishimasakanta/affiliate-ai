"""ArticleLinkOccurrencePreviewService — D-D2 READ-ONLY occurrence discovery +
mapping/target eligibility preview。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select
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
from app.exceptions import EntityNotFoundError
from app.models import (
    AffiliateLinkTarget,
    AffiliateTargetProjectionPushRun,
    Article,
    ArticleLinkSubstitutionMapping,
    ArticlePublicationArtifact,
)
from app.models.affiliate_link_target import ALT_DISABLED
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
    REASON_ELIGIBLE,
    REASON_MAPPING_TARGET_MISSING,
    REASON_NO_ACTIVE_MAPPING,
    REASON_NO_RUNTIME_ACKNOWLEDGEMENT,
    REASON_RUNTIME_ACK_AMBIGUOUS,
    REASON_RUNTIME_ACK_TARGET_MISMATCH,
    REASON_TARGET_ABSENT_FROM_LATEST_FULL_SNAPSHOT,
    REASON_TARGET_ARTICLE_MISMATCH,
    REASON_TARGET_NOT_ACTIVE,
    ArticleLinkOccurrencePreviewService,
)
from app.services.article_link_substitution_service import (
    ArticleLinkSubstitutionService,
)

_ORIGIN = "https://bizfluxlab.com"
_HREF = "https://official.example.test/tool-a"


def _settings(base_url: str | None = _ORIGIN) -> Settings:
    return Settings(wordpress_base_url=base_url)


def _svc(session: Session, base_url: str | None = _ORIGIN) -> ArticleLinkOccurrencePreviewService:
    return ArticleLinkOccurrencePreviewService(session, settings=_settings(base_url))


def _seed_article(session: Session, *, slug: str = "p1", body: str | None = None) -> Article:
    art = Article(
        title="t", slug=slug, keyword_id=None,
        body=body or f"[tool]({_HREF})\n",
    )
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
        token=token,
        article_id=article_id,
        affiliate_program_id=affiliate_program_id,
        destination_url="https://aff.example.test/x",
        destination_host="aff.example.test",
        status=status,
        link_identity_hash="1" * 64,
    )
    session.add(target)
    session.commit()
    return target


def _first_occurrence_hash(session: Session, article: Article) -> str:
    preview = _svc(session).preview(article.id)
    assert len(preview.occurrences) == 1
    return preview.occurrences[0].occurrence_identity_hash


def _succeed_run(
    session: Session, *, manifest: list[dict], runtime_origin: str = _ORIGIN
) -> AffiliateTargetProjectionPushRun:
    repo = AffiliateTargetProjectionPushRunRepository(session)
    run = repo.add_running(
        snapshot_scope="full", runtime_origin=runtime_origin,
        requested_snapshot_hash="a" * 64, requested_target_count=len(manifest),
        request_manifest_json=serialize_manifest(manifest), started_at=datetime.now(UTC),
    )
    repo.mark_succeeded(
        run, http_status=200, response_projection_snapshot_hash="a" * 64,
        received_count=len(manifest), inserted_count=len(manifest), updated_count=0,
        unchanged_count=0, finished_at=datetime.now(UTC),
    )
    session.commit()
    return run


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


# ==================== basic discovery ======================================
def test_article_not_found_raises(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _svc(session).preview(999999)


def test_no_mappings_all_discovered_zero_eligible(session: Session) -> None:
    art = _seed_article(session)
    preview = _svc(session).preview(art.id)
    assert preview.external_occurrence_count == 1
    assert preview.active_mapping_count == 0
    assert preview.eligible_substitution_count == 0
    occ = preview.occurrences[0]
    assert occ.mapped is False
    assert occ.eligible is False
    assert occ.reason == REASON_NO_ACTIVE_MAPPING


def test_deterministic_output_stable_across_repeated_calls(session: Session) -> None:
    art = _seed_article(
        session,
        body=(
            "[a](https://official.example.test/a) "
            "[b](https://official.example.test/b)\n"
        ),
    )
    p1 = _svc(session).preview(art.id)
    p2 = _svc(session).preview(art.id)
    assert [o.occurrence_ordinal for o in p1.occurrences] == [
        o.occurrence_ordinal for o in p2.occurrences
    ]
    assert [o.occurrence_identity_hash for o in p1.occurrences] == [
        o.occurrence_identity_hash for o in p2.occurrences
    ]
    assert [o.original_href for o in p1.occurrences] == [
        o.original_href for o in p2.occurrences
    ]


# ==================== active mapping -> target lookup ======================
def test_active_mapping_shows_target_lookup(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    occ_hash = _first_occurrence_hash(session, art)
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.mapped is True
    assert occ.mapping_status == "active"
    assert occ.target_id == target.id
    assert occ.target_status == "active"
    assert occ.target_token_fingerprint == token_fingerprint(target.token)
    assert preview.active_mapping_count == 1


def test_mapping_target_missing_fails_closed(session: Session) -> None:
    art = _seed_article(session)
    occ_hash = _first_occurrence_hash(session, art)
    repo = ArticleLinkSubstitutionMappingRepository(session)
    repo.add_active(
        article_id=art.id, occurrence_identity_hash=occ_hash, original_href=_HREF,
        affiliate_link_target_id=999999, approved_at=datetime.now(UTC),
        idempotency_key=None,
    )
    session.commit()
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.mapped is True
    assert occ.eligible is False
    assert occ.reason == REASON_MAPPING_TARGET_MISSING


def test_cross_article_inconsistency_fails_closed(session: Session) -> None:
    art1 = _seed_article(session, slug="p1")
    art2 = _seed_article(session, slug="p2")
    other_target = _seed_target(session, article_id=art2.id, token="BBBB0000tokentwoo0000")
    occ_hash = _first_occurrence_hash(session, art1)
    repo = ArticleLinkSubstitutionMappingRepository(session)
    repo.add_active(
        article_id=art1.id, occurrence_identity_hash=occ_hash, original_href=_HREF,
        affiliate_link_target_id=other_target.id, approved_at=datetime.now(UTC),
        idempotency_key=None,
    )
    session.commit()
    preview = _svc(session).preview(art1.id)
    occ = preview.occurrences[0]
    assert occ.eligible is False
    assert occ.reason == REASON_TARGET_ARTICLE_MISMATCH


def test_target_inactive_fails_closed(session: Session) -> None:
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    occ_hash = _first_occurrence_hash(session, art)
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    AffiliateLinkTargetRepository(session).mark_disabled(target, disabled_at=datetime.now(UTC))
    session.commit()
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.target_status == ALT_DISABLED
    assert occ.eligible is False
    assert occ.reason == REASON_TARGET_NOT_ACTIVE


# ==================== runtime acknowledgement =============================
def _mapped_active_target(session: Session):
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    occ_hash = _first_occurrence_hash(session, art)
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ_hash,
        original_href=_HREF, affiliate_link_target_id=target.id,
    )
    return art, target


def test_no_ack_history_fails_closed(session: Session) -> None:
    art, _target = _mapped_active_target(session)
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.eligible is False
    assert occ.reason == REASON_NO_RUNTIME_ACKNOWLEDGEMENT


def test_wordpress_base_url_not_configured_fails_closed(session: Session) -> None:
    art, _target = _mapped_active_target(session)
    preview = _svc(session, base_url=None).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.eligible is False
    assert occ.reason == REASON_NO_RUNTIME_ACKNOWLEDGEMENT


def test_latest_running_ack_fails_closed(session: Session) -> None:
    art, target = _mapped_active_target(session)
    repo = AffiliateTargetProjectionPushRunRepository(session)
    repo.add_running(
        snapshot_scope="full", runtime_origin=_ORIGIN, requested_snapshot_hash="a" * 64,
        requested_target_count=1,
        request_manifest_json=serialize_manifest([_matching_manifest_entry(target)]),
        started_at=datetime.now(UTC),
    )
    session.commit()
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.eligible is False
    assert occ.reason == REASON_RUNTIME_ACK_AMBIGUOUS


def test_latest_outcome_unknown_fails_closed(session: Session) -> None:
    art, target = _mapped_active_target(session)
    repo = AffiliateTargetProjectionPushRunRepository(session)
    run = repo.add_running(
        snapshot_scope="full", runtime_origin=_ORIGIN, requested_snapshot_hash="a" * 64,
        requested_target_count=1,
        request_manifest_json=serialize_manifest([_matching_manifest_entry(target)]),
        started_at=datetime.now(UTC),
    )
    repo.mark_outcome_unknown(
        run, error_message="timeout", finished_at=datetime.now(UTC),
    )
    session.commit()
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.eligible is False
    assert occ.reason == REASON_RUNTIME_ACK_AMBIGUOUS


def test_latest_failed_then_prior_succeeded_resolver_semantics_preserved(
    session: Session,
) -> None:
    art, target = _mapped_active_target(session)
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])

    repo = AffiliateTargetProjectionPushRunRepository(session)
    failing_run = repo.add_running(
        snapshot_scope="full", runtime_origin=_ORIGIN, requested_snapshot_hash="b" * 64,
        requested_target_count=1,
        request_manifest_json=serialize_manifest([_matching_manifest_entry(target)]),
        started_at=datetime.now(UTC),
    )
    repo.mark_failed(
        failing_run, error_message="bad_schema_version", finished_at=datetime.now(UTC),
        http_status=400, server_code="bad_schema_version",
    )
    session.commit()

    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    # failed (definitive) は透過的にスキップされ、1 つ前の succeeded manifest が使われる。
    assert occ.eligible is True
    assert occ.reason == REASON_ELIGIBLE


def test_latest_succeeded_but_target_omitted_fails_closed(session: Session) -> None:
    art, target = _mapped_active_target(session)
    other_target = _seed_target(
        session, article_id=art.id, token="ZZZZ0000tokenzzzz0000", affiliate_program_id=2
    )
    # manifest には別 target だけが含まれる -> この target は省略されている。
    _succeed_run(session, manifest=[_matching_manifest_entry(other_target)])
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.eligible is False
    assert occ.reason == REASON_TARGET_ABSENT_FROM_LATEST_FULL_SNAPSHOT


def test_latest_succeeded_exact_match_eligible(session: Session) -> None:
    art, target = _mapped_active_target(session)
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.eligible is True
    assert occ.reason == REASON_ELIGIBLE
    assert preview.eligible_substitution_count == 1


def test_fingerprint_mismatch_fails_closed(session: Session) -> None:
    art, target = _mapped_active_target(session)
    entry = _matching_manifest_entry(target)
    entry["token_fingerprint"] = "f" * 64
    _succeed_run(session, manifest=[entry])
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.eligible is False
    assert occ.reason == REASON_RUNTIME_ACK_TARGET_MISMATCH


def test_link_identity_hash_mismatch_fails_closed(session: Session) -> None:
    art, target = _mapped_active_target(session)
    entry = _matching_manifest_entry(target)
    entry["link_identity_hash"] = "9" * 64
    _succeed_run(session, manifest=[entry])
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.eligible is False
    assert occ.reason == REASON_RUNTIME_ACK_TARGET_MISMATCH


def test_entry_hash_mismatch_fails_closed(session: Session) -> None:
    art, target = _mapped_active_target(session)
    entry = _matching_manifest_entry(target)
    entry["entry_hash"] = "8" * 64
    _succeed_run(session, manifest=[entry])
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.eligible is False
    assert occ.reason == REASON_RUNTIME_ACK_TARGET_MISMATCH


def test_projection_version_2_fails_closed(session: Session) -> None:
    art, target = _mapped_active_target(session)
    entry = _matching_manifest_entry(target)
    entry["projection_version"] = PROJECTION_VERSION_DISABLED
    _succeed_run(session, manifest=[entry])
    preview = _svc(session).preview(art.id)
    occ = preview.occurrences[0]
    assert occ.eligible is False
    assert occ.reason == REASON_RUNTIME_ACK_TARGET_MISMATCH


# ==================== no writes / no network (§32) ==========================
def test_preview_service_module_never_imports_httpx() -> None:
    import app.services.article_link_occurrence_preview_service as mod

    assert not hasattr(mod, "httpx")


def test_preview_performs_zero_db_writes_and_zero_network(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    art, target = _mapped_active_target(session)
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])

    def _forbidden_request(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("preview must never perform an HTTP request")

    monkeypatch.setattr(httpx.Client, "request", _forbidden_request)
    monkeypatch.setattr(httpx.Client, "send", _forbidden_request)

    def _row_counts() -> dict[str, int]:
        return {
            "mappings": session.scalar(
                select(func.count()).select_from(ArticleLinkSubstitutionMapping)
            ),
            "artifacts": session.scalar(
                select(func.count()).select_from(ArticlePublicationArtifact)
            ),
            "targets": session.scalar(
                select(func.count()).select_from(AffiliateLinkTarget)
            ),
            "push_runs": session.scalar(
                select(func.count()).select_from(AffiliateTargetProjectionPushRun)
            ),
        }

    before = _row_counts()

    committed = {"count": 0}
    original_commit = session.commit

    def _tracking_commit():
        committed["count"] += 1
        return original_commit()

    monkeypatch.setattr(session, "commit", _tracking_commit)

    preview = _svc(session).preview(art.id)

    after = _row_counts()
    assert before == after
    assert committed["count"] == 0
    assert preview.eligible_substitution_count == 1
