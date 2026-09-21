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
from app.services.content_queue_service import ContentQueueService
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


# ================================================================ C2.5.1
def test_split_japanese_ideas_match_the_same_catalog_program_as_the_unsplit_form(
    seeded: Session,
) -> None:
    AffiliateProgramRepository(seeded).create(
        name="Minutes Tool", provider="direct", match_terms=["議事録", "AI 議事録"]
    )
    seeded.commit()
    plan = KeywordExpansionService(seeded).plan(
        _CONFIG,
        _RULES,
        [
            IdeaCandidate(keyword="議事録 作成 ツール", cluster="A"),
            IdeaCandidate(keyword="議事 録 作成 ツール", cluster="A"),  # Google Ads の分かち書き
            IdeaCandidate(keyword="ai 議事 録 比較", cluster="A"),
            IdeaCandidate(
                keyword="c rm ツール", cluster="E"
            ),  # 英語 term (CRM) は詰めて match しない
            IdeaCandidate(keyword="c rm", cluster="E"),
        ],
    )
    by = _by(plan)
    for text in ("議事録 作成 ツール", "議事 録 作成 ツール"):
        assert by[text].affiliate.program_names == ("Minutes Tool",), text
        assert by[text].keyword == text  # 表示テキストは Google の原文のまま
    assert by["ai 議事 録 比較"].affiliate.program_names == ("Minutes Tool",)
    assert by["c rm ツール"].affiliate.level == "none"
    assert by["c rm"].affiliate.level == "none"


def test_catalog_spacing_option_is_only_used_by_the_planner_not_by_the_c22_queue(
    seeded: Session,
) -> None:

    AffiliateProgramRepository(seeded).create(
        name="Minutes Tool", provider="direct", match_terms=["議事録"]
    )
    seeded.commit()
    keyword = KeywordRepository(seeded).create(keyword="議事 録 テスト")
    seeded.commit()
    inputs, _articles = ContentQueueService(seeded).load_inputs()
    row = next(k for k in inputs if k.id == keyword.id)
    assert row.affiliate_matches == ()  # C2.2 の既存 keyword の coverage は従来どおり


# ================================================================ C2.5.4
def _aff(plan) -> dict:
    return {d.keyword: d.affiliate for d in plan.decisions}


def test_service_reports_tiers_and_keeps_the_legacy_coverage_fields(seeded: Session) -> None:
    plan = KeywordExpansionService(seeded).plan(
        _CONFIG,
        _RULES,
        [
            IdeaCandidate(keyword="Make 使い方", cluster="C"),
            IdeaCandidate(keyword="CRM おすすめ", cluster="E"),
            IdeaCandidate(keyword="make sure", cluster="C"),
            IdeaCandidate(keyword="Zapier 代替", cluster="C"),
            IdeaCandidate(keyword="ハブスポット とは", cluster="E"),
        ],
    )
    aff = _aff(plan)
    make = aff["Make 使い方"]
    assert (make.level, make.program_names) == ("single", ("Make",))
    assert make.strong_program_names == ("Make",) and make.weak_program_names == ()
    assert make.no_strong_affiliate_match is False
    crm = aff["CRM おすすめ"]
    assert (crm.level, crm.program_names) == ("multiple", ("HubSpot", "Pipedrive"))
    assert (crm.strong_program_count, crm.weak_program_count) == (0, 2)
    assert crm.no_strong_affiliate_match is True
    idiom = aff["make sure"]  # legacy の covered は従来どおり (Make に match)
    assert (idiom.level, idiom.program_names) == ("single", ("Make",))
    assert idiom.strong_program_count == 0 and idiom.weak_program_names == ("Make",)
    none = aff["Zapier 代替"]
    assert none.level == "none" and none.no_strong_affiliate_match is True
    assert (none.strong_program_count, none.weak_program_count) == (0, 0)
    alias = aff["ハブスポット とは"]  # 明示 alias: legacy の covered は広げない
    assert (alias.level, alias.program_count, alias.program_names) == ("none", 0, ())
    assert alias.strong_program_names == ("HubSpot",) and alias.no_strong_affiliate_match is False
    assert alias.alias_only_strong_program_names == ("HubSpot",)  # legacy の covered ではないと明示
    assert aff["Make 使い方"].alias_only_strong_program_names == ()


def test_summary_reports_keep_tier_counts_and_the_japanese_spacing_mismatch(
    seeded: Session,
) -> None:
    AffiliateProgramRepository(seeded).create(
        name="Minutes Tool", provider="direct", match_terms=["議事録"]
    )
    seeded.commit()
    plan = KeywordExpansionService(seeded).plan(
        _CONFIG,
        _RULES,
        [
            # spacing option でだけ covered
            IdeaCandidate(keyword="議事 録 作成 ツール", cluster="A"),
            IdeaCandidate(keyword="議事録 自動 作成", cluster="A"),  # legacy でも covered
            IdeaCandidate(keyword="Make 使い方", cluster="C"),
        ],
    )
    s = plan.summary
    assert s["japanese_spacing_only_coverage"] == 1
    assert s["keep_japanese_spacing_only_coverage"] == sum(
        1 for d in plan.decisions if d.decision == "keep" and d.keyword == "議事 録 作成 ツール"
    )
    keeps = [d for d in plan.decisions if d.decision == "keep"]
    assert s["keep_affiliate_strong"] == sum(1 for d in keeps if d.affiliate.strong_program_count)
    assert s["keep_no_strong_affiliate_match"] == sum(
        1 for d in keeps if d.affiliate.no_strong_affiliate_match
    )


def test_tiering_does_not_change_any_expansion_decision_through_the_service(
    seeded: Session,
) -> None:
    candidates = [IdeaCandidate(keyword=k, cluster=c) for c, k, _, _ in CASES]
    plan = KeywordExpansionService(seeded).plan(_CONFIG, _RULES, candidates)
    assert (plan.summary["keep"], plan.summary["merge"], plan.summary["reject"]) == (67, 38, 30)
    got = _by(plan)
    for _cluster, keyword, decision, target in CASES:
        assert got[keyword].decision == decision, keyword
        if decision == "merge":
            assert got[keyword].target == target, keyword


def test_json_output_carries_additive_tier_metadata_and_the_legacy_keys(
    seeded: Session, tmp_path: Path, capsys
) -> None:
    ideas = _write(tmp_path, {"candidates": {"C": ["Make 使い方"], "E": ["CRM おすすめ"]}})
    assert run(ideas_file=ideas, output_format="json", session_factory=_factory(seeded)) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    by = {d["keyword"]: d for d in payload["decisions"]}
    aff = by["Make 使い方"]["affiliate"]
    assert {"level", "program_count", "providers", "program_names"} <= set(aff)
    assert {
        "strong_program_count",
        "weak_program_count",
        "strong_program_names",
        "weak_program_names",
        "no_strong_affiliate_match",
    } <= set(aff)
    assert aff["strong_program_names"] == ["Make"] and aff["no_strong_affiliate_match"] is False
    crm = by["CRM おすすめ"]["affiliate"]
    assert crm["weak_program_names"] == ["HubSpot", "Pipedrive"]
    assert crm["strong_program_count"] == 0 and crm["no_strong_affiliate_match"] is True
    assert {
        "keep_affiliate_strong",
        "keep_affiliate_weak_only",
        "keep_no_strong_affiliate_match",
        "japanese_spacing_only_coverage",
        "keep_japanese_spacing_only_coverage",
    } <= set(payload["summary"])
    assert _TRACKING not in out and "example.invalid" not in out


def test_table_output_shows_tiers_after_the_legacy_affiliate_token(
    seeded: Session, tmp_path: Path, capsys
) -> None:
    ideas = _write(tmp_path, {"candidates": {"C": ["Make 使い方"], "E": ["CRM おすすめ"]}})
    assert run(ideas_file=ideas, session_factory=_factory(seeded)) == EXIT_OK
    out = capsys.readouterr().out
    assert "affiliate=multiple(2) tier=strong:0/weak:2" in out
    assert "affiliate=single(1) tier=strong:1/weak:0" in out
    assert "affiliate tiers (keeps, report only): strong=1 weak_only=1 no_strong=1" in out
    assert "japanese-spacing" not in out.lower()  # 不一致が無ければ note は出さない


def test_table_output_notes_the_spacing_mismatch_only_when_present(
    seeded: Session, tmp_path: Path, capsys
) -> None:
    AffiliateProgramRepository(seeded).create(
        name="Minutes Tool", provider="direct", match_terms=["議事録"]
    )
    seeded.commit()
    ideas = _write(tmp_path, {"candidates": {"A": ["議事 録 作成 ツール"]}})
    assert run(ideas_file=ideas, session_factory=_factory(seeded)) == EXIT_OK
    out = capsys.readouterr().out
    assert "covered only through Japanese-spacing matching" in out
    assert "scoring / article planning / the C2.2 queue use the legacy matcher" in out
