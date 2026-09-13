"""ArticlePublicationArtifactService / ArticlePublicationArtifactRepository —
生成 (idempotent) / artifact_hash 一意性 / 承認 (set-once)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.exceptions import ArticlePublicationArtifactError, EntityNotFoundError
from app.models import Article, ArticlePublicationArtifact
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.services.article_publication_artifact_service import (
    ArticlePublicationArtifactService,
)
from app.wordpress.publication_artifact import expected_replacement_href

_TOKEN = "CCCCCCCCCCCCCCCCCCCCCC"


def _svc(session: Session) -> ArticlePublicationArtifactService:
    return ArticlePublicationArtifactService(session)


def _seed_article(session: Session, slug: str = "p1") -> Article:
    art = Article(title="t", slug=slug, keyword_id=None, body="# b\n")
    session.add(art)
    session.commit()
    return art


def _manifest_entries(*, token: str = _TOKEN, identity_hash: str = "a" * 64) -> list[dict]:
    return [
        {
            "occurrence_ordinal": 0,
            "occurrence_identity_hash": identity_hash,
            "mapping_id": 1,
            "affiliate_link_target_id": 1,
            "token": token,
            "target_projection_version": 1,
            "original_href": "https://official.example.test/x",
            "replacement_href": expected_replacement_href(token),
            "rel_before": "nofollow",
            "rel_after": "sponsored nofollow",
        }
    ]


def _create_kwargs(article_id: int, **overrides) -> dict:
    base = {
        "article_id": article_id,
        "canonical_body_hash": "b" * 64,
        "renderer_version": "v1",
        "manifest_entries": _manifest_entries(),
        "tracked_html": "<p>tracked</p>",
    }
    base.update(overrides)
    return base


# ==================== creation ============================================
def test_create_artifact_happy_path(session: Session) -> None:
    art = _seed_article(session)
    artifact = _svc(session).create_artifact(**_create_kwargs(art.id))
    assert artifact.article_id == art.id
    assert artifact.substitution_count == 1
    assert artifact.approved_at is None
    assert artifact.approved_artifact_hash is None
    assert len(artifact.artifact_hash) == 64
    assert len(artifact.tracked_html_hash) == 64


def test_create_artifact_frozen_fields_unchanged_across_reads(session: Session) -> None:
    art = _seed_article(session)
    svc = _svc(session)
    created = svc.create_artifact(**_create_kwargs(art.id))
    fetched = svc.get(created.id)
    assert fetched.canonical_body_hash == created.canonical_body_hash
    assert fetched.substitution_manifest_json == created.substitution_manifest_json
    assert fetched.tracked_html == created.tracked_html
    assert fetched.artifact_hash == created.artifact_hash


def test_create_artifact_bad_manifest_raises_domain_error(session: Session) -> None:
    art = _seed_article(session)
    bad_entries = [{"occurrence_ordinal": 0}]  # 形が不正
    with pytest.raises(ArticlePublicationArtifactError):
        _svc(session).create_artifact(**_create_kwargs(art.id, manifest_entries=bad_entries))


# ==================== artifact_hash uniqueness =============================
def test_create_artifact_idempotent_same_inputs_returns_existing_row(
    session: Session,
) -> None:
    art = _seed_article(session)
    svc = _svc(session)
    a = svc.create_artifact(**_create_kwargs(art.id))
    b = svc.create_artifact(**_create_kwargs(art.id))
    assert a.id == b.id
    assert a.artifact_hash == b.artifact_hash


def test_create_artifact_different_tracked_html_same_hash_inputs_still_dedupes(
    session: Session,
) -> None:
    """artifact_hash は (schema_version, article_id, canonical_body_hash,
    renderer_version, manifest) のみの関数 — tracked_html 自体は含まれない。
    そのため同一の hash-input 集合であれば tracked_html が違っても同一行として
    dedupe される (idempotency は hash-input の同一性で判定される契約)。"""

    art = _seed_article(session)
    svc = _svc(session)
    a = svc.create_artifact(**_create_kwargs(art.id, tracked_html="<p>v1</p>"))
    b = svc.create_artifact(**_create_kwargs(art.id, tracked_html="<p>v2 different</p>"))
    assert a.id == b.id
    assert a.tracked_html == "<p>v1</p>"  # 最初に作られた行の内容が保持される


def test_create_artifact_different_canonical_body_hash_creates_distinct_row(
    session: Session,
) -> None:
    art = _seed_article(session)
    svc = _svc(session)
    a = svc.create_artifact(**_create_kwargs(art.id, canonical_body_hash="b" * 64))
    b = svc.create_artifact(**_create_kwargs(art.id, canonical_body_hash="c" * 64))
    assert a.id != b.id
    assert a.artifact_hash != b.artifact_hash


def test_artifact_hash_unique_constraint_enforced_at_db_level(session: Session) -> None:
    art = _seed_article(session)
    repo = ArticlePublicationArtifactRepository(session)
    common = {
        "article_id": art.id,
        "canonical_body_hash": "b" * 64,
        "renderer_version": "v1",
        "artifact_schema_version": 1,
        "substitution_manifest_json": "[]",
        "artifact_hash": "d" * 64,
        "tracked_html": "<p>x</p>",
        "tracked_html_hash": "e" * 64,
        "substitution_count": 0,
        "approved_at": None,
        "approved_artifact_hash": None,
        "generated_at": datetime.now(UTC),
    }
    repo.add(**common)
    session.commit()
    with pytest.raises(IntegrityError):
        repo.add(**common)
    session.rollback()


# ==================== approval (set-once) ==================================
def test_approved_fields_null_initially(session: Session) -> None:
    art = _seed_article(session)
    artifact = _svc(session).create_artifact(**_create_kwargs(art.id))
    assert artifact.approved_at is None
    assert artifact.approved_artifact_hash is None


def test_approve_artifact_sets_approved_fields(session: Session) -> None:
    art = _seed_article(session)
    svc = _svc(session)
    artifact = svc.create_artifact(**_create_kwargs(art.id))
    approved = svc.approve_artifact(
        artifact.id, expected_artifact_hash=artifact.artifact_hash
    )
    assert approved.approved_at is not None
    assert approved.approved_artifact_hash == artifact.artifact_hash


def test_approve_artifact_second_approval_rejected(session: Session) -> None:
    art = _seed_article(session)
    svc = _svc(session)
    artifact = svc.create_artifact(**_create_kwargs(art.id))
    svc.approve_artifact(artifact.id, expected_artifact_hash=artifact.artifact_hash)
    with pytest.raises(ArticlePublicationArtifactError, match="already approved"):
        svc.approve_artifact(artifact.id, expected_artifact_hash=artifact.artifact_hash)


def test_approve_artifact_mismatched_hash_rejected(session: Session) -> None:
    art = _seed_article(session)
    svc = _svc(session)
    artifact = svc.create_artifact(**_create_kwargs(art.id))
    with pytest.raises(ArticlePublicationArtifactError, match="does not match"):
        svc.approve_artifact(artifact.id, expected_artifact_hash="f" * 64)
    # 承認は成立していない (mismatched hash では変更されない)。
    refreshed = svc.get(artifact.id)
    assert refreshed.approved_at is None
    assert refreshed.approved_artifact_hash is None


def test_approve_artifact_missing_artifact_raises_not_found(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _svc(session).approve_artifact(999999, expected_artifact_hash="a" * 64)


def test_changed_artifact_is_distinct_row_and_starts_unapproved(session: Session) -> None:
    art = _seed_article(session)
    svc = _svc(session)
    a = svc.create_artifact(**_create_kwargs(art.id, canonical_body_hash="b" * 64))
    svc.approve_artifact(a.id, expected_artifact_hash=a.artifact_hash)

    b = svc.create_artifact(**_create_kwargs(art.id, canonical_body_hash="c" * 64))
    assert b.id != a.id
    assert b.approved_at is None
    assert b.approved_artifact_hash is None

    # 元の a の承認状態は変化しない (immutable)。
    refreshed_a = svc.get(a.id)
    assert refreshed_a.approved_at is not None


def test_immutable_content_fields_unchanged_after_approval(session: Session) -> None:
    art = _seed_article(session)
    svc = _svc(session)
    artifact = svc.create_artifact(**_create_kwargs(art.id))
    before = {
        "canonical_body_hash": artifact.canonical_body_hash,
        "renderer_version": artifact.renderer_version,
        "substitution_manifest_json": artifact.substitution_manifest_json,
        "artifact_hash": artifact.artifact_hash,
        "tracked_html": artifact.tracked_html,
        "tracked_html_hash": artifact.tracked_html_hash,
        "substitution_count": artifact.substitution_count,
    }
    svc.approve_artifact(artifact.id, expected_artifact_hash=artifact.artifact_hash)
    after = svc.get(artifact.id)
    for field, value in before.items():
        assert getattr(after, field) == value


# ==================== reads / no generic mutation path ======================
def test_get_by_hash(session: Session) -> None:
    art = _seed_article(session)
    svc = _svc(session)
    artifact = svc.create_artifact(**_create_kwargs(art.id))
    found = svc.get_by_hash(artifact.artifact_hash)
    assert found is not None
    assert found.id == artifact.id
    assert svc.get_by_hash("f" * 64) is None


def test_list_for_article(session: Session) -> None:
    art = _seed_article(session)
    svc = _svc(session)
    a = svc.create_artifact(**_create_kwargs(art.id, canonical_body_hash="b" * 64))
    b = svc.create_artifact(**_create_kwargs(art.id, canonical_body_hash="c" * 64))
    listed = svc.list_for_article(art.id)
    assert [row.id for row in listed] == sorted([a.id, b.id])


def test_repository_has_no_generic_update_method(session: Session) -> None:
    repo = ArticlePublicationArtifactRepository(session)
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")


def test_model_has_no_destination_or_secret_columns() -> None:
    cols = set(ArticlePublicationArtifact.__table__.columns.keys())
    forbidden = {
        "destination_url", "shared_secret", "secret", "signature", "ip", "email",
        "updated_at",
    }
    assert cols.isdisjoint(forbidden)
