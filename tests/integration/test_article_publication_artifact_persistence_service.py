"""ArticlePublicationArtifactPersistenceService — D-D4/D-D4.2/D-D4.3/D-D4.4
persist orchestration: pre-persist verification / canonical drift guard /
renderer-schema guard / artifact idempotency / integrity-drift rejection /
rollback safety / no mandatory post-commit refresh (D-D4.2) / no post-commit
validation on fresh insert (D-D4.3) / no existing-lookup TOCTOU window
(D-D4.4 — persist() now performs exactly one ``get_by_hash`` call, via
``ArticlePublicationArtifactService.create_or_get_artifact``, instead of an
outer pre-check followed by ``create_artifact``'s own inner lookup).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.exceptions import ArticlePublicationArtifactError, EntityNotFoundError
from app.models import Article, ArticlePublicationArtifact
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.services.article_publication_artifact_persistence_service import (
    ArticlePublicationArtifactPersistenceService,
)
from app.services.article_publication_artifact_service import (
    ArticlePublicationArtifactService,
)
from app.services.article_publication_preparation_service import (
    ArticlePublicationPreparationService,
    PreparedPublicationResult,
)

_HREF = "https://official.example.test/tool-a"


def _settings() -> Settings:
    return Settings(wordpress_base_url="https://bizfluxlab.com")


def _seed_article(session: Session, *, slug: str = "p1", body: str | None = None) -> Article:
    art = Article(title="t", slug=slug, keyword_id=None, body=body or f"[tool]({_HREF})\n")
    session.add(art)
    session.commit()
    return art


def _prepare(session: Session, article: Article) -> PreparedPublicationResult:
    return ArticlePublicationPreparationService(session, settings=_settings()).prepare(article.id)


def _persist_svc(session: Session) -> ArticlePublicationArtifactPersistenceService:
    return ArticlePublicationArtifactPersistenceService(session)


def _count_artifacts(session: Session) -> int:
    return len(session.execute(select(ArticlePublicationArtifact)).scalars().all())


# ==================== happy path / zero-substitution (§24) ==================
def test_persist_zero_substitution_artifact(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    assert prepared.substitution_count == 0

    artifact = _persist_svc(session).persist(prepared)
    assert artifact.substitution_count == 0
    assert artifact.substitution_manifest_json == "[]"
    assert artifact.tracked_html == artifact.tracked_html  # sanity
    assert artifact.approved_at is None
    assert artifact.approved_artifact_hash is None


def test_persist_sets_exact_frozen_fields(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    artifact = _persist_svc(session).persist(prepared)

    assert artifact.article_id == prepared.article_id
    assert artifact.canonical_body_hash == prepared.canonical_body_hash
    assert artifact.renderer_version == prepared.renderer_version
    assert artifact.artifact_schema_version == prepared.artifact_schema_version
    assert artifact.substitution_manifest_json == prepared.substitution_manifest_json
    assert artifact.artifact_hash == prepared.artifact_hash
    assert artifact.tracked_html == prepared.tracked_html
    assert artifact.tracked_html_hash == prepared.tracked_html_hash
    assert artifact.substitution_count == prepared.substitution_count
    assert artifact.generated_at is not None
    assert artifact.created_at is not None
    assert artifact.approved_at is None
    assert artifact.approved_artifact_hash is None


# ==================== §7: canonical drift guard =============================
def test_persist_rejects_stale_article_body(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)

    art.body = "a completely different body\n"
    session.commit()

    with pytest.raises(ArticlePublicationArtifactError, match="stale"):
        _persist_svc(session).persist(prepared)
    assert _count_artifacts(session) == 0


# ==================== §8: renderer/schema guard ==============================
def test_persist_rejects_renderer_version_mismatch(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    tampered = dataclasses.replace(prepared, renderer_version="wordpress_html_v99")

    with pytest.raises(ArticlePublicationArtifactError, match="renderer_version"):
        _persist_svc(session).persist(tampered)
    assert _count_artifacts(session) == 0


def test_persist_rejects_artifact_schema_version_mismatch(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    tampered = dataclasses.replace(prepared, artifact_schema_version=99)

    with pytest.raises(ArticlePublicationArtifactError, match="artifact_schema_version"):
        _persist_svc(session).persist(tampered)
    assert _count_artifacts(session) == 0


def test_persist_rejects_occurrence_schema_version_mismatch(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    tampered = dataclasses.replace(prepared, occurrence_schema_version=99)

    with pytest.raises(ArticlePublicationArtifactError, match="occurrence_schema_version"):
        _persist_svc(session).persist(tampered)
    assert _count_artifacts(session) == 0


# ==================== §6: self-consistency re-verification ==================
def test_persist_rejects_tampered_tracked_html(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    tampered = dataclasses.replace(prepared, tracked_html=prepared.tracked_html + "<!-- x -->")

    with pytest.raises(ArticlePublicationArtifactError):
        _persist_svc(session).persist(tampered)
    assert _count_artifacts(session) == 0


def test_persist_rejects_tampered_tracked_html_hash(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    tampered = dataclasses.replace(prepared, tracked_html_hash="f" * 64)

    with pytest.raises(
        ArticlePublicationArtifactError, match="tracked_html_hash"
    ):
        _persist_svc(session).persist(tampered)
    assert _count_artifacts(session) == 0


def test_persist_rejects_tampered_artifact_hash(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    tampered = dataclasses.replace(prepared, artifact_hash="e" * 64)

    with pytest.raises(ArticlePublicationArtifactError, match="artifact_hash"):
        _persist_svc(session).persist(tampered)
    assert _count_artifacts(session) == 0


def test_persist_rejects_tampered_manifest_json(session: Session) -> None:
    art = _seed_article(
        session,
        body=f"[tool]({_HREF})\n",
    )
    prepared = _prepare(session, art)
    tampered = dataclasses.replace(
        prepared, substitution_manifest_json='[{"tampered": true}]'
    )

    with pytest.raises(
        ArticlePublicationArtifactError, match="substitution_manifest_json"
    ):
        _persist_svc(session).persist(tampered)
    assert _count_artifacts(session) == 0


def test_persist_rejects_wrong_substitution_count(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    tampered = dataclasses.replace(prepared, substitution_count=99)

    with pytest.raises(
        ArticlePublicationArtifactError, match="substitution_count"
    ):
        _persist_svc(session).persist(tampered)
    assert _count_artifacts(session) == 0


# ==================== §9: idempotency / integrity drift ======================
def test_persist_is_idempotent_for_exact_same_artifact(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    first = _persist_svc(session).persist(prepared)

    # D-D4.3 §13: 2 回目の persist (既存 identical row) は commit を一切発行しない
    # -- read-only idempotency short-circuit であることを pin する。
    def _commit_should_not_be_called():
        raise AssertionError(
            "persist() must not call session.commit() for an identical existing artifact"
        )

    with patch.object(session, "commit", side_effect=_commit_should_not_be_called):
        second = _persist_svc(session).persist(prepared)

    assert first.id == second.id
    assert _count_artifacts(session) == 1
    assert second.approved_at is None  # 承認状態も変化しない
    assert second.tracked_html == prepared.tracked_html  # 凍結フィールドも無変更


def test_persist_rejects_duplicate_hash_with_inconsistent_frozen_content(
    session: Session,
) -> None:
    """D-D4.3 §12: integrity drift は insert より前に確定する -- commit は
    一切呼ばれず、artifact 件数/既存行/session の健全性のいずれも影響を受けない。"""

    art = _seed_article(session)
    prepared = _prepare(session, art)

    # DB に「同じ artifact_hash だが tracked_html_hash が異なる」forged 行を
    # 直接 insert する (persistence service を経由しない -- ハッシュ衝突ではなく
    # 実装バグによる drift をシミュレートする)。
    repo = ArticlePublicationArtifactRepository(session)
    forged = repo.add(
        article_id=prepared.article_id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        artifact_schema_version=prepared.artifact_schema_version,
        substitution_manifest_json=prepared.substitution_manifest_json,
        artifact_hash=prepared.artifact_hash,
        tracked_html=prepared.tracked_html,
        tracked_html_hash="9" * 64,  # 不整合な値
        substitution_count=prepared.substitution_count,
        approved_at=None,
        approved_artifact_hash=None,
        generated_at=datetime.now(UTC),
    )
    session.commit()
    forged_id = forged.id

    def _commit_should_not_be_called():
        raise AssertionError(
            "persist() must not call session.commit() on the integrity-drift path"
        )

    with patch.object(session, "commit", side_effect=_commit_should_not_be_called):
        with pytest.raises(ArticlePublicationArtifactError, match="integrity drift"):
            _persist_svc(session).persist(prepared)

    # artifact 件数は forged 行 1 件のみのまま (新規 insert が一切起きていない)。
    assert _count_artifacts(session) == 1
    unchanged = ArticlePublicationArtifactRepository(session).get_by_id(forged_id)
    assert unchanged.tracked_html_hash == "9" * 64  # 既存行も無変更のまま

    # session はそのまま読み取りに使える (壊れていない)。
    again = ArticlePublicationArtifactRepository(session).get_by_hash(prepared.artifact_hash)
    assert again is not None
    assert again.id == forged_id


# ==================== §21: persistence transaction / rollback ===============
def test_persist_rolls_back_on_db_failure(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)

    with patch.object(
        ArticlePublicationArtifactRepository, "add", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError, match="boom"):
            _persist_svc(session).persist(prepared)

    session.rollback()
    assert _count_artifacts(session) == 0


# ==================== §15: no approval inheritance ===========================
def test_new_artifact_does_not_inherit_old_approval(session: Session) -> None:
    art = _seed_article(session)
    prepared1 = _prepare(session, art)
    artifact1 = _persist_svc(session).persist(prepared1)
    ArticlePublicationArtifactService(session).approve_artifact(
        artifact1.id, expected_artifact_hash=artifact1.artifact_hash
    )
    assert artifact1.approved_at is not None

    art.body = f"different body [tool]({_HREF}) more\n"
    session.commit()
    prepared2 = _prepare(session, art)
    assert prepared2.artifact_hash != prepared1.artifact_hash

    artifact2 = _persist_svc(session).persist(prepared2)
    assert artifact2.id != artifact1.id
    assert artifact2.approved_at is None
    assert artifact2.approved_artifact_hash is None


def test_missing_article_raises_not_found(session: Session) -> None:
    art = _seed_article(session)
    prepared = _prepare(session, art)
    tampered = dataclasses.replace(prepared, article_id=999999)
    with pytest.raises(EntityNotFoundError):
        _persist_svc(session).persist(tampered)


# ==================== D-D4.2: no mandatory post-commit refresh ==============
def test_persist_never_calls_session_refresh(session: Session, engine) -> None:
    """D-D4.2: ``create_artifact`` (persist() が呼ぶ) の ``commit`` が最後の
    fallible DB 操作でなければならない -- ``session.refresh`` を呼んでいない
    ことを実際の挙動として pin する。呼ばれたら raise するようにしても
    persist() が成功することで証明する。"""

    art = _seed_article(session)
    prepared = _prepare(session, art)

    def _refresh_should_not_be_called(*_args, **_kwargs):
        raise AssertionError("persist()/create_artifact() must not call session.refresh()")

    with patch.object(session, "refresh", side_effect=_refresh_should_not_be_called):
        artifact = _persist_svc(session).persist(prepared)

    assert artifact.id is not None
    assert artifact.artifact_hash == prepared.artifact_hash
    assert artifact.created_at is not None

    # 完全に独立した別 session (同じ engine) から読み直しても同じ durable な値。
    from sqlalchemy.orm import sessionmaker

    independent_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        fresh = ArticlePublicationArtifactRepository(independent_session).get_by_id(artifact.id)
        assert fresh is not None
        assert fresh.artifact_hash == prepared.artifact_hash
        assert fresh.created_at is not None
    finally:
        independent_session.close()


# ==================== D-D4.3: no mandatory post-commit validation ===========
def test_persist_fresh_insert_never_calls_post_commit_consistency_check(
    session: Session, engine
) -> None:
    """D-D4.3 §11: 新規 insert 経路では commit 成功後に追加の検証/DB read が
    一切走らないこと -- ``_require_consistent_persisted`` (D-D4 で post-commit
    に走っていた, まさに今回問題になった helper) を常に raise するよう patch し、
    それでも新規 persist が成功することで「呼ばれていない」ことを実際の挙動
    として pin する (ソースの grep だけに頼らない)。"""

    art = _seed_article(session)
    prepared = _prepare(session, art)

    with patch.object(
        ArticlePublicationArtifactPersistenceService,
        "_require_consistent_persisted",
        side_effect=AssertionError(
            "fresh insert must not call _require_consistent_persisted"
        ),
    ):
        artifact = _persist_svc(session).persist(prepared)

    assert artifact.artifact_hash == prepared.artifact_hash
    assert _count_artifacts(session) == 1

    # 独立した別 session から読み直しても、commit だけで durable に反映されている。
    from sqlalchemy.orm import sessionmaker

    independent_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        fresh = ArticlePublicationArtifactRepository(independent_session).get_by_id(
            artifact.id
        )
        assert fresh is not None
        assert fresh.artifact_hash == prepared.artifact_hash
    finally:
        independent_session.close()


def test_persist_existing_lookup_happens_before_any_insert(session: Session) -> None:
    """D-D4.3 §4-5: 既存行 lookup + 整合性チェックが insert より前に確定する
    ことを、``repository.add`` を raise するよう patch して pin する -- 既存
    identical row を返す経路では ``add`` (insert) が一切呼ばれないはず。"""

    art = _seed_article(session)
    prepared = _prepare(session, art)
    first = _persist_svc(session).persist(prepared)

    with patch.object(
        ArticlePublicationArtifactRepository,
        "add",
        side_effect=AssertionError(
            "persist() must not call repository.add() when an identical "
            "artifact_hash already exists"
        ),
    ):
        second = _persist_svc(session).persist(prepared)

    assert second.id == first.id
    assert _count_artifacts(session) == 1


# ==================== D-D4.4: TOCTOU race hardening ==========================
def test_persist_calls_get_by_hash_exactly_once(session: Session) -> None:
    """D-D4.4: outer lookup (persist) と inner lookup (create_artifact) が
    別々に存在していた D-D4.3 の 2 段階構造を廃止し、
    ``create_or_get_artifact`` の中の 1 回だけに統合したことを直接 pin する
    -- TOCTOU window がそもそも構造的に存在しないことの証明。"""

    art = _seed_article(session)
    prepared = _prepare(session, art)

    original_get_by_hash = ArticlePublicationArtifactRepository.get_by_hash
    call_count = {"n": 0}

    def _counting_get_by_hash(self, artifact_hash):
        call_count["n"] += 1
        return original_get_by_hash(self, artifact_hash)

    with patch.object(
        ArticlePublicationArtifactRepository, "get_by_hash", _counting_get_by_hash
    ):
        _persist_svc(session).persist(prepared)

    assert call_count["n"] == 1


def test_persist_race_existing_row_becomes_visible_and_is_identical(
    session: Session,
) -> None:
    """D-D4.4 §7 Case 1: lookup の時点で (他の書き込みにより) 既に存在する行が
    見え、その行の凍結フィールドが候補と完全一致する場合 -- 安全に既存行を
    返し、新規行を作らない。"""

    art = _seed_article(session)
    prepared = _prepare(session, art)

    # 事前に「他の writer が作った」ことにする consistent な既存行。
    repo = ArticlePublicationArtifactRepository(session)
    pre_existing = repo.add(
        article_id=prepared.article_id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        artifact_schema_version=prepared.artifact_schema_version,
        substitution_manifest_json=prepared.substitution_manifest_json,
        artifact_hash=prepared.artifact_hash,
        tracked_html=prepared.tracked_html,
        tracked_html_hash=prepared.tracked_html_hash,
        substitution_count=prepared.substitution_count,
        approved_at=None,
        approved_artifact_hash=None,
        generated_at=datetime.now(UTC),
    )
    session.commit()

    def _commit_should_not_be_called():
        raise AssertionError(
            "persist() must not commit when the single lookup already finds "
            "a consistent existing row"
        )

    with patch.object(session, "commit", side_effect=_commit_should_not_be_called):
        result = _persist_svc(session).persist(prepared)

    assert result.id == pre_existing.id
    assert _count_artifacts(session) == 1


def test_persist_race_existing_row_becomes_visible_but_inconsistent(
    session: Session,
) -> None:
    """D-D4.4 §7 Case 2: lookup の時点で見えた既存行の凍結フィールドが候補と
    食い違う場合 -- integrity drift として fail closed し、insert/commit を
    一切行わず、既存行も無変更のまま、session は読み取りに使える。"""

    art = _seed_article(session)
    prepared = _prepare(session, art)

    repo = ArticlePublicationArtifactRepository(session)
    forged = repo.add(
        article_id=prepared.article_id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        artifact_schema_version=prepared.artifact_schema_version,
        substitution_manifest_json=prepared.substitution_manifest_json,
        artifact_hash=prepared.artifact_hash,
        tracked_html=prepared.tracked_html,
        tracked_html_hash="deadbeef" * 8,  # 不整合な値
        substitution_count=prepared.substitution_count,
        approved_at=None,
        approved_artifact_hash=None,
        generated_at=datetime.now(UTC),
    )
    session.commit()
    forged_id = forged.id

    def _commit_should_not_be_called():
        raise AssertionError(
            "persist() must not commit on the integrity-drift path"
        )

    with patch.object(session, "commit", side_effect=_commit_should_not_be_called):
        with pytest.raises(ArticlePublicationArtifactError, match="integrity drift"):
            _persist_svc(session).persist(prepared)

    assert _count_artifacts(session) == 1
    unchanged = repo.get_by_id(forged_id)
    assert unchanged.tracked_html_hash == "deadbeef" * 8
    assert unchanged.approved_at is None

    # session はそのまま読み取りに使える。
    still_readable = repo.get_by_hash(prepared.artifact_hash)
    assert still_readable is not None
    assert still_readable.id == forged_id


def test_create_or_get_artifact_returns_provenance(session: Session) -> None:
    """D-D4.4: ``create_or_get_artifact`` の ``created`` provenance を直接確認する
    -- 新規なら True、既存の再利用なら False。"""

    from app.services.article_publication_artifact_service import (
        ArticlePublicationArtifactService,
    )

    art = _seed_article(session)
    prepared = _prepare(session, art)
    svc = ArticlePublicationArtifactService(session)

    first = svc.create_or_get_artifact(
        article_id=prepared.article_id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        manifest_entries=prepared.substitution_manifest,
        tracked_html=prepared.tracked_html,
        artifact_schema_version=prepared.artifact_schema_version,
    )
    assert first.created is True

    second = svc.create_or_get_artifact(
        article_id=prepared.article_id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        manifest_entries=prepared.substitution_manifest,
        tracked_html=prepared.tracked_html,
        artifact_schema_version=prepared.artifact_schema_version,
    )
    assert second.created is False
    assert second.artifact.id == first.artifact.id


def test_create_artifact_dedup_contract_unchanged_by_provenance_refactor(
    session: Session,
) -> None:
    """D-D4.4: ``create_artifact`` 自身の既存 D-D1 契約 (identity は
    artifact_hash のみ、tracked_html は含まない) は provenance リファクタで
    一切変わっていないことを re-confirm する。"""

    from app.services.article_publication_artifact_service import (
        ArticlePublicationArtifactService,
    )

    art = _seed_article(session)
    prepared = _prepare(session, art)
    svc = ArticlePublicationArtifactService(session)

    a = svc.create_artifact(
        article_id=prepared.article_id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        manifest_entries=prepared.substitution_manifest,
        tracked_html="<p>v1</p>",
        artifact_schema_version=prepared.artifact_schema_version,
    )
    b = svc.create_artifact(
        article_id=prepared.article_id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        manifest_entries=prepared.substitution_manifest,
        tracked_html="<p>v2 different</p>",
        artifact_schema_version=prepared.artifact_schema_version,
    )
    assert a.id == b.id
    assert a.tracked_html == "<p>v1</p>"


def test_persist_unique_constraint_race_after_lookup_rolls_back_cleanly(
    session: Session,
) -> None:
    """D-D4.4 §10: 狭い race -- ``get_by_hash`` が None を返した直後に、別の
    writer が同じ ``artifact_hash`` を先に commit してしまい、こちらの
    INSERT が UNIQUE 制約に当たるケース。auto-retry はせず、rollback して
    raise するだけでよい -- ただし durable な半端 row を残さず、session は
    そのまま使えて、呼び出し側には「成功した」と伝えないこと。"""

    art = _seed_article(session)
    prepared = _prepare(session, art)

    # 「lookup の時点では見えなかったが、実際には既に別 writer が commit 済み」
    # という状況を再現するため、get_by_hash だけ None を返すよう偽装しつつ、
    # DB には実際に同じ artifact_hash の行を先に commit しておく。
    repo = ArticlePublicationArtifactRepository(session)
    repo.add(
        article_id=prepared.article_id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        artifact_schema_version=prepared.artifact_schema_version,
        substitution_manifest_json=prepared.substitution_manifest_json,
        artifact_hash=prepared.artifact_hash,
        tracked_html=prepared.tracked_html,
        tracked_html_hash=prepared.tracked_html_hash,
        substitution_count=prepared.substitution_count,
        approved_at=None,
        approved_artifact_hash=None,
        generated_at=datetime.now(UTC),
    )
    session.commit()
    assert _count_artifacts(session) == 1

    with patch.object(
        ArticlePublicationArtifactRepository, "get_by_hash", return_value=None
    ):
        with pytest.raises(Exception):  # noqa: B017 - IntegrityError (backend-specific)
            _persist_svc(session).persist(prepared)

    # persist() 自身が rollback 済み (呼び出し側は何もしていない) -- durable な
    # 半端行は残らず、元の 1 件のみが残る。
    assert _count_artifacts(session) == 1

    # session はそのまま読み取りに使える。
    still_readable = ArticlePublicationArtifactRepository(session).get_by_hash(
        prepared.artifact_hash
    )
    assert still_readable is not None
