"""ChangeEffectService の統合テスト (C9.3)。

pin する契約:

- 適用が無ければ「比較するものが無い」と明示して空を返す。
- 成熟していない期間では ``insufficient_data`` を返し、改善を主張しない。
- ``causal_claim`` は常に ``none``。
- 変更当日はどちらの窓にも入らない。
- 観測が無い指標は ``None`` のまま (0 を作らない)。
- read-only -- 記事も change_* も一切変更しない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.change.effect import (
    CAUSAL_CLAIM_NONE,
    EFFECT_INSUFFICIENT_DATA,
    EFFECT_OBSERVED,
    POST_WINDOW_NOT_ELAPSED,
)
from app.models import (
    Article,
    ChangeApplication,
    ChangeRequest,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
)
from app.services.change_effect_service import ChangeEffectService

_BASE = "https://example.com"
_PROPERTY = "sc-domain:example.com"
_CHANGE_AT = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
_SLUG = "rpa-implementation"


class _Settings:
    wordpress_base_url = _BASE
    search_console_property_uri = _PROPERTY
    search_console_credentials_file = None
    ga4_property_id = None


@pytest.fixture
def applied(session: Session):
    article = Article(
        id=18,
        title="記事18",
        slug=_SLUG,
        body="本文",
        status="published",
        published_url=f"{_BASE}/{_SLUG}/",
        published_at=_CHANGE_AT - timedelta(days=120),
        article_type="informational",
        monetization_mode="supporting",
        wordpress_post_id="1018",
    )
    session.add(article)
    session.commit()

    request = ChangeRequest(
        source_engine="seo",
        article_id=article.id,
        target_article_id=None,
        change_type="add_internal_link",
        proposal_version=1,
        proposal_hash="a" * 64,
        expected_source_body_hash="b" * 64,
        proposed_body_hash="c" * 64,
        proposed_body="本文\n\nあわせて読みたい",
        rationale="test",
        proposal_json={},
        evidence_json={},
        status="applied",
        created_at=_CHANGE_AT,
    )
    session.add(request)
    session.commit()

    application = ChangeApplication(
        change_request_id=request.id,
        article_id=article.id,
        proposal_hash="a" * 64,
        proposal_version=1,
        source_body_hash="b" * 64,
        proposed_body_hash="c" * 64,
        pre_change_body="本文",
        wordpress_post_id="1018",
        outcome="succeeded",
        attempted_at=_CHANGE_AT,
        finished_at=_CHANGE_AT,
    )
    session.add(application)
    session.commit()
    return article, request, application


def _gsc(session: Session, *, day: date, impressions: int, clicks: int, position: float) -> None:
    run = session.scalars(select(SearchConsoleImportRun).limit(1)).first()
    if run is None:
        run = SearchConsoleImportRun(
            property_uri=_PROPERTY,
            start_date=day,
            end_date=day,
            status="succeeded",
            dimensions_json='[["date","page"]]',
            import_identity_hash="a" * 64,
        )
        session.add(run)
        session.commit()
    # coverage は取り込み run の問い合わせ終端で決まる (行の有無ではない)。
    if day > run.end_date:
        run.end_date = day
    session.add(
        SearchConsolePageDaily(
            property_uri=_PROPERTY,
            metric_date=day,
            page=f"{_BASE}/{_SLUG}/",
            clicks=clicks,
            impressions=impressions,
            ctr=(clicks / impressions) if impressions else 0.0,
            position=position,
            source_import_run_id=run.id,
        )
    )
    session.commit()


def _build(session: Session, *, now: datetime, days: int = 7):
    return ChangeEffectService(session, settings=_Settings()).build(
        window_days=days, minimum_impressions=10, now=now
    )


def test_no_applied_change_yields_an_explicit_empty_report(session: Session) -> None:
    report = _build(session, now=datetime(2026, 7, 1, tzinfo=UTC))

    assert report.effects == []
    assert any("nothing to compare" in note for note in report.notes)


def test_immature_window_returns_insufficient_data(session: Session, applied) -> None:
    for offset in range(1, 8):
        _gsc(
            session,
            day=_CHANGE_AT.date() - timedelta(days=offset),
            impressions=100,
            clicks=5,
            position=12.0,
        )

    # 変更の 2 日後 -- 変更後の窓はまだ経過していない。
    report = _build(session, now=datetime(2026, 6, 3, 12, 0, tzinfo=UTC))
    effect = report.effects[0]

    assert effect.maturity["status"] == EFFECT_INSUFFICIENT_DATA
    assert POST_WINDOW_NOT_ELAPSED in effect.maturity["reasons"]
    assert effect.causal_claim == CAUSAL_CLAIM_NONE


def test_change_day_is_excluded_from_both_windows(session: Session, applied) -> None:
    report = _build(session, now=datetime(2026, 7, 1, tzinfo=UTC))
    effect = report.effects[0]

    assert effect.pre_window["end"] == "2026-05-31"
    assert effect.post_window["start"] == "2026-06-02"
    assert effect.change_date == "2026-06-01"


def test_missing_measurement_stays_none_instead_of_zero(session: Session, applied) -> None:
    report = _build(session, now=datetime(2026, 7, 1, tzinfo=UTC))
    effect = report.effects[0]

    assert effect.pre["impressions"] is None
    assert effect.post["impressions"] is None
    assert effect.deltas["impressions"] is None
    assert effect.pre["ga4_sessions"] is None


def test_mature_window_reports_observed_with_caveats(session: Session, applied) -> None:
    for offset in range(1, 8):
        _gsc(
            session,
            day=_CHANGE_AT.date() - timedelta(days=offset),
            impressions=100,
            clicks=4,
            position=12.0,
        )
    for offset in range(1, 8):
        _gsc(
            session,
            day=_CHANGE_AT.date() + timedelta(days=offset),
            impressions=120,
            clicks=8,
            position=10.0,
        )

    report = _build(session, now=datetime(2026, 6, 20, tzinfo=UTC))
    effect = report.effects[0]

    assert effect.maturity["status"] == EFFECT_OBSERVED
    assert effect.deltas["impressions"] == 140.0
    assert effect.deltas["clicks"] == 28.0
    assert effect.deltas["position"] == -2.0
    # 「観測できた」ことは「原因が分かった」ことではない。
    assert effect.causal_claim == CAUSAL_CLAIM_NONE
    assert effect.caveats


def test_the_report_is_read_only(session: Session, applied) -> None:
    article, request, application = applied
    before = (article.body, request.status, application.outcome)

    _build(session, now=datetime(2026, 7, 1, tzinfo=UTC))

    session.refresh(article)
    session.refresh(request)
    session.refresh(application)
    assert (article.body, request.status, application.outcome) == before


def test_a_failed_application_is_not_compared(session: Session, applied) -> None:
    _, _, application = applied
    application.outcome = "failed"
    session.commit()

    report = _build(session, now=datetime(2026, 7, 1, tzinfo=UTC))

    assert report.effects == []
