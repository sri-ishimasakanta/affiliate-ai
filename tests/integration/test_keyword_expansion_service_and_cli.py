"""KeywordExpansionService / scripts/plan_keyword_expansion.py — DB を read-only で読む planner。

DB は in-memory の test engine のみ (production DB には触れない)。Google / HTTP / LLM は 0。
"""

from __future__ import annotations

import contextlib
import json
import socket
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

import scripts.discover_keyword_ideas as discover
import scripts.plan_keyword_expansion as planner
from app.article.cluster_plan import load_cluster_config
from app.article.keyword_expansion import IdeaCandidate, load_expansion_rules
from app.keyword.providers.google_ads_ideas import GoogleAdsKeywordIdeaProvider
from app.models import AffiliateProgram, Article, Keyword, KeywordScore
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_repository import ArticleRepository
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.keyword_score_repository import KeywordScoreRepository
from app.services.keyword_expansion_service import KeywordExpansionService
from scripts.plan_keyword_expansion import (
    DEFAULT_CLUSTER_CONFIG,
    DEFAULT_RULES,
    EXIT_CONFIG,
    EXIT_INPUT,
    EXIT_OK,
    load_ideas,
    main,
    run,
)
from tests.support.c22_pool import POOL_30
from tests.support.c23_expansion_cases import CASES
from tests.support.google_ads_fakes import dummy_google_ads_settings
from tests.support.google_ads_idea_fakes import (
    FakeIdeasClient,
    FakeIdeasPager,
    idea_row,
    idea_row_with_metrics,
)

_TRACKING = "https://track.example.invalid/secret-tracking-token"
_CONFIG = load_cluster_config(DEFAULT_CLUSTER_CONFIG)
_RULES = load_expansion_rules(DEFAULT_RULES)


@pytest.fixture
def seeded(session: Session) -> Session:
    """production と同じ 30 keyword + article #1 (published) + catalog を in-memory DB に作る。"""

    keywords = KeywordRepository(session)
    scores = KeywordScoreRepository(session)
    for kid, text, score, _missing in POOL_30:
        row = keywords.create(keyword=text)
        assert row.id == kid  # id 順に採番される
        row.status = "analyzed"
        if score is not None:
            scores.create(
                keyword_id=row.id,
                search_demand=10.0,
                commercial_intent=20.0,
                affiliate_opportunity=30.0,
                competition_ease=40.0,
                trend=50.0,
                originality=60.0,
                site_relevance=70.0,
                total_score=score,
                score_version="v1",
                input_source="signals",
            )
    article = ArticleRepository(session).create(title="pillar", slug="pillar", keyword_id=21)
    article.status = "published"

    programs = AffiliateProgramRepository(session)
    programs.create(name="Make", provider="make", match_terms=["Make"], tracking_url=_TRACKING)
    programs.create(
        name="HubSpot",
        provider="impact",
        match_terms=["HubSpot", "CRM"],
        tracking_url=_TRACKING,
        landing_page_url="https://www.example.invalid/hubspot",
    )
    programs.create(name="Pipedrive", provider="direct", match_terms=["CRM"])
    programs.create(
        name="Paused CRM Tool",
        provider="direct",
        match_terms=["CRM"],
        status=AffiliateProgramStatus.PAUSED,
    )
    session.commit()
    return session


def _factory(session: Session):
    return lambda: contextlib.nullcontext(session)


def _no_session():
    raise AssertionError("the DB must not be opened")


def _counts(session: Session) -> dict[str, int]:
    return {
        model.__tablename__: session.scalar(select(func.count()).select_from(model))
        for model in (Keyword, KeywordScore, Article, AffiliateProgram)
    }


def _write(tmp_path: Path, payload, name: str = "ideas.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _by(plan) -> dict:
    return {d.keyword: d for d in plan.decisions}


# ================================================================ service
def test_service_reproduces_the_c23_decisions_against_the_db_pool_articles_and_queue(
    seeded: Session,
) -> None:
    candidates = [IdeaCandidate(keyword=k, cluster=c) for c, k, _, _ in CASES]
    plan = KeywordExpansionService(seeded).plan(_CONFIG, _RULES, candidates)
    got = _by(plan)
    for _cluster, keyword, decision, target in CASES:
        assert got[keyword].decision == decision, keyword
        if decision == "merge":
            assert got[keyword].target == target, keyword
    assert (plan.summary["keep"], plan.summary["merge"], plan.summary["reject"]) == (67, 38, 30)
    # article #1 は DB から検出され、その intent の候補は article に吸収される
    assert got["業務効率化 ツール 中小企業"].target_kind == "article"
    assert got["業務効率化 ツール 中小企業"].target_article_id == 1


def test_affiliate_coverage_comes_from_the_live_db_catalog(seeded: Session) -> None:
    plan = KeywordExpansionService(seeded).plan(
        _CONFIG,
        _RULES,
        [
            IdeaCandidate(keyword="CRM おすすめ", cluster="E"),
            IdeaCandidate(keyword="Make 使い方", cluster="C"),
            IdeaCandidate(keyword="Zapier 代替", cluster="C"),
        ],
    )
    by = _by(plan)
    crm = by["CRM おすすめ"].affiliate
    assert crm.level == "multiple" and crm.program_names == ("HubSpot", "Pipedrive")
    assert crm.providers == ("direct", "impact")
    assert "Paused CRM Tool" not in crm.program_names  # paused は catalog に含めない
    assert by["Make 使い方"].affiliate.program_names == ("Make",)
    assert by["Zapier 代替"].affiliate.level == "none"


def test_metrics_are_reported_when_available_otherwise_not_available(seeded: Session) -> None:
    plan = KeywordExpansionService(seeded).plan(
        _CONFIG,
        _RULES,
        [
            IdeaCandidate(keyword="CRM おすすめ", metrics={"avg_monthly_searches": 320}),
            IdeaCandidate(keyword="HubSpot 料金"),
        ],
    )
    by = _by(plan)
    assert by["CRM おすすめ"].metrics == {"avg_monthly_searches": 320}
    assert by["HubSpot 料金"].metrics == "not_available"


def test_service_is_read_only_select_only_no_commit_and_inserts_no_keywords(
    engine: Engine, seeded: Session
) -> None:
    before = _counts(seeded)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(_conn, _cursor, statement, _params, _context, _executemany) -> None:
        statements.append(statement)

    commits: list[int] = []
    event.listen(seeded, "after_commit", lambda _s: commits.append(1))
    try:
        candidates = [IdeaCandidate(keyword=k, cluster=c) for c, k, _, _ in CASES]
        KeywordExpansionService(seeded).plan(_CONFIG, _RULES, candidates)
    finally:
        event.remove(engine, "before_cursor_execute", _spy)

    assert statements
    assert all(s.lstrip().split(None, 1)[0].upper() == "SELECT" for s in statements)
    assert commits == []
    assert not seeded.new and not seeded.dirty and not seeded.deleted
    assert _counts(seeded) == before  # keyword は 1 件も増えない


def test_service_makes_no_http_or_socket_calls(seeded: Session, monkeypatch) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(httpx.Client, "send", _boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)
    plan = KeywordExpansionService(seeded).plan(
        _CONFIG, _RULES, [IdeaCandidate(keyword="ClickUp 料金")]
    )
    assert plan.summary["keep"] == 1


# ==================================================================== CLI
def test_ideas_file_loading_accepts_discover_output_and_manual_candidates(tmp_path: Path) -> None:
    discover_shape = _write(
        tmp_path,
        {
            "ideas": [
                {"cluster": "E", "keyword": "crm おすすめ", "metrics": {"avg_monthly_searches": 5}},
                {"cluster": "E", "keyword": "crm 比較", "metrics": None},
            ]
        },
    )
    loaded = load_ideas(discover_shape)
    assert [(c.keyword, c.cluster, c.metrics) for c in loaded] == [
        ("crm おすすめ", "E", {"avg_monthly_searches": 5}),
        ("crm 比較", "E", None),
    ]
    manual = _write(tmp_path, {"candidates": {"B": ["タスク管理 ツール おすすめ"]}}, "manual.json")
    assert load_ideas(manual)[0].cluster == "B"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"ideas": "x"},
        {"ideas": [1]},
        {"ideas": [{"keyword": ""}]},
        {"ideas": [{"keyword": "a", "cluster": "Z"}]},
        {"ideas": [{"keyword": "a", "metrics": "x"}]},
        {"candidates": {"B": "x"}},
        {"candidates": []},
    ],
)
def test_invalid_ideas_files_fail_before_the_db_is_opened(tmp_path: Path, payload, capsys) -> None:
    path = _write(tmp_path, payload)
    assert run(ideas_file=path, session_factory=_no_session) == EXIT_INPUT
    assert "INPUT ERROR" in capsys.readouterr().out


def test_missing_or_broken_ideas_file_and_config_fail_before_the_db(tmp_path: Path, capsys) -> None:
    assert run(ideas_file=tmp_path / "nope.json", session_factory=_no_session) == EXIT_INPUT
    broken = tmp_path / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    assert run(ideas_file=broken, session_factory=_no_session) == EXIT_INPUT

    ideas = _write(tmp_path, {"candidates": {"B": ["x"]}})
    assert (
        run(ideas_file=ideas, rules_path=tmp_path / "no-rules.json", session_factory=_no_session)
        == EXIT_CONFIG
    )
    assert (
        run(
            ideas_file=ideas,
            cluster_config=tmp_path / "no-config.json",
            session_factory=_no_session,
        )
        == EXIT_CONFIG
    )
    assert "CONFIG ERROR" in capsys.readouterr().out


def test_unknown_configured_keyword_is_a_config_error_and_writes_nothing(
    session: Session, tmp_path: Path, capsys
) -> None:
    ideas = _write(tmp_path, {"candidates": {"B": ["x"]}})
    before = _counts(session)
    assert run(ideas_file=ideas, session_factory=_factory(session)) == EXIT_CONFIG  # DB が空
    assert "unknown keyword" in capsys.readouterr().out
    assert _counts(session) == before


def test_table_output_lists_keep_merge_reject_with_reasons_and_no_secrets(
    seeded: Session, tmp_path: Path, capsys
) -> None:
    ideas = _write(
        tmp_path,
        {
            "ideas": [
                {
                    "cluster": "E",
                    "keyword": "crm おすすめ",
                    "metrics": {"avg_monthly_searches": 880, "competition": "HIGH"},
                },
                {"cluster": "E", "keyword": "顧客管理 ツール 比較", "metrics": None},
                {"cluster": "C", "keyword": "Zapier 使い方", "metrics": None},
                {"cluster": "B", "keyword": "ClickUp 料金", "metrics": None},
                {"cluster": "B", "keyword": "ClickUp 代替", "metrics": None},
            ]
        },
    )
    assert run(ideas_file=ideas, session_factory=_factory(seeded)) == EXIT_OK
    out = capsys.readouterr().out
    assert "READ-ONLY: no Google call, no DB write, no insert" in out
    assert "keep=3 merge=1 reject=1" in out
    assert "E | crm おすすめ | distinct_intent" in out
    assert "avg=880 competition=HIGH" in out and "metrics=not_available" in out
    assert "affiliate=multiple(2)" in out  # live catalog: HubSpot + Pipedrive
    assert (
        "E | 顧客管理 ツール 比較 | intent_overlap_with_candidate -> crm おすすめ [candidate]"
        in out
    )
    assert (
        "C | Zapier 使い方 | no_catalog_competitor_tutorial (rule no-catalog-competitor-tutorial)"
        in out
    )
    assert _TRACKING not in out and "example.invalid" not in out


def test_json_output_is_valid_deterministic_and_secret_free(
    seeded: Session, tmp_path: Path, capsys
) -> None:
    ideas = _write(
        tmp_path, {"candidates": {"E": ["CRM おすすめ", "SFA 比較"], "B": ["ClickUp 料金"]}}
    )
    outputs = []
    for _ in range(2):
        assert run(ideas_file=ideas, output_format="json", session_factory=_factory(seeded)) == 0
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1]
    payload = json.loads(outputs[0])
    assert payload["summary"]["keep"] == 2 and payload["summary"]["merge"] == 1
    assert all(d["metrics"] == "not_available" for d in payload["decisions"])
    assert _TRACKING not in outputs[0] and "example.invalid" not in outputs[0]


def test_planner_run_performs_zero_writes_zero_http_and_never_inserts_keywords(
    engine: Engine, seeded: Session, tmp_path: Path, monkeypatch
) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("HTTP is forbidden")

    monkeypatch.setattr(httpx.Client, "send", _boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", _boom)
    ideas = _write(tmp_path, {"candidates": {"B": ["ClickUp 料金", "ClickUp 代替"]}})
    before = _counts(seeded)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(_conn, _cursor, statement, _params, _context, _executemany) -> None:
        statements.append(statement)

    try:
        assert run(ideas_file=ideas, session_factory=_factory(seeded)) == EXIT_OK
    finally:
        event.remove(engine, "before_cursor_execute", _spy)

    assert statements and all(s.lstrip().split(None, 1)[0].upper() == "SELECT" for s in statements)
    assert _counts(seeded) == before
    assert not seeded.new and not seeded.dirty and not seeded.deleted


def test_main_requires_an_ideas_file_and_rejects_bad_flags(monkeypatch, capsys) -> None:
    monkeypatch.setattr(planner, "SessionLocal", _no_session)
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        main(["--ideas-file", "x.json", "--format", "xml"])
    assert main(["--ideas-file", "does-not-exist.json"]) == EXIT_INPUT
    assert "INPUT ERROR" in capsys.readouterr().out


# ================================================= discover -> planner chain (fake client)
def test_discover_output_feeds_the_planner_with_metrics_end_to_end(
    seeded: Session, tmp_path: Path, capsys
) -> None:
    pager = FakeIdeasPager(
        [
            idea_row_with_metrics("crm おすすめ", avg_monthly_searches=880, competition="HIGH"),
            idea_row("顧客管理 ツール 比較"),
            idea_row("Zapier 使い方"),
        ]
    )
    client = FakeIdeasClient(response=pager)
    ideas_file = tmp_path / "ideas.json"
    code = discover.run(
        execute=True,
        clusters=["E"],
        output=ideas_file,
        settings=dummy_google_ads_settings(),
        provider_factory=lambda s, n: GoogleAdsKeywordIdeaProvider(
            s, client=client, max_requests=n
        ),
    )
    assert code == 0 and client.calls == 1
    capsys.readouterr()

    assert run(ideas_file=ideas_file, output_format="json", session_factory=_factory(seeded)) == 0
    payload = json.loads(capsys.readouterr().out)
    by = {d["keyword"]: d for d in payload["decisions"]}
    assert by["crm おすすめ"]["decision"] == "keep"
    assert by["crm おすすめ"]["metrics"]["avg_monthly_searches"] == 880
    assert by["顧客管理 ツール 比較"]["target"] == "crm おすすめ"
    assert by["zapier 使い方"]["decision"] == "reject"
    assert by["顧客管理 ツール 比較"]["metrics"] == "not_available"


def test_google_ads_spaced_japanese_ideas_dedupe_against_db_keywords_and_keep_original_text(
    seeded: Session,
) -> None:
    plan = KeywordExpansionService(seeded).plan(
        _CONFIG,
        _RULES,
        [
            IdeaCandidate(keyword="ai 議事 録 おすすめ", cluster="A"),  # Google の分かち書き
            IdeaCandidate(keyword="make料金", cluster="C"),
            IdeaCandidate(keyword="タスク 管理 ツール おすすめ", cluster="B"),
        ],
    )
    by = _by(plan)
    assert by["ai 議事 録 おすすめ"].reason_code == "duplicate_existing_keyword"
    assert by["ai 議事 録 おすすめ"].target == "AI 議事録 おすすめ"
    assert by["make料金"].reason_code == "duplicate_existing_keyword"
    assert by["タスク 管理 ツール おすすめ"].decision == "keep"  # 原文のまま
