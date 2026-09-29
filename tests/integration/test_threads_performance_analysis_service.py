"""自分の投稿の成績の分析 (T6.5) を DB・CLI・生成につなぐ。

pin する契約:

- 分析は DB を 1 行も変えない。Threads の client を作らない。CLI の ``--analysis`` はファイルも
  書かない (標準出力だけ)。既定の診断の出力はそのまま。
- lane は提案の種類から、トピックはコンテナ作成の記録から、きっかけは提案の来歴から読む。
- 生成への参考は provider を渡したときだけ。渡さなければ prompt は T6.5 より前と同じ。中立・
  失敗なら prompt に何も足さない。使った参考は提案の来歴に小さく残る。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
)
from app.services.threads_performance_analysis_service import (
    ThreadsPerformanceAnalysisService,
    feedback_provider,
)
from app.social.threads.performance_analysis import (
    DIRECTION_HIGHER,
    EVIDENCE_HYPOTHESIS,
    LANE_GROWTH,
    LANE_REGULAR,
    MODE_NEUTRAL,
    PerformanceFeedback,
)
from tests.integration.test_threads_performance_service import (
    _T0,
    _publication,
    _Settings,
    article,  # noqa: F401 - fixture
)

_AS_OF = _T0 + timedelta(days=1)


@pytest.fixture(autouse=True)
def _no_threads_client(monkeypatch):
    from app.social.threads import service as threads_service

    def _refuse(*_a, **_k):
        raise AssertionError("the analysis must not build a Threads client")

    monkeypatch.setattr(threads_service.ThreadsService, "__init__", _refuse)


def _attempt(session, publication, topic_tag=None, *, recorded=True):
    session.add(ThreadsPublicationAttempt(
        threads_publication_id=publication.id, step="create_container", outcome="succeeded",
        detail_json={"topic_tag": topic_tag} if recorded else {"container_id": "c"},
        started_at=_T0, finished_at=_T0,
    ))  # fmt: skip
    session.commit()


def _growth(session, seed, *, remote_at, views):
    proposal = ThreadsPostProposal(
        source_article_id=None, source_article_body_hash="0" * 64, angle="account_growth",
        link_mode="none", content_text=f"成長 {seed}", character_count=4,
        content_seed=seed * 64, proposal_hash=seed.upper() * 64, policy_version="t2.1",
        generator_version="threads-growth-1", status="approved",
        learning_guidance_json={"content_kind": "account_growth"},
    )  # fmt: skip
    session.add(proposal)
    session.flush()
    row = ThreadsPublication(
        proposal_id=proposal.id, proposal_hash=proposal.proposal_hash, source_article_id=None,
        angle="account_growth", exact_published_text=proposal.content_text,
        threads_media_id=f"g-{seed}", status="published", published_at=remote_at,
        remote_timestamp=remote_at.strftime("%Y-%m-%dT%H:%M:%S+0000"),
    )  # fmt: skip
    session.add(row)
    session.flush()
    session.add(ThreadsInsightSnapshot(
        threads_publication_id=row.id, threads_media_id=row.threads_media_id,
        observed_at=remote_at + timedelta(hours=1), age_hours=1.0, outcome="observed",
        views=views, likes=0, replies=0, reposts=0, quotes=0, shares=0,
    ))  # fmt: skip
    session.commit()
    return row


def _seed(session, article):  # noqa: F811
    rows = []
    for i, views in enumerate((30, 20, 10, 25, 15, 5)):
        remote = _T0 + timedelta(hours=i)
        rows.append(_publication(session, article, "abcdef"[i], local_at=remote,
                                 remote_at=remote, views=[(60, views)]))  # fmt: skip
    session.get(ThreadsPostProposal, rows[0].proposal_id).learning_guidance_json = {
        "generation_brief": {"conversation_hook": "choice"}}
    session.commit()
    _attempt(session, rows[0], "AI Threads")
    _attempt(session, rows[1], None)
    _attempt(session, rows[2], recorded=False)
    rows.append(_growth(session, "g", remote_at=_T0 + timedelta(hours=2), views=500))
    return rows


def _counts(session):
    return tuple(session.scalar(select(func.count()).select_from(m))
                 for m in (ThreadsPublication, ThreadsInsightSnapshot, ThreadsPostProposal,
                           ThreadsPublicationAttempt))  # fmt: skip


def test_the_analysis_reads_lanes_topics_and_hooks_and_changes_nothing(
    session: Session, article  # noqa: F811
) -> None:
    _seed(session, article)
    before = _counts(session)
    report = ThreadsPerformanceAnalysisService(session, settings=_Settings()).report(
        as_of=_AS_OF)  # fmt: skip
    assert _counts(session) == before
    assert not session.new and not session.dirty
    posts = {p["publication_id"]: p for p in report["posts"]}
    assert [p["lane"] for p in posts.values()].count(LANE_GROWTH) == 1
    assert report["cohorts"][LANE_REGULAR]["comparable"] == 6  # growth は入らない
    assert report["cohorts"][LANE_REGULAR]["checkpoint"] == "1h"  # 観測は 1h だけ
    first, second, third = posts[1], posts[2], posts[3]
    assert (first["threads_topic"], first["conversation_hook"]) == ("AI Threads", "choice")
    assert (second["threads_topic"], second["conversation_hook"]) == ("none", "legacy")
    assert third["threads_topic"] == "unknown"
    assert first["origin"] == "article" and posts[7]["origin"] == "non_article"
    assert posts[7]["conversation_hook"] == "unknown"  # Growth にきっかけは無い


def test_feedback_is_neutral_when_the_analysis_fails(session: Session, monkeypatch) -> None:
    service = ThreadsPerformanceAnalysisService(session, settings=_Settings())
    monkeypatch.setattr(service, "report", lambda **_k: 1 / 0)
    feedback = service.feedback(as_of=_AS_OF)
    assert feedback.mode == MODE_NEUTRAL
    assert "ZeroDivisionError" in feedback.notes[0]


def _scoped(session):
    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    return _Scoped()


def test_the_cli_analysis_prints_only_and_writes_nothing(
    session: Session, article, tmp_path, capsys  # noqa: F811
) -> None:
    from scripts.analyze_threads_performance import main

    _seed(session, article)
    before = _counts(session)
    common = ["--as-of", _AS_OF.isoformat(), "--output-dir", str(tmp_path)]
    kw = {"session_factory": _scoped(session), "settings": _Settings()}

    assert main([*common, "--analysis"], **kw) == 0
    table = capsys.readouterr().out
    assert "cohort regular: checkpoint 1h n=6 (hypothesis)" in table
    assert "| #7 |" in table and "feedback: mode=" in table
    assert main([*common, "--analysis", "--format", "json", "--lane", "growth"], **kw) == 0
    out = capsys.readouterr().out
    payload = json.loads(out[: out.rindex("read-only:")])
    assert [p["lane"] for p in payload["posts"]] == [LANE_GROWTH]
    assert payload["feedback"]["schema"] == "threads-performance-feedback/1"
    assert main([*common, "--analysis", "--feedback", "--format", "json"], **kw) == 0
    out = capsys.readouterr().out
    assert json.loads(out[: out.rindex("read-only:")])["lane"] == LANE_REGULAR
    assert "files written = 0" in out

    assert _counts(session) == before
    assert list(tmp_path.iterdir()) == []  # 既定の診断のファイルも書かない


# == 生成へのつなぎ ==============================================================================
def _advisory(as_of="2026-10-01T01:00:00+00:00") -> PerformanceFeedback:
    pattern = {"dimension": "angle", "value": "comparison", "component": "reach",
               "checkpoint": "24h", "n": 6, "evidence": EVIDENCE_HYPOTHESIS,
               "direction": DIRECTION_HIGHER, "actionable": True,
               "statement_ja": "切り口 「comparison」は views の順位が上 (仮説)。"}  # fmt: skip
    return PerformanceFeedback(as_of=as_of, checkpoint="24h", cohort_n=12, supported=(pattern,))


def test_the_prompt_is_unchanged_without_feedback_and_extended_with_it(session) -> None:
    from app.services.threads_proposal_service import ThreadsProposalService
    from app.social.threads.performance_analysis import neutral_feedback
    from tests.integration.test_threads_openai_generation import seed_articles
    from tests.integration.test_threads_proposal_stock_service import _NOW

    seed_articles(session)

    def prompt(provider=None):
        return ThreadsProposalService(session, performance_feedback_provider=provider).build_prompt(
            article_id=21, angles=["insight"], learning_as_of=_NOW)  # fmt: skip

    plain = prompt()
    assert plain.performance_feedback is None
    assert plain.as_dict()["performance_feedback"] is None
    neutral = prompt(lambda as_of: neutral_feedback(as_of.isoformat(), "no data"))
    assert neutral.rendered_prompt == plain.rendered_prompt
    failing = prompt(lambda as_of: 1 / 0)
    assert failing.rendered_prompt == plain.rendered_prompt
    assert failing.performance_feedback.mode == MODE_NEUTRAL

    advised = prompt(lambda as_of: _advisory())
    assert advised.rendered_prompt != plain.rendered_prompt
    assert "## 過去の成績からの補助の参考" in advised.rendered_prompt
    assert "事実・記事の根拠・文体の規則が優先" in advised.rendered_prompt
    assert advised.rendered_prompt.startswith(plain.rendered_prompt.split("## 過去の成績")[0][:200])
    assert advised.as_dict()["performance_feedback"]["mode"] == "advisory"


def test_generated_proposals_record_the_feedback_provenance(session, tmp_path) -> None:
    from app.models import TP_AWAITING_APPROVAL
    from app.services.threads_generation_provider import ManualFileProvider
    from app.services.threads_proposal_service import ThreadsProposalService
    from tests.integration.test_threads_openai_generation import seed_articles
    from tests.integration.test_threads_proposal_stock_service import _NOW, _answer, _service

    seed_articles(session)
    manual = ManualFileProvider(tmp_path / "gen")
    proposals = ThreadsProposalService(
        session, performance_feedback_provider=lambda _a: _advisory())  # fmt: skip
    _service(session, tmp_path, provider=manual, proposal_service=proposals).maintain(
        now=_NOW, execute=True)  # fmt: skip
    request = manual.pending()[0]
    assert "## 過去の成績からの補助の参考" in request.prompt
    _answer(manual)
    out = _service(session, tmp_path, provider=manual, proposal_service=proposals).maintain(
        now=_NOW + timedelta(minutes=1), execute=True, collect_only=True)  # fmt: skip
    assert out["created"]
    row = session.get(ThreadsPostProposal, out["created"][0])
    assert row.status == TP_AWAITING_APPROVAL
    recorded = row.learning_guidance_json["performance_feedback"]
    assert recorded["fingerprint"] == _advisory().fingerprint
    assert recorded["patterns"] == [{"dimension": "angle", "value": "comparison",
                                     "component": "reach", "direction": DIRECTION_HIGHER,
                                     "evidence": EVIDENCE_HYPOTHESIS}]  # fmt: skip
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0


def test_the_provider_factory_reads_through_its_own_session(session: Session, article) -> None:  # noqa: F811
    _seed(session, article)
    provide = feedback_provider(_scoped(session), settings=_Settings())
    feedback = provide(_AS_OF)
    assert feedback.checkpoint == "1h" and feedback.cohort_n == 6
