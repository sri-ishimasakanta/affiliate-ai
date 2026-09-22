"""AffiliateClickMetricsService の統合テスト (C5.3)。

first-party の outbound click 記録は既存基盤 (WordPress runtime の ``/go/<token>``
+ 署名付き export + :class:`AffiliateOutboundClick`) が持っている。ここで pin する
のは **帰属** の正しさ:

- 記事 10 のクリックは記事 10 / Make に帰属する。
- 記事 11 のクリックは記事 11 に帰属する (記事 10 に混ざらない)。
- 記事 #1 の既存 Make リンクのクリックは従来どおり記事 #1 に帰属する。
- 未知/無効な token のクリックは **捨てずに** unattributed として数える。
- 公式サイトへの非アフィリエイトリンクは token を持たないため、クリックとして
  記録されず、アフィリエイトクリックにも数えられない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.models import (
    AffiliateClickImportRun,
    AffiliateLinkTarget,
    AffiliateOutboundClick,
    AffiliateProgram,
    Article,
)
from app.services.affiliate_click_metrics_service import AffiliateClickMetricsService

_MAKE_HOST = "www.make.com"


def _seed(session: Session) -> dict[str, AffiliateLinkTarget]:
    program = AffiliateProgram(name="Make", status="active")
    other = AffiliateProgram(name="Other ASP", status="active")
    session.add_all([program, other])
    session.commit()

    articles = {}
    for article_id, slug in ((1, "roundup"), (10, "make-how-to"), (11, "make-pricing")):
        article = Article(
            id=article_id,
            title=slug,
            slug=slug,
            keyword_id=None,
            body="x",
            status="published",
            published_url=f"https://bizfluxlab.com/{slug}/",
        )
        session.add(article)
        articles[slug] = article
    session.commit()

    targets = {}
    for token, article_id in (("tok-a1", 1), ("tok-a10", 10), ("tok-a11", 11)):
        target = AffiliateLinkTarget(
            token=token,
            article_id=article_id,
            affiliate_program_id=program.id,
            destination_url="https://www.make.com/en/register",
            destination_host=_MAKE_HOST,
            status="active",
            link_identity_hash=token.ljust(64, "0"),
        )
        session.add(target)
        targets[token] = target
    session.commit()
    return targets


def _import_run(session: Session) -> AffiliateClickImportRun:
    run = AffiliateClickImportRun(status="succeeded", requested_since_id=0, requested_limit=1000)
    session.add(run)
    session.commit()
    return run


def _click(session: Session, run, *, source_click_id: int, token: str, when: datetime) -> None:
    session.add(
        AffiliateOutboundClick(
            source_click_id=source_click_id,
            token=token,
            clicked_at=when,
            source_import_run_id=run.id,
        )
    )
    session.commit()


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 9, day, hour, 0, 0, tzinfo=UTC)


# ==================== attribution =============================================
def test_article_10_click_is_attributed_to_article_10_and_make(session: Session) -> None:
    _seed(session)
    run = _import_run(session)
    _click(session, run, source_click_id=1, token="tok-a10", when=_at(22))

    metrics = AffiliateClickMetricsService(session).aggregate()
    buckets = metrics.for_article(10)

    assert len(buckets) == 1
    assert buckets[0].clicks == 1
    assert buckets[0].affiliate_program_id == 1
    assert metrics.unattributed_clicks == 0


def test_article_11_click_is_not_mixed_into_article_10(session: Session) -> None:
    _seed(session)
    run = _import_run(session)
    _click(session, run, source_click_id=1, token="tok-a10", when=_at(22))
    _click(session, run, source_click_id=2, token="tok-a11", when=_at(22, 13))

    metrics = AffiliateClickMetricsService(session).aggregate()

    assert metrics.for_article(10)[0].clicks == 1
    assert metrics.for_article(11)[0].clicks == 1
    assert metrics.total_clicks == 2


def test_article_one_existing_make_link_still_attributes(session: Session) -> None:
    _seed(session)
    run = _import_run(session)
    for index in range(5):
        _click(session, run, source_click_id=index + 1, token="tok-a1", when=_at(16, index))

    metrics = AffiliateClickMetricsService(session).aggregate()
    bucket = metrics.for_article(1)[0]

    assert bucket.clicks == 5
    assert bucket.first_click_at < bucket.last_click_at
    assert bucket.by_date == {date(2026, 9, 16): 5}


def test_unknown_token_is_counted_as_unattributed_not_dropped(session: Session) -> None:
    _seed(session)
    run = _import_run(session)
    _click(session, run, source_click_id=1, token="tok-unknown", when=_at(13))
    _click(session, run, source_click_id=2, token="tok-a10", when=_at(22))

    metrics = AffiliateClickMetricsService(session).aggregate()

    assert metrics.total_clicks == 2
    assert metrics.unattributed_clicks == 1
    assert metrics.unattributed_tokens == ["tok-unknown"]
    assert metrics.for_article(10)[0].clicks == 1


def test_article_without_any_target_has_no_affiliate_clicks(session: Session) -> None:
    """公式リンクしか持たない記事は token を持たず、クリックも帰属しない。"""

    _seed(session)
    run = _import_run(session)
    _click(session, run, source_click_id=1, token="tok-a10", when=_at(22))

    metrics = AffiliateClickMetricsService(session).aggregate()
    assert metrics.for_article(1) == []
    assert metrics.for_article(11) == []


def test_clicks_from_a_disabled_target_are_flagged(session: Session) -> None:
    targets = _seed(session)
    targets["tok-a10"].status = "disabled"
    session.commit()
    run = _import_run(session)
    _click(session, run, source_click_id=1, token="tok-a10", when=_at(22))

    metrics = AffiliateClickMetricsService(session).aggregate()

    assert metrics.inactive_target_clicks == 1
    assert metrics.for_article(10)[0].clicks == 1


# ==================== window ==================================================
def test_window_excludes_clicks_outside_the_range(session: Session) -> None:
    _seed(session)
    run = _import_run(session)
    _click(session, run, source_click_id=1, token="tok-a10", when=_at(10))
    _click(session, run, source_click_id=2, token="tok-a10", when=_at(22))

    metrics = AffiliateClickMetricsService(session).aggregate(
        start_date=date(2026, 9, 20), end_date=date(2026, 9, 23)
    )

    assert metrics.total_clicks == 1
    assert metrics.for_article(10)[0].clicks == 1


def test_daily_breakdown_is_kept(session: Session) -> None:
    _seed(session)
    run = _import_run(session)
    _click(session, run, source_click_id=1, token="tok-a10", when=_at(21))
    _click(session, run, source_click_id=2, token="tok-a10", when=_at(22))
    _click(session, run, source_click_id=3, token="tok-a10", when=_at(22, 15))

    bucket = AffiliateClickMetricsService(session).aggregate().for_article(10)[0]

    assert bucket.by_date == {date(2026, 9, 21): 1, date(2026, 9, 22): 2}


def test_no_clicks_yields_an_empty_but_valid_result(session: Session) -> None:
    _seed(session)
    metrics = AffiliateClickMetricsService(session).aggregate()
    assert metrics.total_clicks == 0
    assert metrics.buckets == []
    assert metrics.unattributed_clicks == 0
