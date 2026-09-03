"""DraftGenerationRunService: prepare / execute / submit-result / drift / retry / tx。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.exceptions import (
    DraftGenerationNotReadyError,
    DraftGenerationStateError,
    EntityNotFoundError,
    PromptInputChangedError,
)
from app.models import Article, DraftGenerationRun
from app.models.draft_generation_run import (
    MODE_MANUAL,
    RUN_FAILED,
    RUN_PREPARED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
)
from app.models.enums import ArticleStatus
from app.services.draft_generation_run_service import DraftGenerationRunService
from app.services.draft_prompt_preview_service import DraftPromptPreviewService
from tests.support.draft_generation_fixture import default_overrides, frozen_scenario

_TOOLS7 = ["Make", "HubSpot", "ClickUp", "monday.com", "Pipedrive", "Reclaim.ai", "Todoist"]


def _svc(session: Session) -> DraftGenerationRunService:
    return DraftGenerationRunService(session)


def _run_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(DraftGenerationRun))


def _prepare(session: Session, fs, *, overrides=None, **kw):
    ov = overrides or default_overrides(fs)
    pv = DraftPromptPreviewService(session).preview(
        fs.article_id, snapshot_id=fs.snapshot_id, overrides=ov
    )
    return _svc(session).prepare(
        fs.article_id,
        snapshot_id=fs.snapshot_id,
        expected_prompt_hash=pv["prompt_input_hash"],
        expected_rendered_prompt_hash=pv["rendered_prompt_hash"],
        execution_mode=MODE_MANUAL,
        editorial_overrides=ov,
        **kw,
    )


def _good_output() -> str:
    tools_line = " / ".join(_TOOLS7)
    body = (
        "本記事は広告（アフィリエイト）を含みます。\n\n"
        "## 業務効率化ツールとは\n" + "解説。" * 200 + "\n\n"
        "## 選び方\n判断基準を示します。\n\n"
        f"## おすすめ業務効率化ツール比較\n{tools_line} を比較します。料金は2026年8月時点。\n\n"
        "## 目的別おすすめ\n用途に応じて選びます。\n\n"
        "## 導入時の注意点\n請求書払いは各社で異なり本記事では未確認。\n\n"
        "## よくある質問\nQ&A。\n\n## まとめ\n結論。"
    )
    return json.dumps(
        {"meta_description": "業務効率化ツールのおすすめを目的別に比較して選び方を解説します。",
         "body_markdown": body, "generation_notes": []},
        ensure_ascii=False,
    )


# -- prepare --------------------------------------------------------


def test_prepare_creates_prepared_run_without_llm(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=7)
    run, already = _prepare(session, fs)
    assert already is False
    assert run.status == RUN_PREPARED
    assert run.execution_mode == MODE_MANUAL
    assert run.raw_output is None and run.parsed_body is None
    assert _run_count(session) == 1
    # Article は planned のまま
    assert session.get(Article, fs.article_id).status == ArticleStatus.PLANNED.value


def test_prepare_stores_frozen_prompt_artifact(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    run, _ = _prepare(session, fs)
    from app.article.draft_prompt_canonical import (
        compute_prompt_input_hash,
        compute_rendered_prompt_hash,
    )

    assert compute_prompt_input_hash(run.prompt_package) == run.prompt_input_hash
    assert compute_rendered_prompt_hash(run.rendered_prompt) == run.rendered_prompt_hash
    assert run.snapshot_content_hash == fs.snapshot.content_hash


def test_prepare_drift_guard_prompt_hash(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    ov = default_overrides(fs)
    pv = DraftPromptPreviewService(session).preview(
        fs.article_id, snapshot_id=fs.snapshot_id, overrides=ov
    )
    with pytest.raises(PromptInputChangedError):
        _svc(session).prepare(
            fs.article_id,
            snapshot_id=fs.snapshot_id,
            expected_prompt_hash="0" * 64,
            expected_rendered_prompt_hash=pv["rendered_prompt_hash"],
            execution_mode=MODE_MANUAL,
            editorial_overrides=ov,
        )
    assert _run_count(session) == 0


def test_prepare_drift_guard_rendered_hash(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    ov = default_overrides(fs)
    pv = DraftPromptPreviewService(session).preview(
        fs.article_id, snapshot_id=fs.snapshot_id, overrides=ov
    )
    with pytest.raises(PromptInputChangedError):
        _svc(session).prepare(
            fs.article_id,
            snapshot_id=fs.snapshot_id,
            expected_prompt_hash=pv["prompt_input_hash"],
            expected_rendered_prompt_hash="0" * 64,
            execution_mode=MODE_MANUAL,
            editorial_overrides=ov,
        )
    assert _run_count(session) == 0


def test_prepare_rejects_article_with_body(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    art = session.get(Article, fs.article_id)
    art.body = "existing draft"
    session.commit()
    with pytest.raises(DraftGenerationStateError):
        _prepare(session, fs)
    assert _run_count(session) == 0


def test_prepare_nonterminal_duplicate_returns_existing(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    run1, a1 = _prepare(session, fs)
    run2, a2 = _prepare(session, fs)
    assert a1 is False and a2 is True
    assert run2.id == run1.id
    assert _run_count(session) == 1


def test_prepare_idempotency_key(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    run1, _ = _prepare(session, fs, idempotency_key="k-1")
    run2, already = _prepare(session, fs, idempotency_key="k-1")
    assert already is True and run2.id == run1.id
    assert _run_count(session) == 1


def test_prepare_idempotency_key_conflict(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    _prepare(session, fs, idempotency_key="k-2")
    # 同じ key で異なる identity (provider が違う) -> conflict
    ov = default_overrides(fs)
    pv = DraftPromptPreviewService(session).preview(
        fs.article_id, snapshot_id=fs.snapshot_id, overrides=ov
    )
    with pytest.raises(DraftGenerationStateError):
        _svc(session).prepare(
            fs.article_id,
            snapshot_id=fs.snapshot_id,
            expected_prompt_hash=pv["prompt_input_hash"],
            expected_rendered_prompt_hash=pv["rendered_prompt_hash"],
            execution_mode=MODE_MANUAL,
            editorial_overrides=ov,
            provider="anthropic",
            idempotency_key="k-2",
        )


# -- execute -----------------------------------------------------


def test_execute_planned_to_drafting_manual(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=7)
    run, _ = _prepare(session, fs)
    out_run, next_action, rendered = _svc(session).execute(fs.article_id, run.id)
    assert out_run.status == RUN_RUNNING
    assert next_action == "submit_result"
    assert rendered == run.rendered_prompt
    assert session.get(Article, fs.article_id).status == ArticleStatus.DRAFTING.value


def test_execute_rejects_non_prepared(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    run, _ = _prepare(session, fs)
    _svc(session).execute(fs.article_id, run.id)
    with pytest.raises(DraftGenerationStateError):
        _svc(session).execute(fs.article_id, run.id)


def test_execute_rejects_when_another_run_running(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    run_a, _ = _prepare(session, fs)
    _svc(session).execute(fs.article_id, run_a.id)  # running
    # 別 identity の run を prepare して execute しようとする
    run_b, _ = _prepare(session, fs, provider="anthropic")
    with pytest.raises(DraftGenerationStateError):
        _svc(session).execute(fs.article_id, run_b.id)


def test_execute_rejects_bad_article_status(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    run, _ = _prepare(session, fs)
    art = session.get(Article, fs.article_id)
    art.status = ArticleStatus.REVIEW.value
    session.commit()
    with pytest.raises(DraftGenerationStateError):
        _svc(session).execute(fs.article_id, run.id)


def test_execute_uses_stored_prompt_not_current_builder(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    run, _ = _prepare(session, fs)
    stored_rendered = run.rendered_prompt
    # builder / renderer をこの後に変更しても execute は保存済みを使う
    _, _, rendered = _svc(session).execute(fs.article_id, run.id)
    assert rendered == stored_rendered


def test_execute_rejects_corrupted_stored_hash(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    run, _ = _prepare(session, fs)
    run.prompt_input_hash = "0" * 64  # artifact 破損
    session.commit()
    with pytest.raises(DraftGenerationNotReadyError):
        _svc(session).execute(fs.article_id, run.id)


# -- submit-result ---------------------------------------------


def test_submit_result_success_does_not_write_article_body(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=7)
    run, _ = _prepare(session, fs)
    _svc(session).execute(fs.article_id, run.id)
    out = _svc(session).submit_result(fs.article_id, run.id, _good_output())
    assert out.status == RUN_SUCCEEDED
    assert out.parsed_body is not None and out.parsed_meta_description is not None
    assert out.validation_report["overall"] in {"pass", "warn"}
    art = session.get(Article, fs.article_id)
    assert art.body is None
    assert art.meta_description is None
    assert art.status == ArticleStatus.DRAFTING.value


def test_submit_result_invalid_json_marks_failed(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    run, _ = _prepare(session, fs)
    _svc(session).execute(fs.article_id, run.id)
    out = _svc(session).submit_result(fs.article_id, run.id, "not json at all")
    assert out.status == RUN_FAILED
    assert out.error_message
    assert session.get(Article, fs.article_id).status == ArticleStatus.DRAFTING.value


def test_submit_result_validator_fail_still_succeeds_but_not_promotable(
    session: Session,
) -> None:
    fs = frozen_scenario(session, n_tools=7)
    run, _ = _prepare(session, fs)
    _svc(session).execute(fs.article_id, run.id)
    bad = json.loads(_good_output())
    bad["body_markdown"] = "# タイトル\n" + bad["body_markdown"]  # H1 -> validator fail
    out = _svc(session).submit_result(
        fs.article_id, run.id, json.dumps(bad, ensure_ascii=False)
    )
    assert out.status == RUN_SUCCEEDED
    assert out.validation_report["overall"] == "fail"
    assert out.validation_report["promotion_eligible"] is False


def test_submit_result_requires_running(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    run, _ = _prepare(session, fs)
    with pytest.raises(DraftGenerationStateError):
        _svc(session).submit_result(fs.article_id, run.id, _good_output())


# -- retry regression (Phase 3C-4A 設計矛盾の修正, §75) -------


def test_retry_after_failure_keeps_article_drafting_and_new_run(
    session: Session,
) -> None:
    fs = frozen_scenario(session, n_tools=7)
    run1, _ = _prepare(session, fs)
    _svc(session).execute(fs.article_id, run1.id)  # planned -> drafting
    _svc(session).submit_result(fs.article_id, run1.id, "broken")  # failed
    assert session.get(Article, fs.article_id).status == ArticleStatus.DRAFTING.value
    assert run1.status == RUN_FAILED

    # retry: Article は drafting のまま新 run を prepare + execute できる
    run2, already = _prepare(session, fs, provider="retry")
    assert already is False and run2.id != run1.id
    out_run, _, _ = _svc(session).execute(fs.article_id, run2.id)
    assert out_run.status == RUN_RUNNING
    assert session.get(Article, fs.article_id).status == ArticleStatus.DRAFTING.value
    assert _run_count(session) == 2


# -- transaction ------------------------------------------------


def test_prepare_rolls_back_on_commit_failure(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = frozen_scenario(session, n_tools=3)
    ov = default_overrides(fs)
    pv = DraftPromptPreviewService(session).preview(
        fs.article_id, snapshot_id=fs.snapshot_id, overrides=ov
    )

    def boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", boom)
    with pytest.raises(RuntimeError):
        _svc(session).prepare(
            fs.article_id,
            snapshot_id=fs.snapshot_id,
            expected_prompt_hash=pv["prompt_input_hash"],
            expected_rendered_prompt_hash=pv["rendered_prompt_hash"],
            execution_mode=MODE_MANUAL,
            editorial_overrides=ov,
        )
    monkeypatch.undo()
    assert _run_count(session) == 0


def test_get_rejects_run_from_other_article(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=2)
    run, _ = _prepare(session, fs)
    other = frozen_scenario(session, n_tools=2, suffix="b")
    with pytest.raises(EntityNotFoundError):
        _svc(session).get(other.article_id, run.id)


def test_article_delete_cascades_runs(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=2)
    _prepare(session, fs)
    assert _run_count(session) == 1
    session.delete(session.get(Article, fs.article_id))
    session.commit()
    assert _run_count(session) == 0
