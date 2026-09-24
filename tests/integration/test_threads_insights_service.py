"""Threads の指標取り込みと学習 (T4、Meta には一切接続しない)。

pin する契約:

- 取り込みは **読むだけ**。投稿も、提案の文面も状態も触らない。
- 欠測を 0 で埋めない。観測は append-only で、過去の行を書き換えない。
- 公開直後の 0 を成績として扱わない。
- 1 本しか無いうちは「まだ言えない」以上のことを言わない。
- 母数が足りない次元では平均も比率も出さない。
- token は DB にもログにも例外にも出ない。
- 成績ではアラートを 1 件も作らない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    PUB_PUBLISHED,
    PUB_UNCERTAIN,
    SNAPSHOT_FAILED,
    SNAPSHOT_OBSERVED,
    TP_APPROVED,
    Article,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_insights_service import (
    REC_COLLECT_MORE,
    REC_INSUFFICIENT_DATA,
    ThreadsInsightsService,
)
from app.social.threads.errors import ThreadsAuthError, ThreadsRateLimitError
from app.social.threads.models import ThreadsInsights

_BASE = "https://bizfluxlab.com"
_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_TOKEN = "THAAAsecret-token-must-never-appear"
_TEXT = "体制を先に決めたほうが早い。\n参照先と版を残しておくほうが更新しやすい。"


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = _TOKEN
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = _BASE


class _FakeThreads:
    """ThreadsService の代役。**書き込み系メソッドを持たない。**"""

    def __init__(self, *, insights=None, error=None) -> None:
        self.calls: list[str] = []
        self._insights = insights
        self._error = error

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
        if self._error:
            raise self._error
        return self._insights or ThreadsInsights(
            subject=f"media:{media_id}", values={"views": 0, "likes": 0}, missing=("shares",)
        )


@pytest.fixture
def article(session: Session) -> Article:
    row = Article(
        id=21,
        title="生成AIガイドライン｜要点と実務上の注意点",
        slug="generative-ai-guidelines",
        body="記事本文。",
        status="published",
        published_url=f"{_BASE}/generative-ai-guidelines/",
        published_at=_NOW - timedelta(days=10),
        article_type="informational",
        monetization_mode="supporting",
        wordpress_post_id="84",
    )
    session.add(row)
    session.commit()
    return row


def _proposal(
    session: Session,
    article: Article,
    *,
    angle="insight",
    link_mode="none",
    text=_TEXT,
    seed="a",
) -> ThreadsPostProposal:
    from app.article.draft_promotion_canonical import compute_text_hash

    row = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle=angle,
        link_mode=link_mode,
        content_text=text,
        character_count=len(text),
        destination_url=(
            f"{_BASE}/generative-ai-guidelines/?utm_source=threads"
            if link_mode == "article"
            else None
        ),
        content_seed=seed * 64,
        proposal_hash=seed.upper() * 64,
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=TP_APPROVED,
    )
    session.add(row)
    session.commit()
    return row


def _publication(
    session: Session,
    proposal: ThreadsPostProposal,
    *,
    media_id="media-1",
    published_at=None,
    status=PUB_PUBLISHED,
) -> ThreadsPublication:
    row = ThreadsPublication(
        proposal_id=proposal.id,
        proposal_hash=proposal.proposal_hash,
        source_article_id=proposal.source_article_id,
        angle=proposal.angle,
        exact_published_text=proposal.content_text,
        destination_url=proposal.destination_url,
        threads_media_id=media_id,
        permalink=f"https://www.threads.com/@bizfluxlab/post/{media_id}",
        status=status,
        published_at=published_at if published_at is not None else _NOW - timedelta(hours=100),
    )
    session.add(row)
    session.commit()
    return row


def _service(session: Session, fake: _FakeThreads) -> ThreadsInsightsService:
    return ThreadsInsightsService(session, settings=_Settings(), threads_service=fake)


# -- plan ----------------------------------------------------------------------
def test_plan_never_touches_meta(session: Session, article: Article) -> None:
    publication = _publication(session, _proposal(session, article))
    fake = _FakeThreads()
    plan = _service(session, fake).plan(now=_NOW)

    assert fake.calls == []
    assert plan["executed"] is False
    assert plan["threads_writes"] == 0
    assert plan["publications"][0]["publication_id"] == publication.id


def test_plan_names_the_metrics_that_do_not_exist(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    plan = _service(session, _FakeThreads()).plan(now=_NOW)
    for absent in ("reach", "impressions", "ctr", "engagement_rate"):
        assert absent in plan["unsupported_metrics"]
    assert "views" in plan["supported_metrics"]


def test_plan_does_not_leak_the_token(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    plan = _service(session, _FakeThreads()).plan(now=_NOW)
    assert _TOKEN not in str(plan)
    assert "THAAA" not in str(plan)


# -- collection ----------------------------------------------------------------
def test_collect_without_execute_writes_nothing(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    fake = _FakeThreads()
    outcome = _service(session, fake).collect(execute=False, now=_NOW)

    assert fake.calls == []
    assert outcome.imported == 0
    assert session.scalars(select(ThreadsInsightSnapshot)).all() == []


def test_collect_appends_one_observation(session: Session, article: Article) -> None:
    publication = _publication(session, _proposal(session, article))
    fake = _FakeThreads(
        insights=ThreadsInsights(
            subject="media:media-1",
            values={"views": 120, "likes": 4, "replies": 1},
            missing=("shares", "quotes", "reposts"),
        )
    )
    outcome = _service(session, fake).collect(execute=True, now=_NOW)

    assert outcome.imported == 1
    rows = session.scalars(select(ThreadsInsightSnapshot)).all()
    assert len(rows) == 1
    assert rows[0].threads_publication_id == publication.id
    assert rows[0].outcome == SNAPSHOT_OBSERVED
    assert rows[0].views == 120
    # 取れなかった指標は **NULL のまま** (0 で埋めない)。
    assert rows[0].shares is None
    assert "shares" in rows[0].missing_json


def test_observations_accumulate_instead_of_overwriting(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    service = _service(
        session,
        _FakeThreads(insights=ThreadsInsights(subject="s", values={"views": 10}, missing=())),
    )
    service.collect(execute=True, now=_NOW)
    service._threads = _FakeThreads(
        insights=ThreadsInsights(subject="s", values={"views": 30}, missing=())
    )
    service.collect(execute=True, now=_NOW + timedelta(hours=24))

    rows = session.scalars(
        select(ThreadsInsightSnapshot).order_by(ThreadsInsightSnapshot.observed_at)
    ).all()
    assert [r.views for r in rows] == [10, 30]


def test_a_repeated_observation_at_the_same_moment_is_not_duplicated(
    session: Session, article: Article
) -> None:
    _publication(session, _proposal(session, article))
    service = _service(session, _FakeThreads())
    service.collect(execute=True, now=_NOW)
    outcome = service.collect(execute=True, now=_NOW)

    assert outcome.unchanged == 1
    assert len(session.scalars(select(ThreadsInsightSnapshot)).all()) == 1


def test_collection_never_mutates_the_proposal_or_publication(
    session: Session, article: Article
) -> None:
    proposal = _proposal(session, article)
    publication = _publication(session, proposal)
    before = (
        proposal.content_text,
        proposal.status,
        publication.status,
        publication.exact_published_text,
    )

    _service(session, _FakeThreads()).collect(execute=True, now=_NOW)
    session.refresh(proposal)
    session.refresh(publication)

    assert (
        proposal.content_text,
        proposal.status,
        publication.status,
        publication.exact_published_text,
    ) == before


def test_only_published_publications_are_read(session: Session, article: Article) -> None:
    _publication(
        session,
        _proposal(session, article, seed="b"),
        media_id="media-2",
        status=PUB_UNCERTAIN,
    )
    fake = _FakeThreads()
    outcome = _service(session, fake).collect(execute=True, now=_NOW)

    assert outcome.checked == 0
    assert fake.calls == []


def test_a_failed_fetch_is_recorded_without_inventing_zeros(
    session: Session, article: Article
) -> None:
    _publication(session, _proposal(session, article))
    outcome = _service(
        session, _FakeThreads(error=ThreadsAuthError("access_token=[redacted] rejected"))
    ).collect(execute=True, now=_NOW)

    assert outcome.failed == 1
    row = session.scalars(select(ThreadsInsightSnapshot)).one()
    assert row.outcome == SNAPSHOT_FAILED
    assert row.views is None and row.likes is None
    assert row.error_category == "threads_auth"


def test_the_token_never_reaches_the_database(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    _service(
        session, _FakeThreads(error=ThreadsAuthError(f"request failed with access_token={_TOKEN}"))
    ).collect(execute=True, now=_NOW)

    row = session.scalars(select(ThreadsInsightSnapshot)).one()
    assert _TOKEN not in (row.error_message or "")
    assert "THAAA" not in (row.error_message or "")


# -- reporting -----------------------------------------------------------------
def test_a_brand_new_post_with_zeros_is_not_called_a_poor_performer(
    session: Session, article: Article
) -> None:
    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(minutes=12))
    service = _service(session, _FakeThreads())
    service.collect(execute=True, now=_NOW)
    report = service.report(now=_NOW)

    post = report["publications"][0]
    assert post["maturity"]["stage"] == "just_published"
    assert post["maturity"]["comparable"] is False
    assert "insufficient_age" in post["observations"]
    assert post["interactions_per_view"] is None
    assert report["mature_count"] == 0


def test_one_post_produces_only_insufficient_data(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(minutes=12))
    service = _service(session, _FakeThreads())
    service.collect(execute=True, now=_NOW)
    types = {rec["type"] for rec in service.report(now=_NOW)["recommendations"]}

    assert types <= {REC_INSUFFICIENT_DATA, REC_COLLECT_MORE}
    # 1 本から「この切り口が良い」とは言わない。
    assert "ANGLE_RETRY" not in types
    assert "TOPIC_REUSE" not in types


def test_dimensions_report_counts_but_refuse_to_compare_below_the_minimum(
    session: Session, article: Article
) -> None:
    _publication(session, _proposal(session, article))
    service = _service(
        session,
        _FakeThreads(
            insights=ThreadsInsights(subject="s", values={"views": 200, "likes": 5}, missing=())
        ),
    )
    service.collect(execute=True, now=_NOW)
    by_angle = service.report(now=_NOW)["by_angle"]["insight"]

    assert by_angle["sample"] == 1
    assert by_angle["sufficient_sample"] is False
    assert by_angle["median_views"] is None
    assert by_angle["note"]


def test_no_composite_score_is_produced(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    service = _service(session, _FakeThreads())
    service.collect(execute=True, now=_NOW)
    post = service.report(now=_NOW)["publications"][0]

    assert "score" not in post
    assert not any("score" in key for key in post)


def test_attribution_is_unavailable_without_a_link(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article, link_mode="none"))
    attribution = _service(session, _FakeThreads()).report(now=_NOW)["website_attribution"]

    assert attribution["linked_publications"] == []
    assert attribution["unavailable_reason"]
    assert attribution["limitations"]


def test_attribution_exposes_the_join_key_for_linked_posts(
    session: Session, article: Article
) -> None:
    _publication(session, _proposal(session, article, link_mode="article", seed="c"))
    attribution = _service(session, _FakeThreads()).report(now=_NOW)["website_attribution"]

    assert len(attribution["linked_publications"]) == 1
    assert "utm_campaign" in attribution["join_key"]


def test_the_report_states_that_views_are_still_in_development(
    session: Session, article: Article
) -> None:
    _publication(session, _proposal(session, article))
    caveats = " ".join(_service(session, _FakeThreads()).report(now=_NOW)["caveats"])
    assert "in development" in caveats


# -- health --------------------------------------------------------------------
def test_zero_engagement_produces_no_alert(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    service = _service(session, _FakeThreads())
    service.collect(execute=True, now=_NOW)  # views 0 / likes 0

    assert service.alert_drafts() == []


def test_an_auth_failure_produces_an_alert(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    service = _service(session, _FakeThreads(error=ThreadsAuthError("token rejected")))
    service.collect(execute=True, now=_NOW)

    drafts = service.alert_drafts()
    assert len(drafts) == 1
    assert drafts[0].source == "threads_insights"


def test_a_single_transient_failure_stays_quiet(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    service = _service(session, _FakeThreads(error=ThreadsRateLimitError("slow down")))
    service.collect(execute=True, now=_NOW)

    assert service.alert_drafts() == []


def test_repeated_transient_failures_eventually_alert(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    service = _service(session, _FakeThreads(error=ThreadsRateLimitError("slow down")))
    for hours in (0, 24, 48):
        service.collect(execute=True, now=_NOW + timedelta(hours=hours))

    drafts = service.alert_drafts()
    assert len(drafts) == 1
    assert drafts[0].evidence["consecutive_failures"] == 3


def test_a_recovery_clears_the_failure_streak(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article))
    service = _service(session, _FakeThreads(error=ThreadsRateLimitError("slow down")))
    for hours in (0, 24, 48):
        service.collect(execute=True, now=_NOW + timedelta(hours=hours))
    service._threads = _FakeThreads()
    service.collect(execute=True, now=_NOW + timedelta(hours=72))

    assert service.alert_drafts() == []


def test_text_drift_from_the_approved_copy_alerts(session: Session, article: Article) -> None:
    proposal = _proposal(session, article)
    publication = _publication(session, proposal)
    publication.exact_published_text = "承認されていない別の文面。"
    session.commit()

    drafts = _service(session, _FakeThreads()).alert_drafts()
    assert len(drafts) == 1
    assert drafts[0].severity == "error"


# -- T4.1: JST-derived dimensions ----------------------------------------------
def test_publication_hour_and_weekday_use_the_operations_timezone(
    session: Session, article: Article
) -> None:
    """2026-09-23 18:24 UTC は JST で 09-24 (木) 03 時。UTC の 18 時・水曜ではない。"""

    _publication(
        session,
        _proposal(session, article),
        published_at=datetime(2026, 9, 23, 18, 24, tzinfo=UTC),
    )
    report = _service(session, _FakeThreads()).report(now=_NOW)
    post = report["publications"][0]

    assert report["local_timezone"] == "Asia/Tokyo"
    assert post["published_local_hour"] == 3
    assert post["published_local_weekday"] == "Thu"
    assert post["published_local_at"].startswith("2026-09-24T03:24")
    # 生の時刻は UTC のまま (保存値を書き換えない)。
    assert post["published_at"].startswith("2026-09-23T18:24")
    assert "published_hour_utc" not in post


def test_timezone_conversion_does_not_change_maturity(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(minutes=20))
    post = _service(session, _FakeThreads()).report(now=_NOW)["publications"][0]
    assert post["maturity"]["age_hours"] == pytest.approx(0.33, abs=0.01)
    assert post["maturity"]["stage"] == "just_published"
