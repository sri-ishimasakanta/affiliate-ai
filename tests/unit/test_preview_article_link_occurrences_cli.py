"""scripts.preview_article_link_occurrences — CLI plumbing (D-D2, read-only)。

このテストファイルは repo ルートを ``sys.path`` に追加してから ``scripts`` を
import する (他の scripts/*.py テストが無いためこのファイル内で完結させる)。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.config.database import build_engine  # noqa: E402
from app.exceptions import EntityNotFoundError  # noqa: E402
from app.models import Article, Base  # noqa: E402
from scripts.preview_article_link_occurrences import (  # noqa: E402
    format_preview,
    main,
    run_preview,
)

_HREF = "https://official.example.test/tool-a"


@pytest.fixture
def cli_engine() -> Engine:
    engine = build_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def cli_session_factory(cli_engine: Engine):
    factory = sessionmaker(bind=cli_engine, autoflush=False, expire_on_commit=False)

    def _make() -> Session:
        return factory()

    return _make


def _seed_article(cli_session_factory) -> int:
    with cli_session_factory() as session:
        art = Article(title="t", slug="p1", keyword_id=None, body=f"[tool]({_HREF})\n")
        session.add(art)
        session.commit()
        return art.id


def test_run_preview_returns_dto(cli_session_factory) -> None:
    article_id = _seed_article(cli_session_factory)
    preview = run_preview(article_id=article_id, session_factory=cli_session_factory)
    assert preview.article_id == article_id
    assert preview.external_occurrence_count == 1
    assert preview.active_mapping_count == 0
    assert preview.eligible_substitution_count == 0


def test_run_preview_missing_article_raises(cli_session_factory) -> None:
    with pytest.raises(EntityNotFoundError):
        run_preview(article_id=999999, session_factory=cli_session_factory)


def test_format_preview_never_includes_full_token_placeholder(cli_session_factory) -> None:
    article_id = _seed_article(cli_session_factory)
    preview = run_preview(article_id=article_id, session_factory=cli_session_factory)
    text = format_preview(preview)
    assert "article_id" in text
    assert "eligible_substitution_count" in text
    assert "no DB write" in text
    # このシナリオでは mapped/target が無いので token_fingerprint は "-"。
    assert "token_fingerprint=-" in text


def test_main_exit_code_not_found(cli_session_factory, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "scripts.preview_article_link_occurrences.SessionLocal", cli_session_factory
    )
    code = main(["--article-id", "999999"])
    assert code == 1
    out = capsys.readouterr().out
    assert "NOT FOUND" in out


def test_main_exit_code_ok(cli_session_factory, monkeypatch, capsys) -> None:
    article_id = _seed_article(cli_session_factory)
    monkeypatch.setattr(
        "scripts.preview_article_link_occurrences.SessionLocal", cli_session_factory
    )
    code = main(["--article-id", str(article_id)])
    assert code == 0
    out = capsys.readouterr().out
    assert "Article Link Occurrence Preview" in out
    assert "dry-run: no DB write, no WordPress request" in out


def test_main_reads_the_injected_session_factory_not_the_real_database(
    cli_session_factory, monkeypatch, capsys
) -> None:
    """``SessionLocal`` は default 引数ではなく呼び出し時に解決されること (回帰)。

    default 引数に束縛されていると monkeypatch が効かず、``main`` が実 DB
    (``affiliate_ai.db``) を読んでしまう。テスト用 DB にしか存在しないタイトルで検出する。
    """

    with cli_session_factory() as session:
        article = Article(title="scratch-only-article", slug="scratch-only", keyword_id=None)
        session.add(article)
        session.commit()
        article_id = article.id
    monkeypatch.setattr(
        "scripts.preview_article_link_occurrences.SessionLocal", cli_session_factory
    )

    code = main(["--article-id", str(article_id)])

    assert code == 0
    assert "article_title               = scratch-only-article" in capsys.readouterr().out
