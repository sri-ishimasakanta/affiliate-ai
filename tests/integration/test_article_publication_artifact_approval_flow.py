"""D-D4/D-D4.1/D-D4.2 Human approval flow — reuses D-D1's existing
``ArticlePublicationArtifactService.approve_artifact`` set-once contract
unchanged (no new approval *semantics* are introduced). D-D4.1 hardened this
same method so that it owns rollback on any exception during its write path
(``approve`` -> ``commit``) — the caller no longer needs to know that a
rollback is required. D-D4.2 removed the post-commit ``session.refresh()``
call entirely (unnecessary given ``expire_on_commit=False`` and repository-level
``flush()`` already populating all returned fields) so that ``commit`` is the
final fallible DB operation — a successfully committed approval can no longer
be turned into an apparent failure by a later refresh round trip. These tests
confirm the full prepare -> persist -> approve pipeline, re-pin the existing
D-D1 set-once approval contract in the D-D4 context (wrong hash / missing
artifact / second approval), and prove both the D-D4.1 service-owned rollback
guarantee and the D-D4.2 no-post-commit-refresh guarantee.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.exceptions import ArticlePublicationArtifactError, EntityNotFoundError
from app.models import Article
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.services.article_publication_artifact_inspection_service import (
    ArticlePublicationArtifactInspectionService,
)
from app.services.article_publication_artifact_persistence_service import (
    ArticlePublicationArtifactPersistenceService,
)
from app.services.article_publication_artifact_service import (
    ArticlePublicationArtifactService,
)
from app.services.article_publication_preparation_service import (
    ArticlePublicationPreparationService,
)

_HREF = "https://official.example.test/tool-a"


def _settings() -> Settings:
    return Settings(wordpress_base_url="https://bizfluxlab.com")


def _seed_article(session: Session, *, slug: str = "p1") -> Article:
    art = Article(title="t", slug=slug, keyword_id=None, body=f"[tool]({_HREF})\n")
    session.add(art)
    session.commit()
    return art


def _persisted_artifact(session: Session):
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
    return art, artifact


# ==================== full pipeline ==========================================
def test_full_prepare_persist_inspect_approve_pipeline(session: Session) -> None:
    art, artifact = _persisted_artifact(session)
    assert artifact.approved_at is None

    inspection_before = ArticlePublicationArtifactInspectionService(session).inspect(artifact.id)
    assert inspection_before.approved is False
    assert inspection_before.artifact_hash_valid is True

    approve_svc = ArticlePublicationArtifactService(session)
    approved = approve_svc.approve_artifact(
        artifact.id, expected_artifact_hash=artifact.artifact_hash
    )
    assert approved.approved_at is not None
    assert approved.approved_artifact_hash == artifact.artifact_hash

    inspection_after = ArticlePublicationArtifactInspectionService(session).inspect(artifact.id)
    assert inspection_after.approved is True
    assert inspection_after.approved_at is not None
    assert inspection_after.approved_artifact_hash == artifact.artifact_hash


# ==================== §12/§14: approval requires id + exact hash ============
def test_approve_requires_exact_expected_hash(session: Session) -> None:
    _art, artifact = _persisted_artifact(session)
    approve_svc = ArticlePublicationArtifactService(session)
    with pytest.raises(ArticlePublicationArtifactError, match="does not match"):
        approve_svc.approve_artifact(artifact.id, expected_artifact_hash="f" * 64)

    refreshed = approve_svc.get(artifact.id)
    assert refreshed.approved_at is None
    assert refreshed.approved_artifact_hash is None


def test_approve_missing_artifact_raises_not_found(session: Session) -> None:
    approve_svc = ArticlePublicationArtifactService(session)
    with pytest.raises(EntityNotFoundError):
        approve_svc.approve_artifact(999999, expected_artifact_hash="a" * 64)


# ==================== §13: second-approval behavior (existing D-D1 contract) =
def test_second_approval_rejected_even_with_same_hash(session: Session) -> None:
    """既存 D-D1 の set-once 契約: 同じ artifact_hash での再承認リクエストも
    含め、2 回目の承認は常に拒否される (silently 再定義しない)。"""

    _art, artifact = _persisted_artifact(session)
    approve_svc = ArticlePublicationArtifactService(session)
    approve_svc.approve_artifact(artifact.id, expected_artifact_hash=artifact.artifact_hash)

    with pytest.raises(ArticlePublicationArtifactError, match="already approved"):
        approve_svc.approve_artifact(artifact.id, expected_artifact_hash=artifact.artifact_hash)

    refreshed = approve_svc.get(artifact.id)
    assert refreshed.approved_artifact_hash == artifact.artifact_hash  # 1 回目の値のまま不変


# ==================== D-D4.1: service-owned rollback on commit failure ======
def test_approve_artifact_owns_rollback_on_commit_failure(session: Session) -> None:
    """D-D4.1 hardening: ``approve_artifact`` 自身が commit 失敗時に rollback
    する -- 呼び出し側は rollback を一切呼ばない。"""

    _art, artifact = _persisted_artifact(session)
    approve_svc = ArticlePublicationArtifactService(session)

    original_rollback = session.rollback
    rollback_calls = {"count": 0}

    def _failing_commit():
        raise RuntimeError("boom-during-approval-commit")

    def _tracking_rollback():
        rollback_calls["count"] += 1
        return original_rollback()

    with (
        patch.object(session, "commit", side_effect=_failing_commit),
        patch.object(session, "rollback", side_effect=_tracking_rollback),
    ):
        with pytest.raises(RuntimeError, match="boom-during-approval-commit"):
            approve_svc.approve_artifact(
                artifact.id, expected_artifact_hash=artifact.artifact_hash
            )

    # D/§7: approve_artifact 自身が exactly 1 回 rollback を呼んだこと -- テスト
    # コード側は一切 rollback を呼んでいない。
    assert rollback_calls["count"] == 1

    # E/G: 呼び出し側が rollback しなくても、durable な状態は unapproved のまま。
    refreshed = approve_svc.get(artifact.id)
    assert refreshed.approved_at is None
    assert refreshed.approved_artifact_hash is None

    # F: 同じ session がそのまま読み取りに使える (壊れていない、poison されていない)。
    same_session_read = session.get(type(artifact), artifact.id)
    assert same_session_read is not None
    assert same_session_read.approved_at is None

    # §8: with ブロックを抜けて commit/rollback は元の実装に戻る。正規の承認を
    # 同じ artifact に対してリトライすると、今度は 1 回だけ成功する。
    approved = approve_svc.approve_artifact(
        artifact.id, expected_artifact_hash=artifact.artifact_hash
    )
    assert approved.approved_at is not None
    assert approved.approved_artifact_hash == artifact.artifact_hash

    # 失敗した transaction が承認ライフサイクルを poison していないこと -- 2 回目の
    # 承認は既存の set-once 契約によりちゃんと拒否される。
    with pytest.raises(ArticlePublicationArtifactError, match="already approved"):
        approve_svc.approve_artifact(
            artifact.id, expected_artifact_hash=artifact.artifact_hash
        )


# ==================== persistence-repository shape (§10/§11) ================
def test_repository_get_by_hash_is_the_reused_lookup(session: Session) -> None:
    _art, artifact = _persisted_artifact(session)
    repo = ArticlePublicationArtifactRepository(session)
    found = repo.get_by_hash(artifact.artifact_hash)
    assert found is not None
    assert found.id == artifact.id


def test_repository_has_no_generic_update_or_delete(session: Session) -> None:
    repo = ArticlePublicationArtifactRepository(session)
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")


# ==================== D-D4.2: no mandatory post-commit refresh ==============
def test_approve_artifact_never_calls_session_refresh(session: Session, engine) -> None:
    """D-D4.2: ``commit`` が approve_artifact の最後の fallible DB 操作でなければ
    ならない -- ``session.refresh`` を呼んでいないことをソースの grep ではなく
    実際の挙動として pin する。``refresh`` が万一呼ばれたら raise するようにして、
    それでも操作が成功する (= 呼ばれていない) ことを証明する。加えて、完全に
    独立した別 session からの再読み込みでも同じ値が durable に見えることを
    確認する (refresh なしで commit だけが正しく DB に反映されている証拠)。"""

    _art, artifact = _persisted_artifact(session)
    approve_svc = ArticlePublicationArtifactService(session)

    def _refresh_should_not_be_called(*_args, **_kwargs):
        raise AssertionError("approve_artifact must not call session.refresh()")

    with patch.object(session, "refresh", side_effect=_refresh_should_not_be_called):
        approved = approve_svc.approve_artifact(
            artifact.id, expected_artifact_hash=artifact.artifact_hash
        )

    # commit だけで成功し、呼び出し側に正しく populate された値が返ってくること。
    assert approved.id == artifact.id
    assert approved.approved_at is not None
    assert approved.approved_artifact_hash == artifact.artifact_hash

    # 完全に独立した別 session (同じ engine) から読み直しても同じ durable な値。
    from sqlalchemy.orm import sessionmaker

    independent_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        fresh = ArticlePublicationArtifactRepository(independent_session).get_by_id(artifact.id)
        assert fresh.approved_at is not None
        assert fresh.approved_artifact_hash == artifact.artifact_hash
    finally:
        independent_session.close()
