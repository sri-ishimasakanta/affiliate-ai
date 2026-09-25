"""成績の診断を DB と CLI から作る (読むだけ)。

pin する契約:

- 経過時間の起点は ``remote_timestamp`` (保存済みの ``age_hours`` や ``published_at`` ではない)。
- DB を 1 行も変えない。Threads の client を作らない。
- 管理外の投稿の検出は、一覧を読む API が無いので ``unavailable`` と報告する。
- CLI は JSON と Markdown を出力先に書く。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    PUB_PUBLISHED,
    SNAPSHOT_OBSERVED,
    TP_APPROVED,
    Article,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_performance_service import ThreadsPerformanceService

_T0 = datetime(2026, 9, 25, 0, 0, tzinfo=UTC)


class _Settings:
    threads_enabled = True
    wordpress_base_url = "https://bizfluxlab.com"


@pytest.fixture(autouse=True)
def _no_threads_client(monkeypatch):
    from app.social.threads import service as threads_service

    def _refuse(*_a, **_k):
        raise AssertionError("the diagnostic must not build a Threads client")

    monkeypatch.setattr(threads_service.ThreadsService, "__init__", _refuse)


@pytest.fixture
def article(session: Session) -> Article:
    row = Article(
        id=25,
        title="AI業務効率化",
        slug="ai-business-efficiency",
        body="本文。",
        status="published",
        published_url="https://bizfluxlab.com/ai-business-efficiency/",
        published_at=_T0 - timedelta(days=5),
        article_type="informational",
    )
    session.add(row)
    session.commit()
    return row


def _publication(session, article, seed, *, local_at, remote_at, link="none", views=()):
    proposal = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle="insight",
        link_mode=link,
        content_text=f"投稿 {seed}",
        character_count=len(f"投稿 {seed}"),
        content_seed=seed * 64,
        proposal_hash=seed.upper() * 64,
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=TP_APPROVED,
    )
    session.add(proposal)
    session.flush()
    row = ThreadsPublication(
        proposal_id=proposal.id,
        proposal_hash=proposal.proposal_hash,
        source_article_id=article.id,
        angle="insight",
        exact_published_text=proposal.content_text,
        threads_media_id=f"m-{seed}",
        status=PUB_PUBLISHED,
        published_at=local_at,
        remote_timestamp=remote_at.strftime("%Y-%m-%dT%H:%M:%S+0000"),
    )
    session.add(row)
    session.flush()
    for minutes, count in views:
        session.add(
            ThreadsInsightSnapshot(
                threads_publication_id=row.id,
                threads_media_id=row.threads_media_id,
                observed_at=remote_at + timedelta(minutes=minutes),
                # 保存済みの age_hours はわざと別の起点 (診断はこれを使わない)。
                age_hours=(minutes + 20) / 60,
                outcome=SNAPSHOT_OBSERVED,
                views=count,
                likes=0,
                replies=0,
                reposts=0,
                quotes=0,
                shares=0,
            )
        )
    session.commit()
    return row


def _seed(session, article):
    for i, (link, v1, v3) in enumerate((("none", 30, 40), ("article", 20, 30), ("none", 10, 15))):
        remote = _T0 + timedelta(hours=3 * i)
        _publication(
            session,
            article,
            "abc"[i],
            local_at=remote - timedelta(seconds=35),
            remote_at=remote,
            link=link,
            views=[(60, v1), (180, v3)],
        )


def _counts(session):
    return tuple(
        session.scalar(select(func.count()).select_from(m))
        for m in (ThreadsPublication, ThreadsInsightSnapshot, ThreadsPostProposal)
    )


def test_the_report_uses_the_remote_timestamp_and_changes_nothing(
    session: Session, article: Article
) -> None:
    _seed(session, article)
    before = _counts(session)
    report = ThreadsPerformanceService(session, settings=_Settings()).report(
        as_of=_T0 + timedelta(days=1)
    )
    assert _counts(session) == before
    first = report["publications"][0]
    assert first["age_basis"] == "remote_timestamp"
    assert first["checkpoints"]["1h"]["delta_minutes"] == 0.0  # 起点は remote_timestamp
    assert first["checkpoints"]["1h"]["metrics"]["views"] == 30
    assert report["checkpoint_summary"]["1h"]["comparable_count"] == 3
    # 40 → 30 → 15: 3 本とも下がり、最新は前の中央値 (35) の 43%。
    assert (
        report["checkpoint_summary"]["3h"]["classification"]["status"]
        == "directional_decline_signal"
    )
    assert report["untracked_remote_posts"]["status"] == "unavailable"
    assert report["side_effects"]["threads_calls"] == 0


def test_the_cli_writes_json_and_markdown(session, article, tmp_path, capsys) -> None:
    from scripts.analyze_threads_performance import main

    _seed(session, article)

    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    code = main(
        ["--as-of", (_T0 + timedelta(days=1)).isoformat(), "--output-dir", str(tmp_path)],
        session_factory=_Scoped(),
        settings=_Settings(),
    )
    assert code == 0
    payload = json.loads(
        (tmp_path / "threads_performance_diagnostic_latest.json").read_text("utf-8")
    )
    assert payload["schema_version"] == "threads-performance-diagnostic/1"
    markdown = (tmp_path / "threads_performance_diagnostic_latest.md").read_text("utf-8")
    for heading in (
        "## 1. Raw views (NOT comparable: different ages)",
        "## 2. Views at equal age since publication",
        "## 5. Link vs no-link",
        "## 9. Manual / untracked posts",
        "## Next decision",
    ):
        assert heading in markdown
    assert "read-only: database writes = 0, Threads calls = 0" in capsys.readouterr().out
