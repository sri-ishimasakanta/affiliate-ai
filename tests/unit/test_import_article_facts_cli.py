"""scripts/import_article_facts.py の検証 (atomicity / idempotency / dry-run)。"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, ArticleFact, Source
from scripts import import_article_facts as cli

NOW = datetime.now(UTC)
CHECKED = (NOW - timedelta(days=3)).isoformat()


def _factory(session: Session):
    @contextmanager
    def _f() -> Iterator[Session]:
        yield session

    return _f


def _article(session: Session) -> Article:
    a = Article(title="t", slug="a", keyword_id=None)
    session.add(a)
    session.flush()
    session.commit()
    return a


def _payload(**over) -> dict:
    base = {
        "article_id": 1,
        "sources": [
            {
                "tmp_id": "make_pricing",
                "source_type": "official_pricing",
                "source_url": "https://www.make.com/en/pricing",
                "title": "Make Pricing",
                "checked_at": CHECKED,
            }
        ],
        "tools": [
            {
                "subject_ref": "Make",
                "affiliate_program_id": None,
                "facts": {
                    "official_url": {
                        "value": "https://www.make.com/",
                        "value_status": "verified",
                        "source": "make_pricing",
                        "checked_at": CHECKED,
                    },
                    "japan_business_support": {
                        "value_status": "unknown",
                        "unknown_reason": "公式に記載なし",
                        "source": "make_pricing",
                        "checked_at": CHECKED,
                    },
                },
            }
        ],
    }
    base.update(over)
    return base


def _write(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "facts.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _run(session, path, *, dry_run=False):
    art = _article(session)
    return cli.run_import(
        article_id=art.id, path=path, dry_run=dry_run, session_factory=_factory(session)
    )


def test_import_creates_sources_and_facts(session: Session, tmp_path: Path) -> None:
    result = _run(session, _write(tmp_path, _payload()))
    assert result.sources_created == 1
    assert result.facts_created == 2
    assert session.scalar(select(func.count()).select_from(Source)) == 1
    assert session.scalar(select(func.count()).select_from(ArticleFact)) == 2


def test_import_is_atomic_on_fact_failure(session: Session, tmp_path: Path) -> None:
    bad = _payload()
    # verified なのに value なし -> validation 失敗
    bad["tools"][0]["facts"]["category"] = {
        "value_status": "verified",
        "source": "make_pricing",
        "checked_at": CHECKED,
    }
    art = _article(session)
    try:
        cli.run_import(
            article_id=art.id, path=_write(tmp_path, bad), dry_run=False,
            session_factory=_factory(session),
        )
        raise AssertionError("should have raised")
    except Exception:
        session.rollback()
    # Source も Fact も 1 件も残らない
    assert session.scalar(select(func.count()).select_from(Source)) == 0
    assert session.scalar(select(func.count()).select_from(ArticleFact)) == 0


def test_dry_run_writes_nothing(session: Session, tmp_path: Path) -> None:
    result = _run(session, _write(tmp_path, _payload()), dry_run=True)
    assert result.dry_run is True
    assert result.sources_created == 1 and result.facts_created == 2  # 予定件数
    assert session.scalar(select(func.count()).select_from(Source)) == 0
    assert session.scalar(select(func.count()).select_from(ArticleFact)) == 0


def test_rerun_is_idempotent(session: Session, tmp_path: Path) -> None:
    art = _article(session)
    path = _write(tmp_path, _payload())
    r1 = cli.run_import(article_id=art.id, path=path, dry_run=False,
                        session_factory=_factory(session))
    r2 = cli.run_import(article_id=art.id, path=path, dry_run=False,
                        session_factory=_factory(session))
    assert r1.facts_created == 2
    assert r2.sources_reused == 1 and r2.sources_created == 0
    assert r2.facts_skipped_same == 2 and r2.facts_created == 0
    assert session.scalar(select(func.count()).select_from(Source)) == 1
    assert session.scalar(select(func.count()).select_from(ArticleFact)) == 2


def test_new_checked_at_appends_history(session: Session, tmp_path: Path) -> None:
    art = _article(session)
    cli.run_import(article_id=art.id, path=_write(tmp_path, _payload()),
                   dry_run=False, session_factory=_factory(session))
    newer = (NOW - timedelta(days=1)).isoformat()
    p2 = _payload()
    p2["sources"][0]["checked_at"] = newer
    p2["tools"][0]["facts"]["official_url"]["checked_at"] = newer
    p2["tools"][0]["facts"]["official_url"]["value"] = "https://www.make.com/en"
    del p2["tools"][0]["facts"]["japan_business_support"]
    r = cli.run_import(article_id=art.id, path=_write(tmp_path, p2),
                       dry_run=False, session_factory=_factory(session))
    assert r.sources_created == 1  # 新しい observation
    assert r.facts_created == 1
    assert session.scalar(select(func.count()).select_from(ArticleFact)) == 3


def test_article_id_mismatch_rejected(session: Session, tmp_path: Path) -> None:
    art = _article(session)
    payload = _payload(article_id=999)
    try:
        cli.run_import(article_id=art.id, path=_write(tmp_path, payload),
                       dry_run=False, session_factory=_factory(session))
        raise AssertionError("should reject")
    except Exception as exc:
        assert "does not match" in str(exc)


def test_no_external_imports() -> None:
    src = Path("scripts/import_article_facts.py").read_text(encoding="utf-8").lower()
    for token in ("import requests", "from requests", "import httpx", "urllib.request"):
        assert token not in src
