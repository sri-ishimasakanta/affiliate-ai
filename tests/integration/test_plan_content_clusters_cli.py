"""scripts/plan_content_clusters.py — read-only CLI (table / json)。

DB は in-memory の test engine のみ。production DB / HTTP / LLM には触れない。
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

import scripts.plan_content_clusters as m
from app.article.cluster_plan import load_cluster_config
from app.models import Article, Keyword
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_repository import ArticleRepository
from app.repositories.keyword_repository import KeywordRepository
from scripts.plan_content_clusters import DEFAULT_CONFIG, EXIT_CONFIG, EXIT_OK, main, run

_TRACKING = "https://track.example.invalid/secret-tracking-token"
_CONFIG = {
    "version": 1,
    "clusters": [
        {
            "id": "B",
            "name": "productivity",
            "priority": 1,
            "keywords": [
                {"keyword": "業務効率化 ツール おすすめ", "role": "pillar"},
                {"keyword": "業務効率化 ツール 比較", "role": "supporting"},
            ],
        },
        {
            "id": "C",
            "name": "automation",
            "priority": 2,
            "keywords": [
                {"keyword": "RPA おすすめ", "role": "pillar"},
                {"keyword": "RPA 比較", "role": "supporting"},
                {"keyword": "Make 料金", "role": "supporting"},
            ],
        },
    ],
    "vocabulary": {"product_terms": ["Make"], "generic_theme_tokens": ["ツール"]},
}


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "clusters.json"
    path.write_text(json.dumps(_CONFIG, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def seeded(session: Session) -> Session:
    keywords = KeywordRepository(session)
    created = {
        text: keywords.create(keyword=text)
        for text in (
            "業務効率化 ツール おすすめ",
            "業務効率化 ツール 比較",
            "RPA おすすめ",
            "RPA 比較",
            "Make 料金",
            "ChatGPT 料金",
        )
    }
    article = ArticleRepository(session).create(
        title="pillar", slug="pillar", keyword_id=created["業務効率化 ツール おすすめ"].id
    )
    article.status = "published"
    AffiliateProgramRepository(session).create(
        name="Make", provider="make", match_terms=["Make"], tracking_url=_TRACKING
    )
    session.commit()
    return session


def _factory(session: Session):
    return lambda: contextlib.nullcontext(session)


def _no_session():
    raise AssertionError("the DB must not be opened")


def _counts(session: Session) -> tuple[int, int]:
    return (
        session.scalar(select(func.count()).select_from(Keyword)),
        session.scalar(select(func.count()).select_from(Article)),
    )


# ================================================================ config gate
def test_default_config_is_the_tracked_file_and_valid() -> None:
    assert DEFAULT_CONFIG.name == "content_clusters.json"
    assert DEFAULT_CONFIG.parent.name == "config" and DEFAULT_CONFIG.parent.parent.name == "app"
    assert [c.id for c in load_cluster_config(DEFAULT_CONFIG).clusters] == ["B", "C", "A", "D"]


def test_missing_config_fails_before_opening_the_db(capsys) -> None:
    code = run(config_path="does-not-exist.json", session_factory=_no_session)
    assert code == EXIT_CONFIG
    assert "CONFIG ERROR" in capsys.readouterr().out


def test_invalid_config_fails_before_opening_the_db(tmp_path: Path, capsys) -> None:
    bad = dict(_CONFIG)
    bad["clusters"] = [
        {
            "id": "B",
            "name": "n",
            "priority": 1,
            "keywords": [
                {"keyword": "a", "role": "pillar"},
                {"keyword": "b", "role": "pillar"},
            ],
        }
    ]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    assert run(config_path=path, session_factory=_no_session) == EXIT_CONFIG
    assert "exactly one pillar" in capsys.readouterr().out


def test_unknown_keyword_is_reported_as_config_error(
    seeded: Session, tmp_path: Path, capsys
) -> None:
    raw = json.loads(json.dumps(_CONFIG))
    raw["clusters"][1]["keywords"].append({"keyword": "存在しない", "role": "supporting"})
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    before = _counts(seeded)

    assert run(config_path=path, session_factory=_factory(seeded)) == EXIT_CONFIG
    assert "unknown keyword" in capsys.readouterr().out
    assert _counts(seeded) == before


def test_main_bad_flags_and_missing_config_do_not_touch_the_db(monkeypatch, capsys) -> None:
    monkeypatch.setattr(m, "SessionLocal", _no_session)
    with pytest.raises(SystemExit) as exc:
        main(["--format", "xml"])
    assert exc.value.code == 2
    assert main(["--config", "does-not-exist.json"]) == EXIT_CONFIG
    assert "CONFIG ERROR" in capsys.readouterr().out


# ================================================================== outputs
def test_table_output_lists_slots_merges_blocks_and_unassigned(
    seeded: Session, config_file: Path, capsys
) -> None:
    assert run(config_path=config_file, session_factory=_factory(seeded)) == EXIT_OK
    out = capsys.readouterr().out
    assert "READ-ONLY: no DB write, no HTTP, no LLM" in out
    assert "new slots (2)" in out and "merged into another slot (1)" in out
    assert "blocked (2)" in out
    assert "RPA 比較 -> RPA おすすめ" in out
    assert "already_has_article" in out and "overlaps_existing_article" in out
    assert "ChatGPT 料金" in out  # unassigned
    assert " 1. C/pillar | RPA おすすめ" in out
    assert "affiliate=single(1)" in out  # Make 料金
    assert _TRACKING not in out


def test_json_output_is_valid_deterministic_and_secret_free(
    seeded: Session, config_file: Path, capsys
) -> None:
    assert run(config_path=config_file, output_format="json", session_factory=_factory(seeded)) == 0
    first = capsys.readouterr().out
    assert run(config_path=config_file, output_format="json", session_factory=_factory(seeded)) == 0
    second = capsys.readouterr().out

    assert first == second
    payload = json.loads(first)
    assert payload["summary"]["new_slots"] == 2
    assert [e["keyword"] for e in payload["slots"]] == ["RPA おすすめ", "Make 料金"]
    assert [e["position"] for e in payload["slots"]] == [1, 2]
    assert {e["decision"] for e in payload["slots"]} == {"ok"}
    assert {e["decision"] for e in payload["merged"]} == {"merge"}
    assert {e["decision"] for e in payload["blocked"]} == {"blocked"}
    assert _TRACKING not in first and "example.invalid" not in first


# ============================================================ zero side effects
def test_run_performs_zero_db_writes_zero_http(
    seeded: Session, engine: Engine, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("HTTP is forbidden")

    monkeypatch.setattr(httpx.Client, "send", _boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", _boom)

    before = _counts(seeded)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(_conn, _cursor, statement, _params, _context, _executemany) -> None:
        statements.append(statement)

    try:
        assert run(config_path=config_file, session_factory=_factory(seeded)) == EXIT_OK
        assert (
            run(config_path=config_file, output_format="json", session_factory=_factory(seeded))
            == EXIT_OK
        )
    finally:
        event.remove(engine, "before_cursor_execute", _spy)

    assert statements
    assert all(s.lstrip().split(None, 1)[0].upper() == "SELECT" for s in statements)
    assert _counts(seeded) == before
    assert not seeded.new and not seeded.dirty and not seeded.deleted
