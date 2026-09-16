"""WordPressContentUpdateRunRepository の統合テスト (D-D5B / D-D5B.1 / D-D5B.2)。

D-D5B は local foundation のみ -- live preflight GET も WordPress write もここでは
一切行わない。``add_running`` / ``mark_succeeded`` / ``mark_failed`` /
``mark_outcome_unknown`` がローカル DB に対して正しく動作することだけを検証する。

D-D5B.1: ``running`` は preflight 成功後にのみ作られる (durable な ``prepared``
相当の事前状態は無い) ため、``expected_pre_update_wordpress_raw_content_hash`` と
``observed_pre_update_wordpress_raw_content_hash`` の両方が必須であることを
pin する -- 欠落した preflight 証跡から ``running`` 行が作れないことを証明する。

D-D5B.2: D-D5A.1 の確定した契約により、永続化された running 行は必ず
``expected_pre_update_wordpress_raw_content_hash ==
observed_pre_update_wordpress_raw_content_hash`` を満たす (不一致は
``WORDPRESS_CURRENT_CONTENT_DRIFT`` として run を作らず fail closed するのが
将来の D-D5C 分類 service の責務)。D-D5B.1 が誤って「両者は差があってもよい」と
していたテスト/契約を訂正し、repository guard + DB CHECK constraint の
defense-in-depth を証明する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.exceptions import WordPressContentUpdateRunError
from app.models import Article, ArticlePublicationArtifact
from app.models.wordpress_content_update_run import (
    WP_CONTENT_UPDATE_FAILED,
    WP_CONTENT_UPDATE_OUTCOME_UNKNOWN,
    WP_CONTENT_UPDATE_RUNNING,
    WP_CONTENT_UPDATE_SUCCEEDED,
)
from app.repositories.wordpress_content_update_run_repository import (
    WordPressContentUpdateRunRepository,
)
from app.wordpress.content_update_request import build_wordpress_content_update_request

_HREF = "https://official.example.test/tool-a"


def _seed_artifact(session: Session, *, slug: str = "p1") -> ArticlePublicationArtifact:
    art = Article(title="t", slug=slug, keyword_id=None, body=f"[tool]({_HREF})\n")
    session.add(art)
    session.commit()

    artifact = ArticlePublicationArtifact(
        article_id=art.id,
        canonical_body_hash="a" * 64,
        renderer_version="wordpress_html_v1",
        artifact_schema_version=1,
        substitution_manifest_json="[]",
        artifact_hash="b" * 64,
        tracked_html="<p>tracked</p>",
        tracked_html_hash="c" * 64,
        substitution_count=0,
        generated_at=datetime.now(UTC),
    )
    session.add(artifact)
    session.commit()
    return artifact


def _running_fields(artifact: ArticlePublicationArtifact, **over) -> dict:
    req = build_wordpress_content_update_request(
        wordpress_post_id="25",
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        tracked_html=artifact.tracked_html,
        target_base_url="https://bizfluxlab.com",
    )
    fields = dict(
        article_id=artifact.article_id,
        wordpress_post_id="25",
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        method=req.method,
        endpoint_path=req.endpoint_path,
        update_payload_json=req.update_payload_json,
        update_payload_hash=req.update_payload_hash,
        content_update_request_identity_hash=req.content_update_request_identity_hash,
        target_content_update_request_identity_hash=req.target_content_update_request_identity_hash,
        target_base_url="https://bizfluxlab.com",
        request_content_hash=req.request_content_hash,
        expected_pre_update_wordpress_raw_content_hash="e" * 64,
        observed_pre_update_wordpress_raw_content_hash="e" * 64,
        observed_pre_update_modified_gmt_raw="2026-09-06T13:46:00",
        idempotency_key=None,
        started_at=datetime.now(UTC),
    )
    fields.update(over)
    return fields


# ==================== insert (running から直接始まる) =========================
def test_add_running_inserts_running_status(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))

    assert run.id is not None
    assert run.status == WP_CONTENT_UPDATE_RUNNING
    assert run.finished_at is None
    assert run.started_at is not None


def test_get_by_id_round_trips(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))

    found = repo.get_by_id(run.id)
    assert found is not None
    assert found.id == run.id


# ==================== running -> succeeded =====================================
def test_running_to_succeeded_sets_finished_at_and_evidence(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))

    finished = datetime.now(UTC)
    updated = repo.mark_succeeded(
        run,
        http_status=200,
        response_content_raw_hash="f" * 64,
        response_content_rendered_hash="g" * 64,
        finished_at=finished,
    )

    assert updated.status == WP_CONTENT_UPDATE_SUCCEEDED
    assert updated.finished_at == finished
    assert updated.http_status == 200
    assert updated.response_content_raw_hash == "f" * 64
    assert updated.error_message is None


def test_succeeded_requires_response_content_raw_hash_argument(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))

    with pytest.raises(TypeError):
        repo.mark_succeeded(  # type: ignore[call-arg]
            run, http_status=200, finished_at=datetime.now(UTC)
        )


# ==================== running -> failed =========================================
def test_running_to_failed_sets_finished_at_and_error(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))

    finished = datetime.now(UTC)
    updated = repo.mark_failed(
        run,
        error_message="definitive rejection: 403",
        finished_at=finished,
        http_status=403,
        provider_error_code="rest_cannot_edit",
    )

    assert updated.status == WP_CONTENT_UPDATE_FAILED
    assert updated.finished_at == finished
    assert updated.error_message == "definitive rejection: 403"
    assert updated.http_status == 403


# ==================== running -> outcome_unknown ================================
def test_running_to_outcome_unknown_stays_terminal(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))

    finished = datetime.now(UTC)
    updated = repo.mark_outcome_unknown(run, error_message="timeout", finished_at=finished)

    assert updated.status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
    assert updated.finished_at == finished

    # terminal -- 二度と succeeded/failed へ自動遷移しない
    with pytest.raises(WordPressContentUpdateRunError):
        repo.mark_succeeded(
            updated,
            http_status=200,
            response_content_raw_hash="f" * 64,
            finished_at=datetime.now(UTC),
        )
    with pytest.raises(WordPressContentUpdateRunError):
        repo.mark_failed(updated, error_message="x", finished_at=datetime.now(UTC))


def test_outcome_unknown_run_is_discoverable_by_blocking_lookup(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))
    repo.mark_outcome_unknown(run, error_message="timeout", finished_at=datetime.now(UTC))

    blocking = repo.find_blocking_run_for_post(
        article_id=artifact.article_id, wordpress_post_id="25"
    )
    assert blocking is not None
    assert blocking.id == run.id


# ==================== terminal -> any transition rejected ======================
@pytest.mark.parametrize(
    "make_terminal",
    [
        lambda repo, run: repo.mark_succeeded(
            run,
            http_status=200,
            response_content_raw_hash="f" * 64,
            finished_at=datetime.now(UTC),
        ),
        lambda repo, run: repo.mark_failed(run, error_message="x", finished_at=datetime.now(UTC)),
        lambda repo, run: repo.mark_outcome_unknown(
            run, error_message="x", finished_at=datetime.now(UTC)
        ),
    ],
)
def test_terminal_run_rejects_any_further_transition(session: Session, make_terminal) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))
    terminal = make_terminal(repo, run)

    for attempt in (
        lambda: repo.mark_succeeded(
            terminal,
            http_status=200,
            response_content_raw_hash="f" * 64,
            finished_at=datetime.now(UTC),
        ),
        lambda: repo.mark_failed(terminal, error_message="x", finished_at=datetime.now(UTC)),
        lambda: repo.mark_outcome_unknown(
            terminal, error_message="x", finished_at=datetime.now(UTC)
        ),
    ):
        with pytest.raises(WordPressContentUpdateRunError):
            attempt()


# ==================== hash-namespace regression (§29) ===========================
def test_succeeded_accepts_request_content_hash_different_from_response_raw_hash(
    session: Session,
) -> None:
    """Article #1 が証明した通り、submitted content hash と WordPress stored raw
    hash は正当に異なりうる。succeeded への遷移はこの 2 つの等値性を一切要求しない。
    """

    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    fields = _running_fields(artifact)
    run = repo.add_running(**fields)

    assert run.request_content_hash != "totally-different-raw-hash-value-000000000000000"[:64]

    deliberately_different_raw_hash = "9" * 64
    assert run.request_content_hash != deliberately_different_raw_hash

    updated = repo.mark_succeeded(
        run,
        http_status=200,
        response_content_raw_hash=deliberately_different_raw_hash,
        finished_at=datetime.now(UTC),
    )
    assert updated.status == WP_CONTENT_UPDATE_SUCCEEDED
    assert updated.response_content_raw_hash == deliberately_different_raw_hash
    assert updated.response_content_raw_hash != updated.request_content_hash


# ==================== idempotency (§21) ==========================================
def test_idempotency_key_unique_across_runs(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    repo.add_running(**_running_fields(artifact, idempotency_key="key-1"))
    session.commit()

    with pytest.raises(IntegrityError):
        repo.add_running(**_running_fields(artifact, idempotency_key="key-1"))
        session.commit()
    session.rollback()


def test_get_by_idempotency_key_finds_run(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact, idempotency_key="key-2"))
    session.commit()

    found = repo.get_by_idempotency_key("key-2")
    assert found is not None
    assert found.id == run.id


def test_null_idempotency_key_does_not_collide(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    repo.add_running(**_running_fields(artifact, idempotency_key=None))
    session.commit()
    repo.add_running(**_running_fields(artifact, idempotency_key=None))
    session.commit()  # SQL NULL != NULL -- 複数 NULL は unique 制約に抵触しない


# ==================== reads used by future execution service ====================
def test_latest_succeeded_for_post(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))
    repo.mark_succeeded(
        run,
        http_status=200,
        response_content_raw_hash="f" * 64,
        finished_at=datetime.now(UTC),
    )

    latest = repo.latest_succeeded_for_post(article_id=artifact.article_id, wordpress_post_id="25")
    assert latest is not None
    assert latest.id == run.id


def test_latest_succeeded_for_post_none_when_no_succeeded_run(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    repo.add_running(**_running_fields(artifact))  # still running

    latest = repo.latest_succeeded_for_post(article_id=artifact.article_id, wordpress_post_id="25")
    assert latest is None


def test_find_succeeded_by_target_identity(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))
    repo.mark_succeeded(
        run,
        http_status=200,
        response_content_raw_hash="f" * 64,
        finished_at=datetime.now(UTC),
    )

    found = repo.find_succeeded_by_target_identity(run.target_content_update_request_identity_hash)
    assert found is not None
    assert found.id == run.id


def test_list_by_article_orders_newest_first(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run1 = repo.add_running(**_running_fields(artifact, idempotency_key="k-a"))
    repo.mark_failed(run1, error_message="x", finished_at=datetime.now(UTC))
    run2 = repo.add_running(**_running_fields(artifact, idempotency_key="k-b"))

    runs = repo.list_by_article(artifact.article_id)
    assert [r.id for r in runs][:1] == [run2.id]


# ==================== frozen fields untouched by lifecycle mutation =============
def test_mark_succeeded_does_not_mutate_frozen_request_fields(session: Session) -> None:
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    fields = _running_fields(artifact)
    run = repo.add_running(**fields)

    before = {
        "article_id": run.article_id,
        "wordpress_post_id": run.wordpress_post_id,
        "artifact_hash": run.artifact_hash,
        "update_payload_json": run.update_payload_json,
        "request_content_hash": run.request_content_hash,
        "expected_pre_update_wordpress_raw_content_hash": (
            run.expected_pre_update_wordpress_raw_content_hash
        ),
    }
    repo.mark_succeeded(
        run,
        http_status=200,
        response_content_raw_hash="f" * 64,
        finished_at=datetime.now(UTC),
    )
    for field, value in before.items():
        assert getattr(run, field) == value


# ==================== repository has no generic update/delete ===================
def test_repository_has_no_generic_update_or_delete(session: Session) -> None:
    repo = WordPressContentUpdateRunRepository(session)
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")


# ==================== D-D5B.1 §10: preflight evidence contract ==================
def test_add_running_with_valid_expected_and_observed_raw_hashes_succeeds(
    session: Session,
) -> None:
    """§10.A -- 有効な expected/observed raw hash があれば running 行を作れる。"""

    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(
        **_running_fields(
            artifact,
            expected_pre_update_wordpress_raw_content_hash="1" * 64,
            observed_pre_update_wordpress_raw_content_hash="1" * 64,
        )
    )

    assert run.status == WP_CONTENT_UPDATE_RUNNING
    assert run.expected_pre_update_wordpress_raw_content_hash == "1" * 64
    assert run.observed_pre_update_wordpress_raw_content_hash == "1" * 64


def test_persisted_running_row_always_has_both_raw_hashes(session: Session) -> None:
    """§10.B -- 永続化された running 行は必ず両方の値を持つ (NULL を許さない)。"""

    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(**_running_fields(artifact))

    reloaded = repo.get_by_id(run.id)
    assert reloaded is not None
    assert reloaded.expected_pre_update_wordpress_raw_content_hash is not None
    assert reloaded.observed_pre_update_wordpress_raw_content_hash is not None


def test_missing_observed_raw_hash_argument_raises_before_flush(session: Session) -> None:
    """§10.C -- observed raw hash 引数そのものが無ければ TypeError (呼び出し時点で拒否)。"""

    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    fields = _running_fields(artifact)
    del fields["observed_pre_update_wordpress_raw_content_hash"]

    with pytest.raises(TypeError):
        repo.add_running(**fields)


@pytest.mark.parametrize(
    "field",
    [
        "expected_pre_update_wordpress_raw_content_hash",
        "observed_pre_update_wordpress_raw_content_hash",
    ],
)
def test_none_raw_hash_value_rejected_before_flush(session: Session, field: str) -> None:
    """§10.C -- 引数として明示的に ``None``/空文字を渡した場合も、late な
    ``IntegrityError`` を待たず flush 前に ``WordPressContentUpdateRunError``
    で拒否する。"""

    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    fields = _running_fields(artifact, **{field: None})

    with pytest.raises(WordPressContentUpdateRunError, match=field):
        repo.add_running(**fields)


def test_mismatched_expected_and_observed_raw_hashes_rejected_before_flush(
    session: Session,
) -> None:
    """D-D5B.2 §9.B -- D-D5A.1 の確定した契約では、expected/observed が
    preflight 時点で一致した場合にのみ (``UPDATE_REQUIRED``) running 行が
    作られる。不一致 (``WORDPRESS_CURRENT_CONTENT_DRIFT``) は run を一切
    作らずに fail closed する (D-D5B.2 §1-2 -- D-D5B.1 が誤って「差を許容する」
    としていた契約を訂正する)。repository は late な DB ``IntegrityError`` を
    待たず flush 前に拒否する。"""

    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    fields = _running_fields(
        artifact,
        expected_pre_update_wordpress_raw_content_hash="2" * 64,
        observed_pre_update_wordpress_raw_content_hash="3" * 64,
    )

    with pytest.raises(WordPressContentUpdateRunError, match="does not match"):
        repo.add_running(**fields)


def test_mismatch_rejection_does_not_use_drift_classification_reason_code(
    session: Session,
) -> None:
    """D-D5B.2 §7 -- repository の拒否理由は run-model 契約違反としてのみ表現
    され、``WORDPRESS_CURRENT_CONTENT_DRIFT`` という将来の分類 reason code を
    ここに持ち込まない。"""

    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    fields = _running_fields(
        artifact,
        expected_pre_update_wordpress_raw_content_hash="6" * 64,
        observed_pre_update_wordpress_raw_content_hash="7" * 64,
    )

    with pytest.raises(WordPressContentUpdateRunError) as exc_info:
        repo.add_running(**fields)
    assert "WORDPRESS_CURRENT_CONTENT_DRIFT" not in str(exc_info.value)


def test_direct_db_insert_with_mismatched_raw_hashes_violates_check_constraint(
    session: Session,
) -> None:
    """D-D5B.2 §9.C -- repository を経由せず ORM から直接不一致な値で insert
    しても、DB の CHECK 制約 (``ck_wordpress_content_update_runs_pre_update_raw_match``)
    が拒否する (defense-in-depth の DB 層)。"""

    from sqlalchemy.exc import IntegrityError

    from app.models import WordPressContentUpdateRun
    from app.models.wordpress_content_update_run import WP_CONTENT_UPDATE_RUNNING

    artifact = _seed_artifact(session)
    fields = _running_fields(
        artifact,
        expected_pre_update_wordpress_raw_content_hash="8" * 64,
        observed_pre_update_wordpress_raw_content_hash="9" * 64,
    )
    entity = WordPressContentUpdateRun(status=WP_CONTENT_UPDATE_RUNNING, **fields)
    session.add(entity)

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_request_content_hash_may_differ_from_matching_raw_hashes(
    session: Session,
) -> None:
    """D-D5B.2 §9.D -- expected/observed raw hash が (互いに一致した上で)
    request_content_hash と異なっていても running 行は正常に作れる。raw hash
    と request hash は別 namespace のまま比較されない (D-D5A.1 §3)。"""

    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    fields = _running_fields(
        artifact,
        expected_pre_update_wordpress_raw_content_hash="4" * 64,
        observed_pre_update_wordpress_raw_content_hash="4" * 64,
    )
    assert (
        fields["expected_pre_update_wordpress_raw_content_hash"]
        != fields["request_content_hash"]
    )

    run = repo.add_running(**fields)

    assert run.status == WP_CONTENT_UPDATE_RUNNING
    assert run.request_content_hash == fields["request_content_hash"]
    assert run.expected_pre_update_wordpress_raw_content_hash == "4" * 64
    assert run.observed_pre_update_wordpress_raw_content_hash == "4" * 64


def test_observed_pre_update_modified_gmt_raw_remains_optional(session: Session) -> None:
    """D-D5B.2 §9.F -- provider タイムスタンプは informational/audit only の
    ままで、running 行の作成に必須ではない。"""

    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    fields = _running_fields(artifact, observed_pre_update_modified_gmt_raw=None)

    run = repo.add_running(**fields)

    assert run.status == WP_CONTENT_UPDATE_RUNNING
    assert run.observed_pre_update_modified_gmt_raw is None


def test_add_running_rejects_arbitrary_unknown_keyword(session: Session) -> None:
    # 明示的な signature になったので、モデルに存在しない/意図しないキーは
    # TypeError で拒否される (以前の **fields passthrough では通っていた)。
    artifact = _seed_artifact(session)
    repo = WordPressContentUpdateRunRepository(session)
    fields = _running_fields(artifact)
    fields["response_content_raw_hash"] = "f" * 64  # running 作成時には渡せないフィールド

    with pytest.raises(TypeError):
        repo.add_running(**fields)
