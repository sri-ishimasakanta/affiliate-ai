"""import_threads_insights CLI (T4、read-only)。

pin する契約:

- 既定は PLAN。``--execute`` を付けるまで Meta に問い合わせない。
- PLAN は「何本見るか」「最後の観測はいつか」「存在しない指標は何か」を出す。
- token は 1 文字も出さない (prefix も出さない)。
- Threads への書き込み呼び出しは常に 0 件。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import PUB_PUBLISHED, TP_APPROVED, Article, ThreadsPostProposal, ThreadsPublication
from app.services.threads_insights_service import ThreadsInsightsService
from app.social.threads.models import ThreadsInsights
from scripts.import_threads_insights import _print_plan, _print_report

_BASE = "https://bizfluxlab.com"
_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_TOKEN = "THAAAsecret-token-must-never-appear"
_TEXT = "体制を先に決めたほうが早い。"


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = _TOKEN
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = _BASE


class _FakeThreads:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def describe(self):
        from app.social.threads.service import ThreadsConnectionStatus

        return ThreadsConnectionStatus(
            enabled=True,
            configured=True,
            api_version="v1.0",
            user_id_configured=True,
            access_token_configured=True,
        )

    def media_insights(self, media_id, metrics=None):
        self.calls.append(f"insights:{media_id}")
        return ThreadsInsights(
            subject=f"media:{media_id}", values={"views": 0, "likes": 0}, missing=("shares",)
        )


@pytest.fixture
def published(session: Session) -> ThreadsPublication:
    from app.article.draft_promotion_canonical import compute_text_hash

    article = Article(
        id=21,
        title="生成AIガイドライン",
        slug="generative-ai-guidelines",
        body="記事本文。",
        status="published",
        published_url=f"{_BASE}/generative-ai-guidelines/",
        published_at=_NOW - timedelta(days=10),
        article_type="informational",
        monetization_mode="supporting",
        wordpress_post_id="84",
    )
    session.add(article)
    session.commit()

    proposal = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle="insight",
        link_mode="none",
        content_text=_TEXT,
        character_count=len(_TEXT),
        destination_url=None,
        content_seed="a" * 64,
        proposal_hash="A" * 64,
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=TP_APPROVED,
    )
    session.add(proposal)
    session.commit()

    publication = ThreadsPublication(
        proposal_id=proposal.id,
        proposal_hash=proposal.proposal_hash,
        source_article_id=article.id,
        angle=proposal.angle,
        exact_published_text=proposal.content_text,
        destination_url=None,
        threads_media_id="media-1",
        permalink="https://www.threads.com/@bizfluxlab/post/abc",
        status=PUB_PUBLISHED,
        published_at=_NOW - timedelta(minutes=12),
    )
    session.add(publication)
    session.commit()
    return publication


def _service(session: Session, fake: _FakeThreads) -> ThreadsInsightsService:
    return ThreadsInsightsService(session, settings=_Settings(), threads_service=fake)


def test_plan_output_shows_what_would_be_read(
    session: Session, published: ThreadsPublication, capsys
) -> None:
    fake = _FakeThreads()
    _print_plan(_service(session, fake).plan(now=_NOW))
    out = capsys.readouterr().out

    assert fake.calls == []
    assert "PLAN" in out
    assert "threads writes     = 0" in out
    assert f"publication {published.id}" in out
    assert "(none yet)" in out
    assert "--execute" in out


def test_plan_names_the_metrics_that_do_not_exist(
    session: Session, published: ThreadsPublication, capsys
) -> None:
    _print_plan(_service(session, _FakeThreads()).plan(now=_NOW))
    out = capsys.readouterr().out
    for absent in ("reach", "impressions", "ctr", "engagement_rate"):
        assert absent in out


def test_plan_never_prints_the_token_or_a_prefix(
    session: Session, published: ThreadsPublication, capsys
) -> None:
    _print_plan(_service(session, _FakeThreads()).plan(now=_NOW))
    out = capsys.readouterr().out
    assert _TOKEN not in out
    assert "THAAA" not in out
    assert "THAA" not in out


def test_report_refuses_to_call_a_fresh_post_a_failure(
    session: Session, published: ThreadsPublication, capsys
) -> None:
    service = _service(session, _FakeThreads())
    service.collect(execute=True, now=_NOW)
    _print_report(service.report(now=_NOW))
    out = capsys.readouterr().out

    assert "just_published" in out
    assert "interactions/view = (判定しない)" in out
    assert "INSUFFICIENT_DATA" in out
    assert "in development" in out


def test_report_marks_missing_metrics_as_unobserved_not_zero(
    session: Session, published: ThreadsPublication, capsys
) -> None:
    service = _service(session, _FakeThreads())
    service.collect(execute=True, now=_NOW)
    _print_report(service.report(now=_NOW))
    out = capsys.readouterr().out

    assert "shares    = (未観測)" in out
    assert "views     = 0" in out
