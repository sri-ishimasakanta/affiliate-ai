"""AffiliateCleanClickService の統合テスト (C7)。

これは C7 でもっとも壊してはいけない部分: **自分たちの計測用リクエストが作った
クリックを、読者の行動として数えない**。同時に、履歴を消したり書き換えたりしない。

pin する契約:

- 信頼境界より前のクリックは ``excluded`` に数え、``clean`` から外す。
- 行そのものは DB に残り続ける (削除も更新もしない)。
- ``raw`` = ``excluded`` + ``clean`` が常に成り立つ。
- target の無い token は捨てずに ``unattributed`` として数える。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AffiliateClickImportRun,
    AffiliateLinkTarget,
    AffiliateOutboundClick,
    AffiliateProgram,
    Article,
)
from app.revenue.policy import RevenuePolicy
from app.services.affiliate_clean_click_service import AffiliateCleanClickService

_CUTOFF = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)


def _policy(cutoff: str | None = "2026-09-23T00:00:00+00:00") -> RevenuePolicy:
    return RevenuePolicy(
        policy_version="test",
        raw={
            "trusted_click_measurement": {
                "trusted_measurement_start_at": cutoff,
                "rationale": ["test"],
            }
        },
    )


def _seed(session: Session) -> None:
    program = AffiliateProgram(name="Make", status="active")
    session.add(program)
    session.commit()
    for article_id, slug in ((10, "make-how-to"), (11, "make-pricing")):
        session.add(
            Article(
                id=article_id,
                title=slug,
                slug=slug,
                keyword_id=None,
                body="x",
                status="published",
                published_url=f"https://bizfluxlab.com/{slug}/",
            )
        )
    session.commit()
    for token, article_id in (("tok-10", 10), ("tok-11", 11)):
        session.add(
            AffiliateLinkTarget(
                token=token,
                article_id=article_id,
                affiliate_program_id=program.id,
                destination_url="https://www.make.com/en/register",
                destination_host="www.make.com",
                status="active",
                link_identity_hash=token.ljust(64, "0"),
            )
        )
    session.commit()


def _run(session: Session) -> AffiliateClickImportRun:
    run = AffiliateClickImportRun(status="succeeded", requested_since_id=0, requested_limit=100)
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


def _build(session: Session, *, policy=None, **kwargs):
    return AffiliateCleanClickService(session, policy=policy or _policy()).build(**kwargs)


# ==================== exclusion ===============================================
def test_clicks_before_the_cutoff_are_excluded(session: Session) -> None:
    _seed(session)
    run = _run(session)
    _click(
        session,
        run,
        source_click_id=1,
        token="tok-10",
        when=datetime(2026, 9, 22, 13, 45, tzinfo=UTC),
    )

    baseline = _build(session)

    assert baseline.raw_clicks == 1
    assert baseline.excluded_clicks == 1
    assert baseline.clean_clicks == 0
    assert baseline.counts_for(10).clean_clicks == 0
    assert baseline.counts_for(10).excluded_clicks == 1


def test_clicks_after_the_cutoff_are_clean(session: Session) -> None:
    _seed(session)
    run = _run(session)
    _click(
        session,
        run,
        source_click_id=1,
        token="tok-10",
        when=datetime(2026, 9, 23, 9, 0, tzinfo=UTC),
    )

    baseline = _build(session)

    assert baseline.clean_clicks == 1
    assert baseline.excluded_clicks == 0
    assert baseline.counts_for(10).clean_clicks == 1


def test_the_cutoff_instant_itself_is_trusted(session: Session) -> None:
    _seed(session)
    run = _run(session)
    _click(session, run, source_click_id=1, token="tok-10", when=_CUTOFF)
    assert _build(session).clean_clicks == 1


def test_raw_always_equals_excluded_plus_clean(session: Session) -> None:
    _seed(session)
    run = _run(session)
    _click(
        session,
        run,
        source_click_id=1,
        token="tok-10",
        when=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )
    _click(
        session,
        run,
        source_click_id=2,
        token="tok-11",
        when=datetime(2026, 9, 22, 13, 0, tzinfo=UTC),
    )
    _click(
        session,
        run,
        source_click_id=3,
        token="tok-10",
        when=datetime(2026, 9, 24, 8, 0, tzinfo=UTC),
    )

    baseline = _build(session)

    assert baseline.raw_clicks == 3
    assert baseline.excluded_clicks + baseline.clean_clicks == baseline.raw_clicks
    assert baseline.excluded_clicks == 2
    assert baseline.clean_clicks == 1


def test_history_is_never_deleted_or_rewritten(session: Session) -> None:
    _seed(session)
    run = _run(session)
    when = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    _click(session, run, source_click_id=1, token="tok-10", when=when)

    _build(session)

    rows = session.scalars(select(AffiliateOutboundClick)).all()
    assert session.scalar(select(func.count()).select_from(AffiliateOutboundClick)) == 1
    assert rows[0].token == "tok-10"
    assert rows[0].source_click_id == 1


def test_no_cutoff_means_every_click_is_trusted(session: Session) -> None:
    _seed(session)
    run = _run(session)
    _click(
        session,
        run,
        source_click_id=1,
        token="tok-10",
        when=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )

    baseline = _build(session, policy=_policy(cutoff=None))

    assert baseline.excluded_clicks == 0
    assert baseline.clean_clicks == 1


# ==================== attribution / data quality ==============================
def test_unknown_token_is_counted_not_dropped(session: Session) -> None:
    _seed(session)
    run = _run(session)
    _click(
        session,
        run,
        source_click_id=1,
        token="tok-synthetic",
        when=datetime(2026, 9, 13, 14, 0, tzinfo=UTC),
    )

    baseline = _build(session)

    assert baseline.raw_clicks == 1
    assert baseline.unattributed_raw_clicks == 1
    assert baseline.unattributed_tokens == ["tok-synthetic"]
    assert baseline.counts_for(10).raw_clicks == 0


def test_clicks_are_attributed_per_article(session: Session) -> None:
    _seed(session)
    run = _run(session)
    _click(
        session,
        run,
        source_click_id=1,
        token="tok-10",
        when=datetime(2026, 9, 24, 8, 0, tzinfo=UTC),
    )
    _click(
        session,
        run,
        source_click_id=2,
        token="tok-11",
        when=datetime(2026, 9, 24, 9, 0, tzinfo=UTC),
    )

    baseline = _build(session)

    assert baseline.counts_for(10).clean_clicks == 1
    assert baseline.counts_for(11).clean_clicks == 1
    assert baseline.counts_for(10).affiliate_program_ids == (1,)


def test_clean_data_through_reflects_only_trusted_clicks(session: Session) -> None:
    _seed(session)
    run = _run(session)
    _click(
        session,
        run,
        source_click_id=1,
        token="tok-10",
        when=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )
    _click(
        session,
        run,
        source_click_id=2,
        token="tok-10",
        when=datetime(2026, 9, 24, 8, 0, tzinfo=UTC),
    )

    baseline = _build(session)

    assert baseline.raw_data_through == date(2026, 9, 24)
    assert baseline.clean_data_through == date(2026, 9, 24)


def test_only_excluded_clicks_leave_clean_data_through_unset(session: Session) -> None:
    _seed(session)
    run = _run(session)
    _click(
        session,
        run,
        source_click_id=1,
        token="tok-10",
        when=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )

    baseline = _build(session)

    assert baseline.raw_data_through == date(2026, 9, 22)
    assert baseline.clean_data_through is None


def test_window_bounds_are_respected(session: Session) -> None:
    _seed(session)
    run = _run(session)
    _click(
        session, run, source_click_id=1, token="tok-10", when=datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
    )
    _click(
        session,
        run,
        source_click_id=2,
        token="tok-10",
        when=datetime(2026, 9, 24, 8, 0, tzinfo=UTC),
    )

    baseline = _build(session, window_start=date(2026, 9, 1), window_end=date(2026, 9, 30))

    assert baseline.raw_clicks == 1
    assert baseline.clean_clicks == 1


def test_article_without_clicks_reports_zeroes_not_missing(session: Session) -> None:
    _seed(session)
    counts = _build(session).counts_for(11)
    assert counts.raw_clicks == 0
    assert counts.clean_clicks == 0
    assert counts.first_clean_click_at is None
