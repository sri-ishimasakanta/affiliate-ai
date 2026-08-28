"""scripts/run_keyword_analysis.py の CLI workflow テスト (実 Google Ads 通信なし)。"""

import csv
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.keyword.providers.google_ads import (
    GoogleAdsKeywordMetrics,
    MonthlySearchVolume,
)
from app.keyword.schemas import KeywordSignalCreate
from app.models import Keyword
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_analysis_service import KeywordAnalysisService
from app.services.keyword_metrics_collection_service import KeywordMetricsCollectionService
from app.services.keyword_signal_service import KeywordSignalService
from scripts import run_keyword_analysis as cli
from tests.support.google_ads_fakes import (
    FakeGoogleAdsProvider,
    dummy_google_ads_settings,
)

_ALL7 = (
    "search_demand",
    "commercial_intent",
    "trend",
    "site_relevance",
    "affiliate_opportunity",
    "originality",
    "competition_ease",
)


def _metrics(keyword: str, *, avg: int = 1000) -> GoogleAdsKeywordMetrics:
    volumes = tuple(
        MonthlySearchVolume(year=2025, month=i + 1, monthly_searches=avg + i * 10)
        for i in range(8)
    )
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=avg,
        monthly_search_volumes=volumes,
        competition="HIGH",
        competition_index=60,
        low_top_of_page_bid_micros=120_000_000,
        high_top_of_page_bid_micros=700_000_000,
    )


class _RecordingProvider:
    """provider が呼ばれたら即座に落とすことで dry-run の no-call を検証する。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def fetch_historical_metrics(self, keywords: list[str]):
        self.calls.append(list(keywords))
        raise AssertionError("provider must NOT be called")


def _kw(session: Session, text: str, status: str = "analyzed") -> Keyword:
    entity = Keyword(keyword=text)
    entity.status = status
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _add_signal(session: Session, keyword_id: int, component: str, value: float = 50.0) -> None:
    KeywordSignalService(session).create_signal(
        keyword_id,
        KeywordSignalCreate(
            component=component,
            normalized_value=value,
            provider="manual",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )


def _args(**overrides: object):
    base = {
        "keyword": None,
        "input": None,
        "create_missing": False,
        "collect_auto_signals": False,
        "refresh": False,
        "export_competition_template": None,
        "competition_source": None,
        "score_ready": False,
        "output": None,
        "dry_run": False,
    }
    base.update(overrides)
    return type("Args", (), base)()


def _run(session: Session, args, *, provider: object | None = None):
    @contextmanager
    def _factory() -> Iterator[Session]:
        yield session

    def _build(sess: Session) -> KeywordAnalysisService:
        metrics = KeywordMetricsCollectionService(
            sess,
            provider=provider or FakeGoogleAdsProvider(metrics=[]),
            settings=dummy_google_ads_settings(),
        )
        return KeywordAnalysisService(sess, metrics_service=metrics)

    return cli.run_workflow(args, session_factory=_factory, build_service=_build)


# -- input ------------------------------------------------------
def test_single_and_multiple_keyword_input(session: Session) -> None:
    _kw(session, "kw one")
    _kw(session, "kw two")
    summary = _run(session, _args(keyword=["kw one", "kw two", "kw one", "  "]))
    assert summary.total_keywords == 2
    assert summary.resolved == 2


def test_csv_input(session: Session, tmp_path: Path) -> None:
    _kw(session, "AI 議事録")
    path = tmp_path / "kw.csv"
    path.write_text("keyword\nAI 議事録\nAI 議事録\n", encoding="utf-8")
    summary = _run(session, _args(input=str(path)))
    assert summary.total_keywords == 1


def test_no_keywords_is_bad_input(session: Session) -> None:
    with pytest.raises(ValueError, match="no keywords"):
        _run(session, _args(keyword=[]))


def test_unresolved_keyword_reported(session: Session) -> None:
    summary = _run(session, _args(keyword=["ghost kw"]))
    assert summary.unresolved == 1
    assert any("unresolved" in m for m in summary.messages)


# -- template export ----------------------------------------
def test_competition_template_only_missing_and_import_compatible(
    session: Session, tmp_path: Path
) -> None:
    has_ce = _kw(session, "kw has ce")
    _kw(session, "kw no ce")
    _add_signal(session, has_ce.id, "competition_ease", value=40.0)
    out = tmp_path / "template.csv"

    _run(
        session,
        _args(
            keyword=["kw has ce", "kw no ce"],
            export_competition_template=str(out),
            competition_source="manual_free_tool",
        ),
    )

    rows = list(csv.reader(out.open(encoding="utf-8")))
    # import_competition_ease.py が読む CSV と同一のヘッダ形式
    assert rows[0] == [
        "keyword",
        "keyword_difficulty",
        "source_name",
        "source_reference",
        "observed_at",
    ]
    assert len(rows) == 2  # header + 1 (missing only)
    assert rows[1] == ["kw no ce", "", "manual_free_tool", "", ""]


# -- ranking export -----------------------------------------
def test_ranking_csv_order_and_columns(session: Session, tmp_path: Path) -> None:
    low = _kw(session, "kw low")
    high = _kw(session, "kw high")
    incomplete = _kw(session, "kw incomplete")
    for kw, val in ((low, 20.0), (high, 80.0)):
        for component in _ALL7:
            _add_signal(session, kw.id, component, value=val)
    _add_signal(session, incomplete.id, "search_demand", value=90.0)
    out = tmp_path / "rank.csv"

    _run(
        session,
        _args(
            keyword=["kw low", "kw high", "kw incomplete"],
            score_ready=True,
            output=str(out),
        ),
    )

    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["keyword"] for r in rows] == ["kw high", "kw low", "kw incomplete"]
    assert rows[0]["opportunity_score"] == "80.0"
    assert rows[2]["opportunity_score"] == ""
    assert rows[2]["analysis_status"] == "incomplete"
    assert "competition_ease" in rows[2]["missing_components"]
    assert set(rows[0]) == {
        "keyword",
        "keyword_id",
        *_ALL7,
        "opportunity_score",
        "analysis_status",
        "missing_components",
    }
    # cache と一致
    session.expire_all()
    assert session.get(Keyword, high.id).opportunity_score == 80.0


# -- dry-run ------------------------------------------------
def test_dry_run_no_writes_no_provider_call(session: Session, tmp_path: Path) -> None:
    _kw(session, "kw a")
    out = tmp_path / "rank.csv"
    template = tmp_path / "tmpl.csv"
    provider = _RecordingProvider()

    summary = _run(
        session,
        _args(
            keyword=["kw a", "ghost"],
            create_missing=True,
            collect_auto_signals=True,
            score_ready=True,
            export_competition_template=str(template),
            output=str(out),
            dry_run=True,
        ),
        provider=provider,
    )

    assert summary.dry_run is True
    assert provider.calls == []  # 外部 API は呼ばない
    assert not out.exists()
    assert not template.exists()
    # DB は無変更
    assert KeywordSignalRepository(session).list_by_keyword(
        session.query(Keyword).filter_by(keyword="kw a").one().id
    ) == []
    assert session.query(Keyword).filter_by(keyword="ghost").one_or_none() is None
    assert any("would" in m for m in summary.messages)


# -- collect auto signals via CLI --------------------------
def test_collect_auto_signals_one_bulk_fetch(session: Session) -> None:
    _kw(session, "AI 議事録 おすすめ")
    _kw(session, "ChatGPT 料金")
    provider = FakeGoogleAdsProvider(
        metrics=[_metrics("AI 議事録 おすすめ"), _metrics("ChatGPT 料金")]
    )
    summary = _run(
        session,
        _args(keyword=["AI 議事録 おすすめ", "ChatGPT 料金"], collect_auto_signals=True),
        provider=provider,
    )
    assert provider.calls == [["AI 議事録 おすすめ", "ChatGPT 料金"]]
    assert summary.auto_signals_created == 12  # 2 keyword × (3 GA + 3 local)
    assert summary.competition_ease_missing == 2
    assert summary.complete == 0  # competition_ease 未投入
    assert summary.incomplete == 2


def test_collect_auto_signals_matches_cjk_retokenised_response(session: Session) -> None:
    # Phase 2C-1.1: Google Ads が CJK keyword を分かち書きし直して返しても
    # 1 provider call で 5 keyword すべての 3 GA signal が生成される。
    requested = ["AI 議事録 おすすめ", "ChatGPT 料金", "AI 業務効率化",
                 "生成AI ツール 比較", "RPA 比較"]
    echoed = ["ai 議事 録 おすすめ", "chatgpt 料金", "ai 業務 効率 化",
              "生成 ai ツール 比較", "rpa 比較"]
    for text in requested:
        _kw(session, text)
    provider = FakeGoogleAdsProvider(metrics=[_metrics(e) for e in echoed])
    summary = _run(session, _args(keyword=requested, collect_auto_signals=True),
                   provider=provider)
    assert provider.calls == [requested]  # ちょうど 1 回
    assert summary.auto_signals_created == 5 * 6  # 5 keyword × (3 GA + 3 local)
    assert summary.auto_signals_failed == 0
    assert summary.incomplete == 5  # competition_ease だけ未投入


# -- cost safety ----------------------------------------
def test_no_paid_provider_imports() -> None:
    # 有料 API / 外部 HTTP client の import が無いこと (docstring の言及は許容)。
    forbidden = (
        "import requests",
        "from requests",
        "import httpx",
        "from httpx",
        "import dataforseo",
        "from dataforseo",
        "serpapi",
    )
    for path in (
        Path("scripts/run_keyword_analysis.py"),
        Path("app/services/keyword_analysis_service.py"),
        Path("app/services/keyword_metrics_collection_service.py"),
    ):
        source = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in source, f"{token} found in {path}"


def test_secret_not_in_summary_output(session: Session, capsys) -> None:
    _kw(session, "kw s")
    summary = _run(session, _args(keyword=["kw s"]))
    cli._print_summary(summary)
    out = capsys.readouterr()
    for forbidden in ("api_key", "password", "token", "developer_token", "refresh_token"):
        assert forbidden not in (out.out + out.err).lower()


def test_main_missing_keywords_returns_2(capsys) -> None:
    assert cli.main([]) == cli.EXIT_BAD_INPUT
    assert "cannot run workflow" in capsys.readouterr().err.lower()
