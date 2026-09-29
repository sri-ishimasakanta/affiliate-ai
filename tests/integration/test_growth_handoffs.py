"""承認した Growth Action を、手元の依頼として既存の流れへ渡す (C9-B)。

pin する契約:

- Threads の提案 (通常・別の切り口) → 記事を指定した生成の依頼 (pending) を 1 つ。変換の中で
  OpenAI・Threads を呼ばない。提案は、在庫の保守が **もともと生成するとき** に、方針のスイッチが
  true のときだけ使う (既定 false: 今までと同じ)。依頼の数は増えない。できた提案は
  awaiting_approval のまま (承認・公開は既存の流れ)。答えが来ないまま古くなれば、依頼は
  pending へ戻る。
- 別の切り口は、変換の時点の勧めの切り口を固定する。もう使った切り口なら断る。
- 記事の計画 → 計画の依頼 (pending)。記事は作らない。人が承認して、既存の流れで作った記事を
  結ぶ (前からある記事・違うキーワード・archived・ほかの依頼と結んだ記事は断る)。
- 本文・メタ・配置 → 変更の準備の依頼 (中身は無い・URL は入れない)。元の hash を固定し、計画で
  見た hash が要る (違えば断る)。結ぶのは固定した元の上に作った変更だけ。変更の依頼は作らない。
- どの変換も、重なる依頼・提案・変更の依頼があれば断る (fail closed)。同じ変換の 2 回目は
  同じ依頼を返す。migration 前は何も書かずに断る。
- 変換 ≠ 承認 ≠ 公開 / 適用 (状態は別々に見える)。worker は変換しない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.article.draft_promotion_canonical import compute_text_hash
from app.growth import analysis as ga
from app.growth import inbox as gi
from app.models import (
    CR_AWAITING_APPROVAL,
    PUB_PUBLISHED,
    TP_AWAITING_APPROVAL,
    Article,
    ChangeRequest,
    Keyword,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.models.growth_action import GA_CONVERTED, GrowthActionCandidate, GrowthActionConversion
from app.models.growth_handoff import GrowthHandoffRequest
from app.services import growth_action_conversion_service as gcs
from app.services import growth_action_service as gas
from app.services import growth_handoff_service as ghs
from app.services.threads_generation_provider import ManualFileProvider
from app.services.threads_proposal_stock_service import ThreadsProposalStockService
from tests.integration.test_threads_proposal_stock_service import (  # noqa: F401 - fixture
    _answer,
    _ReadOnlyThreads,
    articles,
)
from tests.integration.test_threads_proposal_stock_service import _Settings as _StockSettings

_NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)


class _Settings:
    wordpress_base_url = "https://bizfluxlab.com"
    search_console_property_uri = "sc-domain:example.com"
    search_console_credentials_file = None
    ga4_property_id = None


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    import httpx

    from app.services import threads_openai_provider
    from app.social.threads import service as threads_service

    def refuse(*_a, **_k):
        raise AssertionError("handoffs must not call anything external")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(threads_service.ThreadsService, "__init__", refuse)
    monkeypatch.setattr(threads_openai_provider.OpenAIResponsesClient, "__init__", refuse)


@pytest.fixture(autouse=True)
def _no_overlap(monkeypatch):
    """記事の計画の重なりの再確認 (既存の計画のサービス) の代役。個別の試験で差し替える。"""

    monkeypatch.setattr(gcs.GrowthActionConversionService, "_cannibalization",
                        lambda self, keyword: {"ok": keyword is not None,
                                               "detail": "no overlap (test)"})  # fmt: skip


@pytest.fixture
def site(session, articles):  # noqa: F811 - 在庫の試験と同じ 5 記事
    keyword = Keyword(keyword="RPA 比較")
    session.add(keyword)
    session.commit()
    return {"articles": articles, "keyword": keyword}


def _src(source, state=ga.USABLE, **metrics):
    return ga.SourceEvidence(source, state, "test", "test", metrics=metrics)


def _article_evidence(aid, *, threads, seo=(), clicks=0):
    sources = {s: _src(s, ga.UNAVAILABLE) for s in ga.SOURCES}
    sources["threads"] = _src("threads", ga.USABLE, **threads)
    sources["affiliate"] = _src("affiliate", ga.INSUFFICIENT, clean_clicks=clicks)
    return ga.GrowthEvidence(
        subject_type="article", subject_id=f"article:{aid}", article_id=aid,
        article={"status": "published", "age_days": 60, "monetization_mode": "affiliate"},
        sources=sources, existing_candidates={"seo": tuple(seo)})  # fmt: skip


def _report(keyword_id: int) -> dict:
    no_post = {"publications": 0, "open_proposals": []}
    # 記事 24: 通常の投稿は 15 日前 (再告知の窓が過ぎた) と、試していない切り口。
    old_post = {"publications": 1, "latest_age_hours": 15 * 24, "oldest_age_hours": 15 * 24,
                "angles_tried": ["insight"], "reach_percentile_ranks": [0.2],
                "open_proposals": []}  # fmt: skip
    seo = ({"candidate_type": "CTR_IMPROVEMENT", "reason_code": "LOW_CTR", "priority": "medium",
            "evidence": {"query": "rpa 比較"}},
           {"candidate_type": "RANKING_IMPROVEMENT", "reason_code": "STRIKING_DISTANCE",
            "priority": "medium", "evidence": {"query": "rpa 導入"}})  # fmt: skip
    evidences = [_article_evidence(25, threads=no_post, seo=seo, clicks=2),
                 _article_evidence(24, threads=old_post)]
    candidates = []
    for evidence in evidences:
        candidates += ga.build_candidates(evidence, ga.classify(
            evidence, angles=("insight", "question")))  # fmt: skip
    keyword = ga.GrowthEvidence(
        subject_type="keyword", subject_id=f"keyword:{keyword_id}", keyword_id=keyword_id,
        article={"keyword": "RPA 比較", "keyword_status": "new"},
        sources={"keyword": _src("keyword", ga.USABLE, total_score=80, score_id=1)})
    candidates += ga.build_candidates(keyword, ga.classify(keyword))
    return {"candidates": [c.as_dict() for c in ga.sort_candidates(candidates)],
            "growth_plan": None}


def _builder(keyword_id):
    def build(session, **_kw):
        return {"entries": [{**c, "availability": gi.ACTIONABLE_NOW, "availability_reasons": []}
                            for c in _report(keyword_id)["candidates"]]}

    return build


def _service(session, site):
    return gcs.GrowthActionConversionService(session, settings=_Settings(),
                                             inbox_builder=_builder(site["keyword"].id))


def _approved(session, site, action, article_id=None) -> GrowthActionCandidate:
    history = gas.GrowthActionHistory(session)
    if not history.rows():
        history.refresh(_report(site["keyword"].id), gi.CoverageContext(), now=_NOW,
                        execute=True)
    row = next(r for r in history.rows() if r.action_type == action
               and (article_id is None or r.article_id == article_id)
               and r.status in ("active", "approved", "pending_review"))  # fmt: skip
    reviews = gas.GrowthActionReviewService(session)
    review = reviews.request_review(row.id, now=_NOW)
    reviews.approve(review.id, expected_candidate_fingerprint=row.candidate_fingerprint, now=_NOW)
    return row


def _counts(session) -> dict:
    names = [r[0] for r in session.execute(text(
        "select name from sqlite_master where type='table'"))]  # fmt: skip
    return {n: session.execute(text(f'select count(*) from "{n}"')).scalar() for n in names}


def _handoffs(session) -> list[GrowthHandoffRequest]:
    return list(session.scalars(select(GrowthHandoffRequest).order_by(GrowthHandoffRequest.id)))


def _refused(session, fn, match):
    before = len(_handoffs(session))
    with pytest.raises(gas.GrowthActionError, match=match):
        fn()
    assert len(_handoffs(session)) == before


def _proposal(session, article_id, *, angle="insight", status=TP_AWAITING_APPROVAL, seed="p"):
    body = session.get(Article, article_id).body
    row = ThreadsPostProposal(
        source_article_id=article_id, source_article_body_hash=compute_text_hash(body),
        angle=angle,
        link_mode="none", content_text="既存の案。", character_count=5, content_seed=seed * 64,
        proposal_hash=seed.upper() * 64, policy_version="t2.1",
        generator_version="threads-proposal-1", status=status)  # fmt: skip
    session.add(row)
    session.commit()
    return row


def _stock(session, tmp_path):
    return ThreadsProposalStockService(session, settings=_StockSettings(),
                                       threads_service=_ReadOnlyThreads(),
                                       provider=ManualFileProvider(tmp_path / "gen"),
                                       alert_notifiers=[])  # fmt: skip


def _enable(monkeypatch):
    monkeypatch.setattr(ghs, "consume_targeted_enabled", lambda policy=None: True)


# == Threads: 記事を指定した生成の依頼 ==================================================
def test_a_regular_threads_conversion_creates_one_pending_targeted_request(session, site) -> None:
    row = _approved(session, site, ga.CREATE_REGULAR_THREADS_POST, 25)
    before = _counts(session)
    plan = _service(session, site).plan(row.id, now=_NOW)
    assert _counts(session) == before  # PLAN は書かない
    assert plan["executable"] is True
    assert plan["plan"]["target_workflow"] == "threads_generation_request"
    assert plan["plan"]["expected_external_calls"] == []
    result = _service(session, site).execute(row.id, now=_NOW)
    assert result["executed"] is True and result["side_effects"]["openai_calls"] == 0
    [handoff] = _handoffs(session)
    assert (handoff.workflow, handoff.status, handoff.lane) == ("threads_generation", "pending",
                                                                "regular")
    assert handoff.article_id == 25 and handoff.requested_angle is None
    assert handoff.source_review_id == result["plan"]["review_id"]
    assert session.scalar(select(func.count()).select_from(ThreadsPostProposal)) == 0
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0
    session.refresh(row)
    assert row.status == GA_CONVERTED
    [down] = result["downstream"]
    assert down["type"] == "threads_generation_request" and down["state"] == "pending"
    assert down["effective_at"] is None  # 変換 ≠ 公開
    # 冪等: 2 回目は同じ依頼 (新しく書かない)。
    again = _service(session, site).execute(row.id, now=_NOW + timedelta(minutes=5))
    assert again["idempotent_replay"] is True and len(_handoffs(session)) == 1
    assert session.scalar(select(func.count()).select_from(GrowthActionConversion)) == 1


def test_an_alternative_angle_freezes_the_recommended_angle(session, site) -> None:
    row = _approved(session, site, ga.CREATE_THREADS_ALTERNATIVE_ANGLE, 24)
    _service(session, site).execute(row.id, now=_NOW)
    [handoff] = _handoffs(session)
    assert (handoff.lane, handoff.requested_angle) == ("alternative_angle", "question")
    assert handoff.frozen_json["angles_tried"] == ["insight"]


def test_an_already_published_angle_is_refused(session, site) -> None:
    proposal = _proposal(session, 24, angle="question", status="approved")
    session.add(ThreadsPublication(proposal_id=proposal.id, proposal_hash=proposal.proposal_hash,
                                   angle="question", exact_published_text="既存の案。",
                                   status=PUB_PUBLISHED, published_at=_NOW - timedelta(days=20)))
    session.commit()
    row = _approved(session, site, ga.CREATE_THREADS_ALTERNATIVE_ANGLE, 24)
    _refused(session, lambda: _service(session, site).execute(row.id, now=_NOW),
             "frozen_angle_untried")


def test_an_open_proposal_or_targeted_request_is_refused(session, site) -> None:
    regular = _approved(session, site, ga.CREATE_REGULAR_THREADS_POST, 24)
    _service(session, site).execute(regular.id, now=_NOW)
    alternative = _approved(session, site, ga.CREATE_THREADS_ALTERNATIVE_ANGLE, 24)
    _refused(session, lambda: _service(session, site).execute(alternative.id, now=_NOW),
             "no_open_targeted_request")
    _proposal(session, 25)
    other = _approved(session, site, ga.CREATE_REGULAR_THREADS_POST, 25)
    _refused(session, lambda: _service(session, site).execute(other.id, now=_NOW),
             "no_open_threads_proposal")


def test_stock_maintenance_ignores_targeted_requests_by_default(session, site, tmp_path) -> None:
    assert ghs.consume_targeted_enabled() is False  # 方針の既定
    row = _approved(session, site, ga.CREATE_REGULAR_THREADS_POST, 25)
    _service(session, site).execute(row.id, now=_NOW)
    stock = _stock(session, tmp_path)
    outcome = stock.maintain(now=_NOW, execute=True)
    requested = {r.article_id for r in stock.provider.pending()}
    assert len(outcome["requests_created"]) == 3 and 25 not in requested
    [handoff] = _handoffs(session)
    assert handoff.status == "pending" and handoff.generation_request_id is None


def test_enabled_stock_maintenance_uses_the_request_without_extra_calls(
        session, site, tmp_path, monkeypatch) -> None:  # fmt: skip
    row = _approved(session, site, ga.CREATE_REGULAR_THREADS_POST, 25)
    _service(session, site).execute(row.id, now=_NOW)
    _enable(monkeypatch)
    stock = _stock(session, tmp_path)
    plan = stock.plan(now=_NOW)
    assert plan["stock"]["requests"][0]["targeted_request_id"] == _handoffs(session)[0].id
    outcome = stock.maintain(now=_NOW, execute=True)
    assert len(outcome["requests_created"]) == 3  # 依頼の数は同じ
    [handoff] = _handoffs(session)
    assert handoff.status == "claimed" and handoff.generation_request_id in outcome[
        "requests_created"]  # fmt: skip
    assert 25 in {r.article_id for r in stock.provider.pending()}
    _answer(stock.provider)
    stock.maintain(now=_NOW + timedelta(hours=1), execute=True)
    session.refresh(handoff)
    assert handoff.status == "proposal_created" and handoff.downstream_type == "threads_proposal"
    [pid] = handoff.downstream_ids_json
    proposal = session.get(ThreadsPostProposal, pid)
    assert proposal.source_article_id == 25 and proposal.status == TP_AWAITING_APPROVAL
    assert proposal.approved_at is None
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0
    observed = ghs.GrowthHandoffService(session).observe(handoff)
    assert observed["downstream"][0]["state"] == TP_AWAITING_APPROVAL
    assert observed["external_effect"] is None  # 提案ができた ≠ 承認 ≠ 公開


def test_healthy_stock_leaves_the_request_waiting(session, site, tmp_path, monkeypatch) -> None:
    for aid, angle, seed in ((21, "insight", "a"), (22, "question", "b"), (23, "comparison", "c")):
        _proposal(session, aid, angle=angle, seed=seed)
    row = _approved(session, site, ga.CREATE_REGULAR_THREADS_POST, 25)
    _service(session, site).execute(row.id, now=_NOW)
    _enable(monkeypatch)
    outcome = _stock(session, tmp_path).maintain(now=_NOW, execute=True)
    assert outcome["requests_created"] == []
    assert _handoffs(session)[0].status == "pending"


def test_a_stale_generation_request_releases_the_targeted_request(
        session, site, tmp_path, monkeypatch) -> None:  # fmt: skip
    row = _approved(session, site, ga.CREATE_REGULAR_THREADS_POST, 25)
    _service(session, site).execute(row.id, now=_NOW)
    _enable(monkeypatch)
    stock = _stock(session, tmp_path)
    stock.maintain(now=_NOW, execute=True)
    first = _handoffs(session)[0].generation_request_id
    outcome = stock.maintain(now=_NOW + timedelta(hours=73), execute=True)
    assert first in outcome["stale_requests"]
    [handoff] = _handoffs(session)
    # 答えの無いまま古くなった依頼は pending に戻り、同じ保守の次の依頼で使われる。
    assert handoff.status == "claimed" and handoff.generation_request_id != first


def test_the_worker_does_not_convert_growth_actions() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "app/services/threads_worker_service.py").read_text(encoding="utf-8")
    assert "GrowthActionConversionService" not in source
    assert "GrowthHandoffService" not in source


# == 記事の計画の依頼 =======================================================================
def test_a_new_article_conversion_creates_a_planning_request_and_no_article(session, site):
    row = _approved(session, site, ga.CREATE_NEW_ARTICLE)
    articles_before = session.scalar(select(func.count()).select_from(Article))
    result = _service(session, site).execute(row.id, now=_NOW)
    [handoff] = _handoffs(session)
    assert (handoff.workflow, handoff.status) == ("article_planning", "pending")
    assert handoff.keyword_id == site["keyword"].id
    assert handoff.frozen_json["keyword"]["keyword"] == "RPA 比較"
    assert session.scalar(select(func.count()).select_from(Article)) == articles_before
    assert "no article was created" in result["plan"]["meaning"]


def test_an_existing_article_or_overlap_refuses_the_planning_request(session, site,
                                                                     monkeypatch) -> None:
    row = _approved(session, site, ga.CREATE_NEW_ARTICLE)
    same_text = Keyword(keyword="rpa  比較")  # 正規化すれば同じキーワード
    session.add(same_text)
    session.flush()
    session.add(Article(title="既存", slug="rpa-hikaku", status="planned",
                        keyword_id=same_text.id))
    session.commit()
    _refused(session, lambda: _service(session, site).execute(row.id, now=_NOW),
             "no_existing_article")
    session.execute(text("delete from articles where slug = 'rpa-hikaku'"))
    session.commit()
    monkeypatch.setattr(gcs.GrowthActionConversionService, "_cannibalization",
                        lambda self, keyword: {"ok": False, "detail": "similar to keyword 3"})
    _refused(session, lambda: _service(session, site).execute(row.id, now=_NOW),
             "no_cannibalization: similar to keyword 3")


def test_a_planning_request_is_linked_only_to_a_matching_new_article(session, site) -> None:
    row = _approved(session, site, ga.CREATE_NEW_ARTICLE)
    _service(session, site).execute(row.id, now=_NOW)
    [handoff] = _handoffs(session)
    service = ghs.GrowthHandoffService(session)
    with pytest.raises(ghs.GrowthHandoffError, match="approved article planning"):
        service.materialize(handoff.id, article_id=21, now=_NOW)
    with pytest.raises(ghs.GrowthHandoffError, match="needs a reason"):
        service.decide_planning(handoff.id, approve=False, now=_NOW)
    service.decide_planning(handoff.id, approve=True, now=_NOW + timedelta(minutes=1))
    assert handoff.status == "approved" and handoff.resolved_at is None
    with pytest.raises(ghs.GrowthHandoffError, match="not linked to keyword"):
        service.materialize(handoff.id, article_id=21, now=_NOW)
    old = Article(title="古い", slug="rpa-old", status="planned", keyword_id=site["keyword"].id,
                  created_at=_NOW - timedelta(days=3))
    archived = Article(title="捨てた", slug="rpa-archived", status="archived",
                       keyword_id=site["keyword"].id, created_at=_NOW + timedelta(hours=1))
    new = Article(title="RPA 比較", slug="rpa-hikaku-new", status="planned",
                  keyword_id=site["keyword"].id, created_at=_NOW + timedelta(hours=2))
    session.add_all([old, archived, new])
    session.commit()
    with pytest.raises(ghs.GrowthHandoffError, match="existed before"):
        service.materialize(handoff.id, article_id=old.id, now=_NOW)
    with pytest.raises(ghs.GrowthHandoffError, match="is archived"):
        service.materialize(handoff.id, article_id=archived.id, now=_NOW)
    service.materialize(handoff.id, article_id=new.id, now=_NOW + timedelta(hours=3))
    session.commit()
    observed = service.observe(handoff)
    assert observed["status"] == "materialized"
    assert observed["downstream"][0] == {"type": "article", "id": new.id, "state": "planned",
                                         "published_at": None}
    assert observed["external_effect"] is None  # 計画した ≠ 公開


# == 変更の準備の依頼 (ChangeRequest V2) ===================================================
def test_a_placement_conversion_freezes_the_source_and_needs_the_plan_hash(session, site):
    row = _approved(session, site, ga.REVIEW_AFFILIATE_PLACEMENT, 25)
    plan = _service(session, site).plan(row.id, now=_NOW)["plan"]
    _refused(session, lambda: _service(session, site).execute(row.id, now=_NOW),
             "source_hash_confirmed: pass --expected-source-hash")
    result = _service(session, site).execute(row.id, now=_NOW,
                                             expected_source_hash=plan["source_hash"])
    [handoff] = _handoffs(session)
    assert (handoff.workflow, handoff.change_type) == ("change_preparation",
                                                       "affiliate_placement")
    source = handoff.frozen_json["source"]
    article = session.get(Article, 25)
    assert source["body_hash"] == compute_text_hash(article.body)
    assert source["meta_hash"] == compute_text_hash("")
    assert source["program_ids"] == [] and source["active_target_ids"] == []
    assert "http" not in json.dumps(handoff.frozen_json, ensure_ascii=False)  # URL を入れない
    assert session.scalar(select(func.count()).select_from(ChangeRequest)) == 0
    assert result["side_effects"]["wordpress_writes"] == 0


def test_update_and_snippet_become_typed_preparation_requests(session, site) -> None:
    for action, change_type in ((ga.UPDATE_EXISTING_ARTICLE, "body_update"),
                                (ga.IMPROVE_SEARCH_SNIPPET, "meta_description")):
        row = _approved(session, site, action, 25)
        plan = _service(session, site).plan(row.id, now=_NOW)["plan"]
        _service(session, site).execute(row.id, now=_NOW,
                                        expected_source_hash=plan["source_hash"])
        assert _handoffs(session)[-1].change_type == change_type
    # 同じ記事でも、種類が違えば別の準備 (同じ種類の 2 つ目は断る)。
    assert len(_handoffs(session)) == 2


def test_a_source_change_after_the_plan_is_refused(session, site) -> None:
    row = _approved(session, site, ga.UPDATE_EXISTING_ARTICLE, 25)
    plan = _service(session, site).plan(row.id, now=_NOW)["plan"]
    session.get(Article, 25).body += "\n追記。"
    session.commit()
    _refused(session, lambda: _service(session, site).execute(
        row.id, now=_NOW, expected_source_hash=plan["source_hash"]),
        "the article changed after the plan")  # fmt: skip


def _change_request(session, article_id, *, source_hash, seed="c", status=CR_AWAITING_APPROVAL,
                    created_at=None):
    row = ChangeRequest(source_engine="manual", article_id=article_id, change_type="text_edit",
                        proposal_hash=seed * 64, expected_source_body_hash=source_hash,
                        proposed_body_hash="p" * 64, proposed_body="新しい本文",
                        rationale="人が書いた変更", proposal_json={}, evidence_json={},
                        status=status, idempotency_key=f"manual-{seed}",
                        **({"created_at": created_at} if created_at else {}))  # fmt: skip
    session.add(row)
    session.commit()
    return row


def test_an_open_change_request_refuses_a_preparation(session, site) -> None:
    _change_request(session, 25, source_hash="x" * 64)
    row = _approved(session, site, ga.UPDATE_EXISTING_ARTICLE, 25)
    plan = _service(session, site).plan(row.id, now=_NOW)["plan"]
    _refused(session, lambda: _service(session, site).execute(
        row.id, now=_NOW, expected_source_hash=plan["source_hash"]),
        "no_conflicting_downstream_work")  # fmt: skip


def test_a_preparation_links_only_to_a_change_made_from_the_frozen_source(session, site):
    row = _approved(session, site, ga.UPDATE_EXISTING_ARTICLE, 25)
    plan = _service(session, site).plan(row.id, now=_NOW)["plan"]
    _service(session, site).execute(row.id, now=_NOW, expected_source_hash=plan["source_hash"])
    [handoff] = _handoffs(session)
    body_hash = handoff.frozen_json["source"]["body_hash"]
    service = ghs.GrowthHandoffService(session)
    with pytest.raises(ghs.GrowthHandoffError, match="links to change_request"):
        service.prepare(handoff.id, downstream_type="link_mapping", downstream_id=1, now=_NOW)
    wrong = _change_request(session, 25, source_hash="y" * 64, seed="w",
                            created_at=_NOW + timedelta(hours=1))
    with pytest.raises(ghs.GrowthHandoffError, match="not made from the frozen source"):
        service.prepare(handoff.id, downstream_type="change_request", downstream_id=wrong.id,
                        now=_NOW)
    session.delete(wrong)
    session.commit()
    good = _change_request(session, 25, source_hash=body_hash, seed="g",
                           created_at=_NOW + timedelta(hours=2))
    service.prepare(handoff.id, downstream_type="change_request", downstream_id=good.id,
                    now=_NOW + timedelta(hours=3))
    session.commit()
    observed = service.observe(handoff)
    assert observed["status"] == "prepared"
    assert observed["downstream"][0]["state"] == CR_AWAITING_APPROVAL
    assert observed["external_effect"] is None  # 準備した ≠ 承認 ≠ 適用


def test_a_meta_preparation_does_not_accept_a_change_request(session, site) -> None:
    row = _approved(session, site, ga.IMPROVE_SEARCH_SNIPPET, 25)
    plan = _service(session, site).plan(row.id, now=_NOW)["plan"]
    _service(session, site).execute(row.id, now=_NOW, expected_source_hash=plan["source_hash"])
    [handoff] = _handoffs(session)
    with pytest.raises(ghs.GrowthHandoffError, match="links to editorial_revision"):
        ghs.GrowthHandoffService(session).prepare(handoff.id, downstream_type="change_request",
                                                  downstream_id=1, now=_NOW)


def test_source_drift_is_visible_on_a_pending_preparation(session, site) -> None:
    row = _approved(session, site, ga.REVIEW_AFFILIATE_PLACEMENT, 25)
    plan = _service(session, site).plan(row.id, now=_NOW)["plan"]
    _service(session, site).execute(row.id, now=_NOW, expected_source_hash=plan["source_hash"])
    [handoff] = _handoffs(session)
    service = ghs.GrowthHandoffService(session)
    assert service.observe(handoff)["source_drift"] == []
    session.get(Article, 25).meta_description = "新しいメタ"
    session.commit()
    assert service.observe(handoff)["source_drift"] == ["meta_hash"]


# == 共通 =============================================================================
def test_before_the_migration_nothing_is_written(session, site) -> None:
    row = _approved(session, site, ga.CREATE_REGULAR_THREADS_POST, 25)
    session.execute(text("drop table growth_handoff_requests"))
    session.commit()
    result = _service(session, site).plan(row.id, now=_NOW)
    check = next(c for c in result["checks"] if c["name"] == "handoff_table_ready")
    assert check["ok"] is False and ghs.HANDOFF_REVISION in check["detail"]
    with pytest.raises(gas.GrowthActionError, match=ghs.HANDOFF_REVISION):
        _service(session, site).execute(row.id, now=_NOW)
    assert session.scalar(select(func.count()).select_from(GrowthActionConversion)) == 0
    session.refresh(row)
    assert row.status == "approved"


def test_an_unapproved_action_is_refused(session, site) -> None:
    history = gas.GrowthActionHistory(session)
    history.refresh(_report(site["keyword"].id), gi.CoverageContext(), now=_NOW, execute=True)
    row = next(r for r in history.rows() if r.action_type == ga.CREATE_NEW_ARTICLE)
    _refused(session, lambda: _service(session, site).execute(row.id, now=_NOW),
             "approved_review")


def test_the_cli_shows_and_decides_handoffs(session, site, capsys) -> None:
    from scripts import manage_growth_actions as cli

    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    row = _approved(session, site, ga.CREATE_NEW_ARTICLE)
    _service(session, site).execute(row.id, now=_NOW)
    [handoff] = _handoffs(session)
    assert cli.main(["handoff", "list"], session_factory=_Scoped(), settings=_Settings()) == 0
    assert f"handoff #{handoff.id} article_planning" in capsys.readouterr().out
    assert cli.main(["handoff", "approve-plan", str(handoff.id)], session_factory=_Scoped(),
                    settings=_Settings()) == 0
    assert "PLAN" in capsys.readouterr().out
    session.refresh(handoff)
    assert handoff.status == "pending"  # --execute が無ければ書かない
    assert cli.main(["handoff", "approve-plan", str(handoff.id), "--execute"],
                    session_factory=_Scoped(), settings=_Settings()) == 0
    session.refresh(handoff)
    assert handoff.status == "approved"
    assert cli.main(["handoff", "materialize", str(handoff.id), "--article-id", "21",
                     "--execute"], session_factory=_Scoped(), settings=_Settings()) == 2
    assert "refused: article 21 is not linked" in capsys.readouterr().out
    link = cli.linkage(session, _Settings(), row.id)
    assert link["downstream"][0]["handoff"]["status"] == "approved"
