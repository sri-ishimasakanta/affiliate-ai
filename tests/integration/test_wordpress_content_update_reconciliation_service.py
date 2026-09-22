"""WordPressContentUpdateReconciliationService の統合テスト (C4.9)。

``outcome_unknown`` は設計上 terminal で、``succeeded`` へ遷移する経路は存在しない
(D-D5A.1 §16-17)。この service は run 行を一切書き換えずに、別テーブルへ
「後から live 観測で照合した結果」を append することだけを行う。

ここで pin する契約:

- 受け付けるのは ``outcome_unknown`` の run **のみ** (succeeded / failed /
  running を後から塗り替えられない)。
- article / post の所有権を検証する (他 article の run は reconcile できない)。
- WordPress へは **read-only の GET を 1 回だけ**。write は絶対に発行しない。
- content-equivalent なら ``reconciled_succeeded``、そうでなければ ``unresolved``。
- ``reconciled_succeeded`` の run だけが blocking から外れる。``unresolved`` は
  blocking のまま (fail closed)。
- 2 回目の呼び出しは no-op -- 行も GET も増えない。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.exceptions import WordPressContentUpdateReconciliationError
from app.models import Article, ArticlePublicationArtifact
from app.models.wordpress_content_update_reconciliation import (
    WP_CU_RECONCILED_SUCCEEDED,
    WP_CU_RECONCILED_UNRESOLVED,
)
from app.models.wordpress_content_update_run import WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
from app.repositories.wordpress_content_update_reconciliation_repository import (
    WordPressContentUpdateReconciliationRepository,
)
from app.repositories.wordpress_content_update_run_repository import (
    WordPressContentUpdateRunRepository,
)
from app.services.wordpress_content_update_reconciliation_service import (
    REASON_CONTENT_NOT_EQUIVALENT,
    REASON_EXCERPT_MISMATCH,
    REASON_TITLE_MISMATCH,
    REASON_WORDPRESS_FETCH_FAILED,
    REASON_WORDPRESS_STATUS_MISMATCH,
    WordPressContentUpdateReconciliationService,
)
from app.wordpress.content_update_request import build_wordpress_content_update_request

_POST_ID = "76"
_TITLE = "AI エージェントの基礎"
_EXCERPT = "AI エージェントの導入を検討する前に押さえる基礎。"
_INTENDED = '<p>本文です。</p>\n<h2>見出し</h2>\n<p><a href="https://example.test/a">出典</a></p>'
# WordPress が保存時に属性を足しても visible text / href 列 / heading 列は変わらない。
_SANITIZED = (
    '<p style="line-height:1.8">本文です。</p>\n'
    '<h2 class="wp-block-heading">見出し</h2>\n'
    '<p><a href="https://example.test/a" rel="noopener">出典</a></p>'
)
_BASELINE_RAW = "<p>更新前の本文</p>"


class _FakeWordPressClient:
    """テスト専用 fake -- 実ネットワークへは一切到達しない。

    ``update_post_content_exact`` を呼ぶと即座に失敗する -- reconciliation が
    write を発行しないことをテスト側で強制するため。
    """

    def __init__(self, *, get_response: dict | None = None, get_exc: BaseException | None = None):
        self._get_response = get_response
        self._get_exc = get_exc
        self.get_calls = 0

    def get_post(self, post_id: int) -> dict:
        self.get_calls += 1
        if self._get_exc is not None:
            raise self._get_exc
        assert self._get_response is not None
        return self._get_response

    def update_post_content_exact(self, post_id: int, payload_json: str):
        raise AssertionError("reconciliation must never issue a write")


def _live(
    *,
    post_id: int = int(_POST_ID),
    status: str = "draft",
    content_raw: str = _SANITIZED,
    title: str = _TITLE,
    excerpt: str = _EXCERPT,
) -> dict:
    return {
        "id": post_id,
        "status": status,
        "content": {"raw": content_raw, "rendered": content_raw},
        "title": {"raw": title, "rendered": title},
        "excerpt": {"raw": excerpt, "rendered": excerpt},
        "modified_gmt": "2026-09-22T10:00:00",
    }


def _seed(session: Session, *, status: str = WP_CONTENT_UPDATE_OUTCOME_UNKNOWN):
    article = Article(
        title=_TITLE,
        slug="ai-agents",
        keyword_id=None,
        body="本文です。",
        meta_description=_EXCERPT,
        wordpress_post_id=_POST_ID,
    )
    session.add(article)
    session.commit()

    artifact = ArticlePublicationArtifact(
        article_id=article.id,
        canonical_body_hash="a" * 64,
        renderer_version="wordpress_html_v1",
        artifact_schema_version=1,
        substitution_manifest_json="[]",
        artifact_hash="b" * 64,
        tracked_html=_INTENDED,
        tracked_html_hash="c" * 64,
        substitution_count=0,
        generated_at=datetime.now(UTC),
    )
    session.add(artifact)
    session.commit()

    req = build_wordpress_content_update_request(
        wordpress_post_id=_POST_ID,
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        tracked_html=_INTENDED,
        target_base_url="https://bizfluxlab.com",
    )
    baseline = compute_text_hash(_BASELINE_RAW)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(
        article_id=article.id,
        wordpress_post_id=_POST_ID,
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        method=req.method,
        endpoint_path=req.endpoint_path,
        update_payload_json=req.update_payload_json,
        update_payload_hash=req.update_payload_hash,
        content_update_request_identity_hash=req.content_update_request_identity_hash,
        target_content_update_request_identity_hash=(
            req.target_content_update_request_identity_hash
        ),
        target_base_url="https://bizfluxlab.com",
        request_content_hash=req.request_content_hash,
        expected_pre_update_wordpress_raw_content_hash=baseline,
        observed_pre_update_wordpress_raw_content_hash=baseline,
        observed_pre_update_modified_gmt_raw="2026-09-22T09:00:00",
        idempotency_key=None,
        started_at=datetime.now(UTC),
    )
    now = datetime.now(UTC)
    if status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN:
        repo.mark_outcome_unknown(run, error_message="read-back unverifiable", finished_at=now)
    elif status == "succeeded":
        repo.mark_succeeded(
            run,
            http_status=200,
            response_content_raw_hash=compute_text_hash(_SANITIZED),
            finished_at=now,
        )
    elif status == "failed":
        repo.mark_failed(run, error_message="definitive failure", finished_at=now)
    session.commit()
    return article, run


def _reconcile(session: Session, run, client, **over):
    kwargs = dict(
        run_id=run.id,
        expected_article_id=run.article_id,
        expected_wordpress_post_id=_POST_ID,
        expected_wordpress_status="draft",
        reason="C4.9: outcome_unknown の書き込みが反映済みかを live 観測で照合する",
    )
    kwargs.update(over)
    return WordPressContentUpdateReconciliationService(session, wordpress_client=client).reconcile(
        **kwargs
    )


# ==================== 一致 -> reconciled_succeeded ============================
def test_equivalent_live_content_is_reconciled_succeeded(session: Session) -> None:
    _article, run = _seed(session)
    client = _FakeWordPressClient(get_response=_live())

    result = _reconcile(session, run, client)

    assert result.verdict == WP_CU_RECONCILED_SUCCEEDED
    assert result.resolved is True
    assert result.unresolved_reason_code is None
    assert result.already_reconciled is False
    assert result.raw_content_changed_since_attempt is True
    assert client.get_calls == 1


def test_run_status_is_never_rewritten(session: Session) -> None:
    _article, run = _seed(session)
    _reconcile(session, run, _FakeWordPressClient(get_response=_live()))

    session.refresh(run)
    assert run.status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
    assert run.error_message == "read-back unverifiable"


def test_reconciled_run_stops_blocking_new_updates(session: Session) -> None:
    article, run = _seed(session)
    repo = WordPressContentUpdateRunRepository(session)

    blocking_before = repo.find_blocking_run_for_post(
        article_id=article.id, wordpress_post_id=_POST_ID
    )
    assert blocking_before is not None and blocking_before.id == run.id

    _reconcile(session, run, _FakeWordPressClient(get_response=_live()))

    assert (
        repo.find_blocking_run_for_post(article_id=article.id, wordpress_post_id=_POST_ID) is None
    )


def test_reconciliation_record_freezes_run_evidence(session: Session) -> None:
    _article, run = _seed(session)
    _reconcile(session, run, _FakeWordPressClient(get_response=_live()))

    row = WordPressContentUpdateReconciliationRepository(session).get_for_run(run.id)
    assert row is not None
    assert row.request_content_hash == run.request_content_hash
    assert (
        row.pre_update_wordpress_raw_content_hash
        == run.observed_pre_update_wordpress_raw_content_hash
    )
    assert row.observed_wordpress_raw_content_hash == compute_text_hash(_SANITIZED)
    # 2 つの hash namespace は別物 -- 等値であってはならない。
    assert row.observed_wordpress_raw_content_hash != row.request_content_hash
    assert row.comparison_json["equivalent"] is True
    assert row.reason


# ==================== 不一致 -> unresolved (blocking のまま) ==================
def test_different_content_is_unresolved_and_keeps_blocking(session: Session) -> None:
    article, run = _seed(session)
    client = _FakeWordPressClient(get_response=_live(content_raw="<p>まったく別の本文</p>"))

    result = _reconcile(session, run, client)

    assert result.verdict == WP_CU_RECONCILED_UNRESOLVED
    assert result.resolved is False
    assert result.unresolved_reason_code == REASON_CONTENT_NOT_EQUIVALENT
    blocking = WordPressContentUpdateRunRepository(session).find_blocking_run_for_post(
        article_id=article.id, wordpress_post_id=_POST_ID
    )
    assert blocking is not None and blocking.id == run.id


def test_unchanged_baseline_content_is_unresolved(session: Session) -> None:
    """write が届いていなければ live は更新前のままで、照合は解決しない。"""

    _article, run = _seed(session)
    result = _reconcile(
        session, run, _FakeWordPressClient(get_response=_live(content_raw=_BASELINE_RAW))
    )

    assert result.verdict == WP_CU_RECONCILED_UNRESOLVED
    assert result.raw_content_changed_since_attempt is False


def test_substituted_href_is_unresolved(session: Session) -> None:
    """visible text が同じでもリンク先が違えば同一とは見なさない。"""

    _article, run = _seed(session)
    swapped = _SANITIZED.replace("https://example.test/a", "https://evil.test/a")
    result = _reconcile(session, run, _FakeWordPressClient(get_response=_live(content_raw=swapped)))

    assert result.verdict == WP_CU_RECONCILED_UNRESOLVED
    assert result.unresolved_reason_code == REASON_CONTENT_NOT_EQUIVALENT


def test_unexpected_wordpress_status_is_unresolved(session: Session) -> None:
    _article, run = _seed(session)
    result = _reconcile(session, run, _FakeWordPressClient(get_response=_live(status="publish")))

    assert result.verdict == WP_CU_RECONCILED_UNRESOLVED
    assert result.unresolved_reason_code == REASON_WORDPRESS_STATUS_MISMATCH


def test_title_mismatch_is_unresolved(session: Session) -> None:
    _article, run = _seed(session)
    result = _reconcile(session, run, _FakeWordPressClient(get_response=_live(title="別の見出し")))

    assert result.unresolved_reason_code == REASON_TITLE_MISMATCH


def test_excerpt_mismatch_is_unresolved(session: Session) -> None:
    _article, run = _seed(session)
    result = _reconcile(session, run, _FakeWordPressClient(get_response=_live(excerpt="別の要約")))

    assert result.unresolved_reason_code == REASON_EXCERPT_MISMATCH


def test_fetch_failure_is_unresolved_not_an_exception(session: Session) -> None:
    _article, run = _seed(session)
    result = _reconcile(session, run, _FakeWordPressClient(get_exc=RuntimeError("network down")))

    assert result.verdict == WP_CU_RECONCILED_UNRESOLVED
    assert result.unresolved_reason_code == REASON_WORDPRESS_FETCH_FAILED
    assert result.observed_wordpress_raw_content_hash is None


# ==================== 受け付けない run ========================================
@pytest.mark.parametrize("status", ["succeeded", "failed", "running"])
def test_non_outcome_unknown_runs_are_refused(session: Session, status: str) -> None:
    _article, run = _seed(session, status=status)
    client = _FakeWordPressClient(get_response=_live())

    with pytest.raises(WordPressContentUpdateReconciliationError):
        _reconcile(session, run, client)

    assert client.get_calls == 0
    assert WordPressContentUpdateReconciliationRepository(session).get_for_run(run.id) is None


def test_wrong_article_is_refused(session: Session) -> None:
    _article, run = _seed(session)
    client = _FakeWordPressClient(get_response=_live())

    with pytest.raises(WordPressContentUpdateReconciliationError):
        _reconcile(session, run, client, expected_article_id=run.article_id + 999)

    assert client.get_calls == 0


def test_wrong_post_id_is_refused(session: Session) -> None:
    _article, run = _seed(session)
    client = _FakeWordPressClient(get_response=_live())

    with pytest.raises(WordPressContentUpdateReconciliationError):
        _reconcile(session, run, client, expected_wordpress_post_id="999")

    assert client.get_calls == 0


def test_blank_reason_is_refused(session: Session) -> None:
    _article, run = _seed(session)
    client = _FakeWordPressClient(get_response=_live())

    with pytest.raises(WordPressContentUpdateReconciliationError):
        _reconcile(session, run, client, reason="   ")

    assert client.get_calls == 0


def test_unsupported_expected_status_is_refused(session: Session) -> None:
    _article, run = _seed(session)
    client = _FakeWordPressClient(get_response=_live())

    with pytest.raises(WordPressContentUpdateReconciliationError):
        _reconcile(session, run, client, expected_wordpress_status="trash")

    assert client.get_calls == 0


def test_missing_run_is_refused(session: Session) -> None:
    service = WordPressContentUpdateReconciliationService(
        session, wordpress_client=_FakeWordPressClient(get_response=_live())
    )
    with pytest.raises(WordPressContentUpdateReconciliationError):
        service.reconcile(
            run_id=9999,
            expected_article_id=1,
            expected_wordpress_post_id=_POST_ID,
            expected_wordpress_status="draft",
            reason="r",
        )


# ==================== 冪等 ====================================================
def test_second_reconcile_is_a_noop(session: Session) -> None:
    _article, run = _seed(session)
    first = _reconcile(session, run, _FakeWordPressClient(get_response=_live()))

    second_client = _FakeWordPressClient(get_response=_live())
    second = _reconcile(session, run, second_client)

    assert second.already_reconciled is True
    assert second.reconciliation_id == first.reconciliation_id
    assert second.verdict == first.verdict
    assert second_client.get_calls == 0


def test_unresolved_verdict_is_not_overwritten_by_a_later_attempt(session: Session) -> None:
    """一度 unresolved になったら、後から live が一致しても自動では解決しない。"""

    _article, run = _seed(session)
    _reconcile(session, run, _FakeWordPressClient(get_response=_live(content_raw="<p>別</p>")))

    later = _reconcile(session, run, _FakeWordPressClient(get_response=_live()))

    assert later.already_reconciled is True
    assert later.verdict == WP_CU_RECONCILED_UNRESOLVED
