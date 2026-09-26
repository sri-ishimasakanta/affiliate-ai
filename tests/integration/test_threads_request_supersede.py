"""答え待ちの生成の依頼を「置き換え済み」として閉じる (監査つき・冪等。本番の依頼は使わない)。

pin する契約:

- 既定は PLAN (何も変えない)。実行すると依頼と prompt を消さずに ``failed/`` へ移し、
  ``outcome.json`` に ``result: superseded``・理由・時刻を残す。答え待ちの数から外れる。
- LLM を呼ばない・提案を作らない・承認も公開もしない。ほかの依頼に触れない。
- 断る: 理由が無い・依頼が無い・答えが取り込みを待っている・もう取り込み済み・別の理由で
  閉じ済み。2 回目は ``already_superseded`` で何もしない。
- CLI (``--supersede-request``) も同じ。PLAN は何も書かない。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import ThreadsPostProposal, ThreadsPublication
from app.services.threads_generation_provider import ManualFileProvider
from tests.integration.test_threads_openai_generation import FakeLuna, _provider, seed_articles
from tests.integration.test_threads_proposal_stock_service import _NOW, _service

REASON = "superseded by threads generation brief t6.3 conversation-style schema"


@pytest.fixture
def articles(session):
    return seed_articles(session)


def _setup(session, tmp_path):
    manual = ManualFileProvider(tmp_path / "gen")
    _service(session, tmp_path, provider=manual).maintain(now=_NOW, execute=True)
    pending = manual.pending()
    assert len(pending) == 3
    return manual, pending


def _snapshot(directory):
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file()
    }


def test_plan_changes_nothing_and_execute_closes_only_that_request(
    session, articles, tmp_path
) -> None:
    manual, pending = _setup(session, tmp_path)
    target, others = pending[0], pending[1:]
    before = _snapshot(tmp_path / "gen")
    original_request = (tmp_path / "gen/pending" / f"{target.request_id}.request.json").read_bytes()
    original_prompt = (tmp_path / "gen/pending" / f"{target.request_id}.prompt.txt").read_bytes()

    service = _service(session, tmp_path, provider=manual)
    plan = service.supersede_pending(target.request_id, reason=REASON, now=_NOW)
    assert plan["result"] == "would_supersede" and plan["executed"] is False
    assert _snapshot(tmp_path / "gen") == before  # PLAN は何も変えない

    done = service.supersede_pending(target.request_id, reason=REASON, now=_NOW, execute=True)
    assert done["result"] == "superseded" and done["executed"] is True
    failed = tmp_path / "gen" / "failed"
    assert (failed / f"{target.request_id}.request.json").read_bytes() == original_request
    assert (failed / f"{target.request_id}.prompt.txt").read_bytes() == original_prompt
    outcome = json.loads((failed / f"{target.request_id}.outcome.json").read_text("utf-8"))
    assert outcome["result"] == "superseded" and outcome["reason"] == REASON
    assert not (failed / f"{target.request_id}.response.json").exists()  # 答えを作らない
    assert {r.request_id for r in manual.pending()} == {r.request_id for r in others}
    for other in others:  # ほかの依頼はそのまま
        name = f"pending/{other.request_id}.request.json"
        assert _snapshot(tmp_path / "gen")[name] == before[name]
    assert session.scalar(select(func.count()).select_from(ThreadsPostProposal)) == 0
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0


def test_it_is_idempotent_and_refuses_unsafe_cases(session, articles, tmp_path) -> None:
    manual, pending = _setup(session, tmp_path)
    service = _service(session, tmp_path, provider=manual)
    rid = pending[0].request_id
    assert service.supersede_pending(rid, reason="", execute=True)["result"] == "refused"
    service.supersede_pending(rid, reason=REASON, now=_NOW, execute=True)
    again = service.supersede_pending(rid, reason=REASON, now=_NOW, execute=True)
    assert again["result"] == "already_superseded" and again["executed"] is False
    unknown = service.supersede_pending("nosuchrequest0000000", reason=REASON, execute=True)
    assert unknown["result"] == "refused" and "no such" in unknown["why"]
    answered = pending[1]
    (tmp_path / "gen/pending" / f"{answered.request_id}.response.json").write_text(
        json.dumps({"proposals": []}), encoding="utf-8"
    )
    waiting = service.supersede_pending(answered.request_id, reason=REASON, execute=True)
    assert waiting["result"] == "refused" and "waiting to be imported" in waiting["why"]
    assert (tmp_path / "gen/pending" / f"{answered.request_id}.request.json").exists()


def test_an_imported_or_otherwise_closed_request_is_refused(session, articles, tmp_path) -> None:
    manual, pending = _setup(session, tmp_path)
    rid = pending[0].request_id
    (tmp_path / "gen/pending" / f"{rid}.response.json").write_text(
        json.dumps({"proposals": [{"angle": pending[0].angles[0], "link_mode": "none",
                    "conversation_hook": pending[0].conversation_hook,
                    "body": f"記事{pending[0].article_id}の要点。体制を先に決める。"}]},
                   ensure_ascii=False), encoding="utf-8")  # fmt: skip
    _service(session, tmp_path, provider=manual).maintain(
        now=_NOW + timedelta(minutes=1), execute=True, collect_only=True
    )
    assert (tmp_path / "gen/done" / f"{rid}.outcome.json").exists()
    refused = _service(session, tmp_path, provider=manual).supersede_pending(
        rid, reason=REASON, execute=True
    )
    assert refused["result"] == "refused" and "already imported" in refused["why"]
    stale = pending[1].request_id
    manual.complete(pending[1], ok=False, outcome={"result": "stale"})
    closed = _service(session, tmp_path, provider=manual).supersede_pending(
        stale, reason=REASON, execute=True
    )
    assert closed["result"] == "refused" and "already closed (stale)" in closed["why"]


def test_superseding_with_the_openai_provider_never_calls_the_api(
    session, articles, tmp_path
) -> None:
    manual, pending = _setup(session, tmp_path)
    fake = FakeLuna()
    provider = _provider(tmp_path, fake)  # 同じディレクトリを使う自動生成の provider
    result = _service(session, tmp_path, provider=provider).supersede_pending(
        pending[0].request_id, reason=REASON, now=_NOW, execute=True
    )
    assert result["result"] == "superseded" and fake.calls == 0
    assert not list((tmp_path / "gen").rglob("*.openai.json"))


def test_the_cli_plans_by_default_and_supersedes_with_execute(
    session, articles, tmp_path, capsys
) -> None:
    from scripts.maintain_threads_proposal_stock import main

    manual, pending = _setup(session, tmp_path)
    rid = pending[0].request_id

    class _Factory:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    common = {"session_factory": _Factory(), "settings": _service(session, tmp_path)._settings,
              "overrides": {"provider": manual, "alert_notifiers": []}, "now": _NOW}  # fmt: skip
    before = _snapshot(tmp_path / "gen")
    assert main(["--supersede-request", rid, "--reason", REASON], **common) == 0
    assert "PLAN only" in capsys.readouterr().out and _snapshot(tmp_path / "gen") == before
    assert main(["--supersede-request", rid, "--reason", REASON, "--execute"], **common) == 0
    assert (tmp_path / "gen/failed" / f"{rid}.outcome.json").exists()
    assert main(["--supersede-request", rid, "--reason", REASON, "--execute"], **common) == 0
    assert '"already_superseded"' in capsys.readouterr().out
    assert main(["--supersede-request", "nosuchrequest0000000", "--reason", REASON], **common) == 2
