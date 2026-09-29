"""T6.5: prompt を作った時点の成績の参考を生成の依頼に固定し、保存の時に照合する。

pin する契約:

- 指紋は参考の中身の正規の JSON から決まる (評価の時刻が違っても同じ中身なら同じ)。
- 依頼には、prompt に入れた参考そのもの (中身と指紋) が固定される。使っていなければ
  ``used_in_generation: false`` と明示し、指紋は求めない。
- 保存の時は固定した参考だけを来歴に使う (今の参考に差し替えない)。固定した中身・指紋・
  実際に送った prompt の 3 つが合わなければ保存しない。
- この項目の無い古い依頼は、前と同じように取り込める。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.models import ThreadsPostProposal
from app.services.threads_generation_provider import GenerationRequest, ManualFileProvider
from app.services.threads_proposal_service import ThreadsProposalService
from app.social.threads.performance_analysis import (
    DIRECTION_HIGHER,
    EVIDENCE_HYPOTHESIS,
    FEEDBACK_NOT_USED,
    FEEDBACK_PROMPT_HEADER,
    PerformanceFeedback,
    freeze_for_request,
    neutral_feedback,
    verify_frozen,
)
from tests.integration.test_threads_openai_generation import seed_articles
from tests.integration.test_threads_proposal_stock_service import _NOW, _answer, _service


def _advisory(value="comparison", as_of="2026-10-01T01:00:00+00:00") -> PerformanceFeedback:
    pattern = {"dimension": "angle", "value": value, "component": "reach", "checkpoint": "24h",
               "n": 6, "evidence": EVIDENCE_HYPOTHESIS, "direction": DIRECTION_HIGHER,
               "actionable": True,
               "statement_ja": f"切り口 「{value}」は views の順位が上 (仮説)。"}  # fmt: skip
    return PerformanceFeedback(as_of=as_of, checkpoint="24h", cohort_n=12, supported=(pattern,),
                               evidence=EVIDENCE_HYPOTHESIS)  # fmt: skip


# == 指紋・固定 (pure) =============================================================================
def test_the_fingerprint_is_deterministic_and_ignores_the_evaluation_time() -> None:
    a = _advisory(as_of="2026-10-01T01:00:00+00:00")
    b = _advisory(as_of="2026-10-02T09:00:00+00:00")
    assert a.fingerprint == b.fingerprint
    frozen = a.freeze()
    assert frozen["used_in_generation"] is True and frozen["fingerprint"] == a.fingerprint
    assert frozen["evaluated_at"] == a.as_of and frozen["evidence"] == EVIDENCE_HYPOTHESIS
    assert PerformanceFeedback.thaw(json.loads(json.dumps(frozen))).fingerprint == a.fingerprint
    assert _advisory("question").fingerprint != a.fingerprint


def test_not_used_feedback_needs_no_fingerprint() -> None:
    frozen = freeze_for_request(None)
    assert frozen == FEEDBACK_NOT_USED and "fingerprint" not in frozen
    assert verify_frozen(frozen, prompt="ふつうの prompt") is None
    with pytest.raises(ValueError, match="no performance feedback was used"):
        verify_frozen(frozen, prompt=f"...\n{FEEDBACK_PROMPT_HEADER}\n...")


def test_a_tampered_or_inconsistent_freeze_is_rejected() -> None:
    frozen = _advisory().freeze()
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        verify_frozen({**frozen, "fingerprint": "0" * 64}, prompt=None)
    with pytest.raises(ValueError, match="does not carry the frozen"):
        verify_frozen(frozen, prompt="参考の節の無い prompt")
    neutral = neutral_feedback("2026-10-01T00:00:00+00:00", "no data").freeze()
    assert verify_frozen(neutral, prompt="参考の節の無い prompt").mode == "neutral"
    with pytest.raises(ValueError, match="is neutral"):
        verify_frozen(neutral, prompt=f"{FEEDBACK_PROMPT_HEADER}\n- x")
    with pytest.raises(ValueError, match="could not be read"):
        verify_frozen({"used_in_generation": True, "content": {"schema": "other"}}, prompt=None)


# == 在庫の生成の経路 =======================================================================
def _stock(session, tmp_path, provider_fn):
    manual = ManualFileProvider(tmp_path / "gen")
    proposals = ThreadsProposalService(session, performance_feedback_provider=provider_fn)
    return manual, _service(session, tmp_path, provider=manual, proposal_service=proposals)


def _request_file(tmp_path, request) -> str:
    return str(tmp_path / "gen" / "pending" / f"{request.request_id}.request.json")


def test_the_request_freezes_the_prompt_time_feedback_and_the_save_uses_it(
    session, tmp_path
) -> None:
    seed_articles(session)
    current = {"feedback": _advisory("comparison")}
    manual, service = _stock(session, tmp_path, lambda _a: current["feedback"])
    service.maintain(now=_NOW, execute=True)
    request = manual.pending()[0]
    data = json.loads(open(_request_file(tmp_path, request), encoding="utf-8").read())
    frozen = data["performance_feedback"]
    assert frozen["fingerprint"] == _advisory("comparison").fingerprint
    assert frozen["used_in_generation"] is True and frozen["content"]["mode"] == "advisory"

    # 依頼と保存の間に参考が変わった (worker が作り直した)。保存は固定した参考を使う。
    current["feedback"] = _advisory("question")
    _answer(manual)
    _, service = _stock(session, tmp_path, lambda _a: current["feedback"])
    out = service.maintain(now=_NOW + timedelta(minutes=1), execute=True, collect_only=True)
    assert out["created"]
    recorded = session.get(ThreadsPostProposal, out["created"][0]).learning_guidance_json[
        "performance_feedback"]  # fmt: skip
    assert recorded["fingerprint"] == _advisory("comparison").fingerprint
    assert recorded["frozen_at_prompt"] is True and recorded["verified_against_prompt"] is True


@pytest.mark.parametrize("tamper", ["fingerprint", "prompt"])
def test_a_mismatch_at_save_time_saves_nothing(session, tmp_path, tamper) -> None:
    seed_articles(session)
    manual, service = _stock(session, tmp_path, lambda _a: _advisory())
    service.maintain(now=_NOW, execute=True)
    pending = manual.pending()
    assert pending
    for request in pending:  # 依頼のすべてを書き換える
        if tamper == "fingerprint":
            path = _request_file(tmp_path, request)
            data = json.loads(open(path, encoding="utf-8").read())
            data["performance_feedback"]["fingerprint"] = "f" * 64
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, ensure_ascii=False))
        else:
            path = tmp_path / "gen" / "pending" / f"{request.request_id}.prompt.txt"
            text = path.read_text("utf-8")
            path.write_text(text.replace(FEEDBACK_PROMPT_HEADER, "## 別の節"), encoding="utf-8")
    _answer(manual)
    _, service = _stock(session, tmp_path, lambda _a: _advisory())
    out = service.maintain(now=_NOW + timedelta(minutes=1), execute=True, collect_only=True)
    assert out["created"] == []
    assert any("performance feedback mismatch" in (f.get("reason") or "")
               for f in [*out.get("skipped", []), *out.get("failures", []),
                         *out.get("rejected", [])])  # fmt: skip
    assert session.query(ThreadsPostProposal).count() == 0


def test_without_a_provider_the_request_says_not_used(session, tmp_path) -> None:
    seed_articles(session)
    manual = ManualFileProvider(tmp_path / "gen")
    _service(session, tmp_path, provider=manual).maintain(now=_NOW, execute=True)
    request = manual.pending()[0]
    assert request.performance_feedback == FEEDBACK_NOT_USED
    _answer(manual)
    out = _service(session, tmp_path, provider=manual).maintain(
        now=_NOW + timedelta(minutes=1), execute=True, collect_only=True)  # fmt: skip
    row = session.get(ThreadsPostProposal, out["created"][0])
    assert row.learning_guidance_json["performance_feedback"] == {
        "used_in_generation": False, "frozen_at_prompt": True}


def test_a_request_from_before_the_freeze_still_reads() -> None:
    legacy = {"request_id": "r" * 20, "article_id": 21, "angles": ["insight"],
              "learning_as_of": _NOW.isoformat(), "guidance_fingerprint": "g" * 64,
              "created_at": _NOW.isoformat()}  # fmt: skip
    request = GenerationRequest.from_dict(legacy, "prompt")
    assert request.performance_feedback is None
    assert "performance_feedback" not in request.as_dict()
