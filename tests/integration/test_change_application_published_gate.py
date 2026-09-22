"""公開済み記事への適用が既存ゲートを正しく通ること (C9.4)。

C9.1-C9.3 の初回本番適用は、ここで再現する不具合で **書き込み前に** 落ちた:

    EditorialRevisionStateError: failed gates: published_update_intent_ok

``ArticleEditorialRevisionService`` は ``published`` な記事の改訂に
``published_update_intent`` (空でない文字列) を要求するが、C9 はそれを渡して
いなかった。良い fail-closed だった一方、承認済みの変更が永久に適用できない。

このテストは **本物の** ``ArticleEditorialRevisionService`` を通す (ゲートを
mock しない)。差し替えるのは WordPress に触れる部分だけ。

pin する契約:

- 承認済み C9 request は、承認の事実から書き起こした意図でゲートを満たす。
- 意図は改訂行に永続化される (誰が何を承認したかが後から読める)。
- ゲート自体は fail-closed のまま -- 意図を渡さない一般の呼び出しは今も失敗する。
- PLAN は意図を作らない (書き込みを authorize しない)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.editorial_revision_canonical import compute_revision_content_hash
from app.exceptions import EditorialRevisionStateError
from app.models import (
    CR_APPLY_FAILED,
    CR_APPROVED,
    Article,
    ArticleEditorialRevision,
    ChangeApplication,
    ChangeRequest,
    SeoImprovementCandidate,
    SeoImprovementRun,
)
from app.models.enums import ArticleStatus
from app.services.article_editorial_revision_service import ArticleEditorialRevisionService
from app.services.change_application_service import (
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    ChangeApplicationService,
)
from app.services.change_request_service import ChangeRequestError, ChangeRequestService
from tests.support.draft_promotion_fixture import article_of, promoted_scenario

_BASE = "https://example.com"
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class _Settings:
    wordpress_base_url = _BASE
    wordpress_username = "user"
    wordpress_app_password = "pass"


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    """WordPress の GET だけを返す。POST/PUT が来たら失敗させる。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def get(self, url, **kwargs):
        return _FakeResponse(
            {"status": "publish", "modified_gmt": "2026-06-01T00:00:00", "content": {"raw": "raw"}}
        )

    def post(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("C9 must not write to WordPress directly")


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def wordpress_stubs(monkeypatch):
    """WordPress に触れる段だけを差し替える (改訂ゲートは本物のまま)。"""

    import httpx

    from app.services import (
        article_publication_artifact_persistence_service as persist_mod,
    )
    from app.services import article_publication_artifact_service as artifact_mod
    from app.services import article_publication_preparation_service as prepare_mod
    from app.services import wordpress_content_update_execution_service as execute_mod

    monkeypatch.setattr(httpx, "Client", _FakeHttp)

    class _Prepare:
        def __init__(self, *a, **k) -> None:
            pass

        def prepare(self, article_id):
            return {"article_id": article_id}

    class _Persist:
        def __init__(self, *a, **k) -> None:
            pass

        def persist(self, prepared):
            return type("artifact", (), {"id": 601, "artifact_hash": "h" * 64})()

    class _Artifact:
        def __init__(self, *a, **k) -> None:
            pass

        def approve_artifact(self, artifact_id, *, expected_artifact_hash):
            return None

    class _Execute:
        def __init__(self, *a, **k) -> None:
            pass

        def execute(self, **kwargs):
            return type("result", (), {"run_id": 701, "run_status": "succeeded"})()

    monkeypatch.setattr(prepare_mod, "ArticlePublicationPreparationService", _Prepare)
    monkeypatch.setattr(persist_mod, "ArticlePublicationArtifactPersistenceService", _Persist)
    monkeypatch.setattr(artifact_mod, "ArticlePublicationArtifactService", _Artifact)
    monkeypatch.setattr(execute_mod, "WordPressContentUpdateExecutionService", _Execute)


@pytest.fixture
def published_pair(session: Session):
    """本物の promotion を持つ公開済み記事と、公開済みのリンク先。"""

    source = promoted_scenario(session, suffix="src")
    target = promoted_scenario(session, suffix="dst")

    src = article_of(session, source.article_id)
    src.status = ArticleStatus.PUBLISHED.value
    src.published_url = f"{_BASE}/source-article/"
    src.wordpress_post_id = "68"
    dst = article_of(session, target.article_id)
    dst.status = ArticleStatus.PUBLISHED.value
    dst.published_url = f"{_BASE}/target-article/"
    dst.wordpress_post_id = "69"
    session.commit()

    run = SeoImprovementRun(
        policy_version="seo-policy-1",
        window_start=date(2026, 5, 1),
        window_end=date(2026, 5, 31),
        evaluated_article_count=2,
        candidate_count=1,
    )
    session.add(run)
    session.commit()
    candidate = SeoImprovementCandidate(
        seo_improvement_run_id=run.id,
        article_id=src.id,
        candidate_type="INTERNAL_LINK_OPPORTUNITY",
        reason_code="DEFERRED_PAIR",
        priority="low",
        evidence_strength="structural",
        suggested_action="内部リンクを 1 本足す",
        evidence_json={"target_article_id": dst.id, "relation": "deferred pair"},
        dedupe_key=f"{src.id}:INTERNAL_LINK_OPPORTUNITY:{dst.id}",
    )
    session.add(candidate)
    session.commit()
    return src, dst, candidate


@pytest.fixture
def approved(session: Session, published_pair):
    src, dst, candidate = published_pair
    service = ChangeRequestService(session)
    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)
    service.approve(request.id, proposal_hash=request.proposal_hash, now=_NOW)
    return src, dst, request


# -- the gate itself is unchanged ----------------------------------------------
def test_the_gate_still_fails_closed_for_a_caller_without_intent(
    session: Session, published_pair
) -> None:
    """一般の呼び出しは、いまも意図なしでは公開済み記事を改訂できない。"""

    src, _, _ = published_pair
    body = (src.body or "") + "\n\n追記\n"

    with pytest.raises(EditorialRevisionStateError) as excinfo:
        ArticleEditorialRevisionService(session).revise(
            src.id,
            body_markdown=body,
            meta_description=src.meta_description,
            expected_current_body_hash=compute_text_hash(src.body or ""),
            expected_current_meta_hash=compute_text_hash(src.meta_description or ""),
            expected_revision_content_hash=compute_revision_content_hash(
                article_id=src.id,
                body_markdown=body,
                meta_description=src.meta_description,
            ),
            revision_reason="意図なしの改訂",
        )

    assert "published_update_intent_ok" in str(excinfo.value)
    session.refresh(src)
    assert "追記" not in (src.body or "")


# -- the C9 path now satisfies it ----------------------------------------------
def test_an_approved_change_satisfies_the_published_update_intent(
    factory, approved, wordpress_stubs, session: Session
) -> None:
    src, _, request = approved

    outcome = ChangeApplicationService(factory, settings=_Settings()).apply(
        request.id, execute=True
    )

    assert outcome.outcome == OUTCOME_SUCCEEDED, outcome.error_message
    assert outcome.editorial_revision_id is not None

    with factory() as check:
        revision = check.get(ArticleEditorialRevision, outcome.editorial_revision_id)
        assert revision.article_status_at_revision == ArticleStatus.PUBLISHED.value
        # 意図は「誰が何を承認したか」として永続化される。
        assert request.proposal_hash in revision.published_update_intent
        assert str(request.id) in revision.published_update_intent
        assert check.get(ChangeRequest, request.id).status == "applied"


def test_plan_creates_no_revision_and_no_intent(
    factory, approved, wordpress_stubs, session: Session
) -> None:
    _, _, request = approved
    before = session.scalar(
        select(ArticleEditorialRevision.id).order_by(ArticleEditorialRevision.id.desc())
    )

    ChangeApplicationService(factory, settings=_Settings()).apply(request.id)

    after = session.scalar(
        select(ArticleEditorialRevision.id).order_by(ArticleEditorialRevision.id.desc())
    )
    assert after == before
    with factory() as check:
        assert check.scalars(select(ChangeApplication)).all() == []


# -- retry after a pre-write failure -------------------------------------------
def _fail_first_revision(monkeypatch) -> None:
    """最初の 1 回だけ改訂段で落とす (本番で起きた形を再現する)。

    ``monkeypatch.undo()`` は他の差し替えまで戻してしまうので、回数で切り替える。
    2 回目以降は **本物の** 改訂 service が走り、ゲートも本物のまま効く。
    """

    from app.services import article_editorial_revision_service as revision_mod

    real = revision_mod.ArticleEditorialRevisionService
    state = {"calls": 0}

    class _FailsOnce:
        def __init__(self, *args, **kwargs) -> None:
            self._real = real(*args, **kwargs)

        def revise(self, *args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise EditorialRevisionStateError("failed gates: published_update_intent_ok")
            return self._real.revise(*args, **kwargs)

    monkeypatch.setattr(revision_mod, "ArticleEditorialRevisionService", _FailsOnce)


def test_retry_after_a_pre_write_failure_is_append_only(
    factory, approved, wordpress_stubs, session: Session, monkeypatch
) -> None:
    """書き込み前に落ちた適用は、同じ承認のまま再試行でき、履歴は消えない。"""

    src, _, request = approved

    # 1) 改訂段で 1 回だけ落とす (= 本番で起きた形。WordPress には届いていない)。
    _fail_first_revision(monkeypatch)
    service = ChangeApplicationService(factory, settings=_Settings())
    first = service.apply(request.id, execute=True)

    assert first.outcome == OUTCOME_FAILED
    with factory() as check:
        assert check.get(ChangeRequest, request.id).status == CR_APPLY_FAILED

    # 2) 修正後に再開する -- 承認は作り直さない。
    with factory() as check:
        reopened = ChangeRequestService(check).reopen_for_retry(
            request.id, proposal_hash=request.proposal_hash
        )
        assert reopened.status == CR_APPROVED
        approvals = len(reopened.approvals)

    second = service.apply(request.id, execute=True)

    assert second.outcome == OUTCOME_SUCCEEDED, second.error_message
    with factory() as check:
        rows = check.scalars(select(ChangeApplication).order_by(ChangeApplication.id)).all()
        # 失敗した 1 回目は残り、再試行は 2 行目として積まれる。
        assert len(rows) == 2
        assert rows[0].id == first.application_id
        assert rows[0].outcome == OUTCOME_FAILED
        assert rows[0].error_category == "EditorialRevisionStateError"
        assert rows[1].outcome == OUTCOME_SUCCEEDED
        # 人の承認は 1 度きりのまま。
        assert len(check.get(ChangeRequest, request.id).approvals) == approvals


def test_retry_requires_the_current_proposal_hash(
    factory, approved, wordpress_stubs, session: Session, monkeypatch
) -> None:
    _, _, request = approved
    _fail_first_revision(monkeypatch)
    ChangeApplicationService(factory, settings=_Settings()).apply(request.id, execute=True)

    with factory() as check:
        with pytest.raises(ChangeRequestError, match="proposal hash mismatch"):
            ChangeRequestService(check).reopen_for_retry(request.id, proposal_hash="a" * 64)
        assert check.get(ChangeRequest, request.id).status == CR_APPLY_FAILED


def test_retry_is_refused_when_the_source_became_stale(
    factory, approved, wordpress_stubs, session: Session, monkeypatch
) -> None:
    """提案後に本文が変われば、再試行ではなく通常の stale 規則が効く。"""

    src, _, request = approved
    _fail_first_revision(monkeypatch)
    ChangeApplicationService(factory, settings=_Settings()).apply(request.id, execute=True)

    with factory() as check:
        article = check.get(Article, src.id)
        article.body = (article.body or "") + "\n\n人が別途編集した。\n"
        check.commit()
        with pytest.raises(ChangeRequestError, match="body changed"):
            ChangeRequestService(check).reopen_for_retry(
                request.id, proposal_hash=request.proposal_hash
            )


def test_a_request_that_never_failed_cannot_be_reopened(
    factory, approved, session: Session
) -> None:
    _, _, request = approved

    with pytest.raises(ChangeRequestError, match="only 'apply_failed'"):
        ChangeRequestService(session).reopen_for_retry(
            request.id, proposal_hash=request.proposal_hash
        )
