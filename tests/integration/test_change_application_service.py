"""ChangeApplicationService の統合テスト (C9.2)。

pin する契約:

- 既定は PLAN。``--execute`` 相当を指定しない限り **WordPress に触れない**。
- 承認されていない提案は適用しない。
- 承認が **いまの** ``proposal_hash`` に対するものでなければ適用しない。
- 記事が提案後に変わっていれば適用しない (古い hash を信じない)。
- 同じリンクが既にあれば適用しない。
- 適用は既存の managed 経路 (編集改訂 -> artifact -> WordPress 更新 -> 照合) を
  そのまま使う。新しい更新スタックは作らない。
- 適用履歴は append-only で、巻き戻し用の変更前本文を必ず持つ。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    CR_APPLIED,
    Article,
    ChangeApplication,
    ChangeRequest,
    SeoImprovementCandidate,
    SeoImprovementRun,
)
from app.services.change_application_service import (
    OUTCOME_BLOCKED,
    OUTCOME_PLANNED,
    OUTCOME_SUCCEEDED,
    ChangeApplicationService,
)
from app.services.change_request_service import ChangeRequestService

_BASE = "https://example.com"
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

_SOURCE_BODY = """記事の冒頭。

導入の段落です。ここでは手順を整理します。

## 事前準備

参考: [公式](https://official.example.jp/docs)
"""


class _Settings:
    wordpress_base_url = _BASE
    wordpress_username = "user"
    wordpress_app_password = "pass"


def _article(session: Session, article_id: int, slug: str, *, body: str) -> Article:
    article = Article(
        id=article_id,
        title=f"記事{article_id}",
        slug=slug,
        body=body,
        status="published",
        published_url=f"{_BASE}/{slug}/",
        published_at=_NOW - timedelta(days=30),
        article_type="informational",
        monetization_mode="supporting",
        wordpress_post_id=str(1000 + article_id),
    )
    session.add(article)
    session.commit()
    return article


def _candidate(session: Session, *, article_id: int, target_article_id: int):
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
        article_id=article_id,
        candidate_type="INTERNAL_LINK_OPPORTUNITY",
        reason_code="DEFERRED_PAIR",
        priority="low",
        evidence_strength="structural",
        suggested_action="内部リンクを 1 本足す",
        evidence_json={"target_article_id": target_article_id, "relation": "deferred pair"},
        dedupe_key="internal-link:18->17",
    )
    session.add(candidate)
    session.commit()
    return candidate


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def approved(session: Session):
    """承認済みの request を 1 つ用意する。"""

    source = _article(session, 18, "rpa-implementation", body=_SOURCE_BODY)
    target = _article(session, 17, "rpa-comparison", body="# 比較\n\n本文\n")
    candidate = _candidate(session, article_id=source.id, target_article_id=target.id)
    service = ChangeRequestService(session)
    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)
    service.approve(request.id, proposal_hash=request.proposal_hash, now=_NOW)
    return source, target, request


@pytest.fixture
def unapproved(session: Session):
    source = _article(session, 18, "rpa-implementation", body=_SOURCE_BODY)
    target = _article(session, 17, "rpa-comparison", body="# 比較\n\n本文\n")
    candidate = _candidate(session, article_id=source.id, target_article_id=target.id)
    request = ChangeRequestService(session).propose_from_seo_candidate(
        candidate_id=candidate.id, now=_NOW
    )
    return source, target, request


def _service(factory, monkeypatch=None) -> ChangeApplicationService:
    return ChangeApplicationService(factory, settings=_Settings())


def test_plan_is_read_only_and_touches_no_http(factory, approved, monkeypatch) -> None:
    import httpx

    def _boom(*args, **kwargs):  # pragma: no cover - 呼ばれたら失敗させる
        raise AssertionError("PLAN must not perform any HTTP request")

    monkeypatch.setattr(httpx, "Client", _boom)
    _, _, request = approved

    outcome = _service(factory).plan(request.id)

    assert outcome.executed is False
    assert outcome.outcome == OUTCOME_PLANNED
    assert outcome.blocked_reasons == []


def test_apply_without_execute_is_plan_only(factory, approved, monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(
        httpx, "Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no writes"))
    )
    _, _, request = approved

    outcome = _service(factory).apply(request.id)

    assert outcome.executed is False
    with factory() as session:
        assert session.scalars(select(ChangeApplication)).all() == []


def test_unapproved_request_is_blocked(factory, unapproved) -> None:
    _, _, request = unapproved

    outcome = _service(factory).plan(request.id)

    assert outcome.outcome == OUTCOME_BLOCKED
    assert any("only 'approved'" in reason for reason in outcome.blocked_reasons)
    assert any("no approval exists" in reason for reason in outcome.blocked_reasons)


def test_a_changed_source_body_blocks_apply(factory, approved, session: Session) -> None:
    source, _, request = approved
    source.body = source.body.replace("手順を整理します", "手順と費用を整理します")
    session.commit()

    outcome = _service(factory).plan(request.id)

    assert outcome.outcome == OUTCOME_BLOCKED
    assert any("body changed" in reason for reason in outcome.blocked_reasons)


def test_a_link_added_elsewhere_blocks_apply(factory, approved, session: Session) -> None:
    source, target, request = approved
    source.body = f"{source.body}\n[手動]({target.published_url})\n"
    session.commit()

    outcome = _service(factory).plan(request.id)

    assert outcome.outcome == OUTCOME_BLOCKED
    assert any("already links" in reason for reason in outcome.blocked_reasons)


def test_an_approval_for_another_proposal_blocks_apply(factory, approved, session: Session) -> None:
    _, _, request = approved
    # 承認後に提案が別内容へ差し替わった状況を作る。
    row = session.get(ChangeRequest, request.id)
    row.proposal_hash = "f" * 64
    session.commit()

    outcome = _service(factory).plan(request.id)

    assert outcome.outcome == OUTCOME_BLOCKED
    assert any("different proposal hash" in reason for reason in outcome.blocked_reasons)


def test_a_missing_candidate_warns_but_does_not_block(factory, approved, session: Session) -> None:
    _, _, request = approved
    session.add(
        SeoImprovementRun(
            policy_version="seo-policy-1",
            window_start=date(2026, 6, 1),
            window_end=date(2026, 6, 30),
            evaluated_article_count=2,
            candidate_count=0,
        )
    )
    session.commit()

    outcome = _service(factory).plan(request.id)

    assert outcome.blocked_reasons == []
    assert any("no longer present" in warning for warning in outcome.warnings)


# -- execute -------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    """WordPress の GET だけを返す (書き込みは managed 経路の担当)。"""

    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def get(self, url, **kwargs):
        _FakeHttp.calls.append(url)
        return _FakeResponse(
            {"status": "publish", "modified_gmt": "2026-06-01T00:00:00", "content": {"raw": "raw"}}
        )

    def post(self, *args, **kwargs):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("C9 must not write to WordPress directly")

    def put(self, *args, **kwargs):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("C9 must not write to WordPress directly")


@pytest.fixture
def managed_path(monkeypatch):
    """managed 経路の各段を差し替え、呼ばれた順序を記録する。"""

    import httpx

    from app.services import (
        article_editorial_revision_service as revision_mod,
    )
    from app.services import (
        article_publication_artifact_persistence_service as persist_mod,
    )
    from app.services import (
        article_publication_artifact_service as artifact_mod,
    )
    from app.services import (
        article_publication_preparation_service as prepare_mod,
    )
    from app.services import (
        wordpress_content_update_execution_service as execute_mod,
    )

    calls: list[str] = []
    _FakeHttp.calls = []
    monkeypatch.setattr(httpx, "Client", _FakeHttp)

    class _Revision:
        def __init__(self, *a, **k) -> None:
            pass

        def revise(self, article_id, **kwargs):
            calls.append("revise")

            class _R:
                revision = type("row", (), {"id": 501})()

            return _R()

    class _Prepare:
        def __init__(self, *a, **k) -> None:
            pass

        def prepare(self, article_id):
            calls.append("prepare")
            return {"article_id": article_id}

    class _Persist:
        def __init__(self, *a, **k) -> None:
            pass

        def persist(self, prepared):
            calls.append("persist")
            return type("artifact", (), {"id": 601, "artifact_hash": "h" * 64})()

    class _Artifact:
        def __init__(self, *a, **k) -> None:
            pass

        def approve_artifact(self, artifact_id, *, expected_artifact_hash):
            calls.append("approve_artifact")

    class _Execute:
        def __init__(self, *a, **k) -> None:
            pass

        def execute(self, **kwargs):
            calls.append("execute")
            return type("result", (), {"run_id": 701, "run_status": "succeeded"})()

    monkeypatch.setattr(revision_mod, "ArticleEditorialRevisionService", _Revision)
    monkeypatch.setattr(prepare_mod, "ArticlePublicationPreparationService", _Prepare)
    monkeypatch.setattr(persist_mod, "ArticlePublicationArtifactPersistenceService", _Persist)
    monkeypatch.setattr(artifact_mod, "ArticlePublicationArtifactService", _Artifact)
    monkeypatch.setattr(execute_mod, "WordPressContentUpdateExecutionService", _Execute)
    return calls


def test_execute_reuses_the_existing_managed_path(factory, approved, managed_path) -> None:
    _, _, request = approved

    outcome = _service(factory).apply(request.id, execute=True)

    assert outcome.outcome == OUTCOME_SUCCEEDED
    assert managed_path == ["revise", "prepare", "persist", "approve_artifact", "execute"]
    assert outcome.editorial_revision_id == 501
    assert outcome.content_update_run_id == 701
    # WordPress への直接の書き込みは無く、状態の読み取りだけが行われる。
    assert all("/wp-json/wp/v2/posts/1018" in url for url in _FakeHttp.calls)


def test_application_history_is_append_only_with_rollback_body(
    factory, approved, managed_path
) -> None:
    source, _, request = approved
    original_body = source.body
    service = _service(factory)

    service.apply(request.id, execute=True)

    with factory() as session:
        rows = session.scalars(select(ChangeApplication).order_by(ChangeApplication.id)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.pre_change_body == original_body
        assert row.source_body_hash and row.proposed_body_hash
        assert row.wordpress_pre_modified_gmt == "2026-06-01T00:00:00"
        assert row.change_request_approval_id is not None
        assert session.get(ChangeRequest, request.id).status == CR_APPLIED

    plan = service.rollback_plan(rows[0].id)
    assert plan["rollback_available"] is True
    assert plan["rollback_body"] == original_body
    assert plan["instructions"]


def test_blocked_apply_never_reaches_the_managed_path(factory, unapproved, managed_path) -> None:
    _, _, request = unapproved

    outcome = _service(factory).apply(request.id, execute=True)

    assert outcome.outcome == OUTCOME_BLOCKED
    assert managed_path == []
    assert _FakeHttp.calls == []
    with factory() as session:
        assert session.scalars(select(ChangeApplication)).all() == []
