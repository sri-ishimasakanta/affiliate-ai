"""成長の証拠と行動の候補を DB から作る (C9、**読むだけ**)。

pin する契約:

- DB を 1 行も変えない。外に問い合わせない (HTTP・Threads・OpenAI)。メールを送らない。
- 既存のエンジン (C6 の SEO・C7 の収益) の候補と成熟度をそのまま使う。
- 信頼できる計測開始 (2026-09-23) より前のクリックは読者の行動に使わない (数は残す)。
- 報酬はプログラム単位のまま (記事の収益は None)。
- 公開前の記事には候補を出さない。データの無い公開済みの記事は「待つ」。記事の無い
  キーワードは新しい記事の候補。
- CLI は標準出力だけ (ファイルを書かない)。worker にはまだ登録しない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.growth import analysis as ga
from app.models import (
    AffiliateClickImportRun,
    AffiliateCommissionFact,
    AffiliateCommissionImportRun,
    AffiliateLinkTarget,
    AffiliateOutboundClick,
    AffiliateProgram,
    Article,
    ArticleLinkSubstitutionMapping,
    Keyword,
    KeywordScore,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
)
from app.services.growth_opportunity_service import GrowthOpportunityService, filter_candidates

_BASE = "https://bizfluxlab.com"
_PROPERTY = "sc-domain:bizfluxlab.com"
_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class _Settings:
    wordpress_base_url = _BASE
    search_console_property_uri = _PROPERTY
    search_console_credentials_file = None
    ga4_property_id = None  # GA4 は未設定 (欠測として扱う)


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    import httpx

    from app.services import threads_openai_provider
    from app.social.threads import service as threads_service

    def refuse(*_a, **_k):
        raise AssertionError("the growth analysis must not call anything external")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(threads_service.ThreadsService, "__init__", refuse)
    monkeypatch.setattr(threads_openai_provider.OpenAIResponsesClient, "__init__", refuse)


def _article(session, aid, slug, *, days_old, mode="affiliate", status="published"):
    session.add(Article(
        id=aid, title=slug, slug=slug, body="本文", status=status,
        published_url=f"{_BASE}/{slug}/" if status == "published" else None,
        published_at=_NOW - timedelta(days=days_old) if status == "published" else None,
        article_type="how_to", monetization_mode=mode,
    ))  # fmt: skip
    session.commit()


def _gsc(session, slug, *, clicks, impressions, position):
    run = SearchConsoleImportRun(property_uri=_PROPERTY,
                                 start_date=(_NOW - timedelta(days=28)).date(),
                                 end_date=(_NOW - timedelta(days=2)).date(), status="succeeded",
                                 dimensions_json='[["date","page"]]',
                                 import_identity_hash="a" * 64,
                                 finished_at=_NOW - timedelta(hours=6))  # fmt: skip
    session.add(run)
    session.commit()
    session.add(SearchConsolePageDaily(
        property_uri=_PROPERTY, metric_date=(_NOW - timedelta(days=3)).date(),
        page=f"{_BASE}/{slug}/", clicks=clicks, impressions=impressions,
        ctr=clicks / impressions, position=position, source_import_run_id=run.id))
    session.commit()


def _monetize(session, aid, token="tok1"):
    program = AffiliateProgram(name="Make", status="active", tracking_url="https://track.test/x")
    session.add(program)
    session.commit()
    session.execute(text("insert into article_affiliate_programs (article_id, "
                         "affiliate_program_id, is_primary) values (:a, :p, 1)"),
                    {"a": aid, "p": program.id})  # fmt: skip
    target = AffiliateLinkTarget(token=token, article_id=aid, affiliate_program_id=program.id,
                                 destination_url="https://www.make.com/en/register",
                                 destination_host="www.make.com", status="active",
                                 link_identity_hash=token.ljust(64, "0"))  # fmt: skip
    session.add(target)
    session.commit()
    session.add(ArticleLinkSubstitutionMapping(
        article_id=aid, affiliate_link_target_id=target.id,
        original_href="https://www.make.com/en/register", occurrence_identity_hash="d" * 64,
        status="active", approved_at=_NOW - timedelta(days=30)))
    session.commit()
    return program


def _clicks(session, token, moments):
    run = AffiliateClickImportRun(status="succeeded", requested_since_id=0, requested_limit=100,
                                  finished_at=_NOW - timedelta(hours=5))  # fmt: skip
    session.add(run)
    session.commit()
    for i, when in enumerate(moments, 1):
        session.add(AffiliateOutboundClick(source_click_id=i, token=token, clicked_at=when,
                                           source_import_run_id=run.id))  # fmt: skip
    session.commit()


def _commission(session, program_id):
    run = AffiliateCommissionImportRun(provider="make", affiliate_program_id=program_id,
                                       status="succeeded",
                                       requested_date_from=(_NOW - timedelta(days=5)).date(),
                                       requested_date_to=(_NOW - timedelta(days=1)).date(),
                                       finished_at=_NOW - timedelta(hours=4))  # fmt: skip
    session.add(run)
    session.commit()
    session.add(AffiliateCommissionFact(
        affiliate_program_id=program_id, provider="make", source_commission_id="c1",
        event_type="sale", provider_status="approved", commission_amount=Decimal("12.50"),
        currency="USD", occurred_at=_NOW - timedelta(days=2), first_seen_at=_NOW,
        last_seen_at=_NOW, source_import_run_id=run.id))
    session.commit()


def _keyword(session, word, total, *, commercial=50.0):
    keyword = Keyword(keyword=word, status="analyzed")
    session.add(keyword)
    session.commit()
    session.add(KeywordScore(
        keyword_id=keyword.id, search_demand=50, commercial_intent=commercial,
        affiliate_opportunity=commercial, competition_ease=50, trend=50, originality=50,
        site_relevance=50, total_score=total, score_version="v1", input_source="signals",
        created_at=_NOW - timedelta(days=5)))
    session.commit()
    return keyword


@pytest.fixture
def seeded(session: Session):
    _article(session, 1, "mature", days_old=120)
    _gsc(session, "mature", clicks=2, impressions=1000, position=6.0)
    program = _monetize(session, 1)
    dirty = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)  # 信頼できる計測開始より前
    clean = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
    _clicks(session, "tok1", [dirty, dirty + timedelta(minutes=3), clean])
    _commission(session, program.id)
    _article(session, 2, "draft", days_old=0, status="drafting")
    _article(session, 3, "fresh", days_old=3, mode="supporting")
    k1 = _keyword(session, "rpa 比較", 80.0, commercial=80.0)
    k2 = _keyword(session, "ai 議事録", 40.0, commercial=20.0)
    return {"k1": k1.id, "k2": k2.id, "program": program.id}


def _tables(session) -> dict:
    names = [r[0] for r in session.execute(text(
        "select name from sqlite_master where type='table'"))]  # fmt: skip
    return {n: session.execute(text(f'select count(*) from "{n}"')).scalar() for n in names}


def _report(session):
    return GrowthOpportunityService(session, settings=_Settings()).evaluate_growth_opportunities(
        _NOW)  # fmt: skip


def _evidence(report, subject):
    return next(e for e in report["evidence"] if e["subject_id"] == subject)


def _candidate(report, subject, action):
    return next(c for c in report["candidates"]
                if c["subject_id"] == subject and c["action_type"] == action)  # fmt: skip


def test_the_report_integrates_sources_and_changes_nothing(session, seeded) -> None:
    before = _tables(session)
    report = _report(session)
    assert _tables(session) == before
    assert not session.new and not session.dirty
    assert report["read_only"] is True and report["side_effects"]["db_writes"] == 0
    assert report["freshness"]["ga4"] == ga.UNAVAILABLE
    mature = _evidence(report, "article:1")
    assert mature["sources"]["seo"]["state"] == ga.USABLE
    assert mature["sources"]["seo"]["metrics"]["impressions"] == 1000
    assert mature["sources"]["ga4"]["state"] == ga.UNAVAILABLE
    assert mature["sources"]["ga4"]["metrics"]["sessions"] is None  # 0 にしない
    assert mature["sources"]["threads"]["state"] == ga.UNAVAILABLE


def test_instrumentation_clicks_are_excluded_but_counted(session, seeded) -> None:
    report = _report(session)
    aff = _evidence(report, "article:1")["sources"]["affiliate"]["metrics"]
    assert (aff["clean_clicks"], aff["excluded_instrumentation_clicks"], aff["raw_clicks"]) == (
        1, 2, 3)  # fmt: skip
    assert report["clicks"]["excluded_instrumentation"] == 2
    interest = _candidate(report, "article:1", ga.REVIEW_AFFILIATE_PLACEMENT)
    assert ga.AFFILIATE_INTEREST in interest["patterns"]
    assert any("excluded from reader behaviour" in w for w in report["warnings"])


def test_commissions_stay_program_level(session, seeded) -> None:
    report = _report(session)
    commission = _evidence(report, "article:1")["sources"]["commission"]
    assert commission["metrics"]["article_level_revenue"] is None
    assert commission["state"] == ga.UNAVAILABLE
    assert [p["affiliate_program_id"] for p in report["program_commissions"]] == [
        seeded["program"]]  # fmt: skip
    assert "commission" not in json.dumps([c["evidence"] for c in report["candidates"]])


def test_candidates_follow_the_existing_engines_and_the_article_state(session, seeded) -> None:
    report = _report(session)
    snippet = _candidate(report, "article:1", ga.IMPROVE_SEARCH_SNIPPET)  # C6 CTR_IMPROVEMENT
    assert snippet["evidence"][0]["source_engine"] == "seo"
    assert snippet["requires_human_approval"] and snippet["external_write_required"] == "wordpress"
    # 公開前の記事は証拠の行だけ。
    assert _evidence(report, "article:2")["article"]["status"] == "drafting"
    assert not [c for c in report["candidates"] if c["subject_id"] == "article:2"]
    # データの無い新しい記事は待つ (と、Threads での紹介の候補)。
    fresh = {c["action_type"] for c in report["candidates"] if c["subject_id"] == "article:3"}
    assert ga.WAIT_FOR_MORE_DATA in fresh and ga.CREATE_REGULAR_THREADS_POST in fresh
    # 記事の無いキーワード: 相対の位置で機会の大きさが決まる。
    k1 = _candidate(report, f"keyword:{seeded['k1']}", ga.CREATE_NEW_ARTICLE)
    k2 = _candidate(report, f"keyword:{seeded['k2']}", ga.CREATE_NEW_ARTICLE)
    assert k1["priority"]["potential_opportunity"]["rank"] > (
        k2["priority"]["potential_opportunity"]["rank"])  # fmt: skip
    assert report["summary"]["candidate_counts_by_action"][ga.CREATE_NEW_ARTICLE] == 2


def test_the_report_is_deterministic_and_worker_ready(session, seeded) -> None:
    a, b = _report(session), _report(session)
    assert a["fingerprint"] == b["fingerprint"]
    assert a["candidates"] == b["candidates"]
    assert a["next_evaluation_at"] == (_NOW + timedelta(hours=24)).isoformat()
    from app.services.threads_worker_service import ThreadsWorkerService
    from app.social.threads.worker import SUBSYSTEM_GROWTH_OPPORTUNITY, SUBSYSTEM_ORDER

    assert SUBSYSTEM_GROWTH_OPPORTUNITY in SUBSYSTEM_ORDER
    # 登録はするが、既定は無効 (本番ではまだ動かさない)。
    assert ThreadsWorkerService.GROWTH_OPPORTUNITY_DEFAULTS["enabled"] is False


def test_filters_narrow_the_display(session, seeded) -> None:
    report = _report(session)
    only = filter_candidates(report, article_id=1)
    assert only and {c["article_id"] for c in only} == {1}
    assert {c["action_type"] for c in filter_candidates(
        report, action_type=ga.CREATE_NEW_ARTICLE)} == {ga.CREATE_NEW_ARTICLE}  # fmt: skip
    assert {c["article_id"] for c in filter_candidates(report, monetized_only=True)} == {1}
    assert all(c["article_id"] == 3 for c in filter_candidates(report, max_age_days=10))


def test_an_empty_database_is_all_missing(session) -> None:
    report = _report(session)
    assert report["summary"]["article_count"] == 0
    assert report["candidates"] == [] or all(
        c["subject_type"] == "site" for c in report["candidates"])  # fmt: skip
    assert report["freshness"]["search_console"] == ga.UNAVAILABLE


def _scoped(session):
    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    return _Scoped()


def test_the_cli_prints_only(session, seeded, tmp_path, capsys, monkeypatch) -> None:
    from scripts.analyze_growth_opportunities import main

    monkeypatch.chdir(tmp_path)
    before = _tables(session)
    kw = {"session_factory": _scoped(session), "settings": _Settings()}
    assert main(["--as-of", _NOW.isoformat()], **kw) == 0
    out = capsys.readouterr().out
    assert "## 1. Summary" in out and "## 3. Action candidates" in out
    assert "## 4. Data quality / missing data" in out
    assert main(["--as-of", _NOW.isoformat(), "--format", "json", "--article-id", "1"], **kw) == 0
    out = capsys.readouterr().out
    payload = json.loads(out[: out.rindex("read-only:")])
    assert {e["article_id"] for e in payload["evidence"]} == {1}
    assert {c["article_id"] for c in payload["candidates"]} == {1}
    assert "files written = 0" in out
    assert list(tmp_path.iterdir()) == []
    assert _tables(session) == before
    assert session.scalar(select(func.count()).select_from(Article)) == 3
