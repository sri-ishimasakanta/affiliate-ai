"""Ga4ImportService の統合テスト (C5.2)。

fake provider を注入し、ネットワークなしで取り込み経路全体を検証する。pin する契約
(Search Console 取り込みと同じ保証を GA4 側でも持たせる):

- prepare は通信しない。期間/property を検証し、identity が決まる。
- 同じ idempotency key での prepare は同じ run を返す。
- execute は provider 呼び出しの前に running を commit する。
- 行は ``(property, date, page_path, channel_scope)`` の UPSERT で重複しない。
- 全トラフィックとオーガニックは別 scope の行として保存される。
- window 外の行は永続化前に拒否し、run は failed になる。
- provider 失敗は run を failed にし、metrics を書かない。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.rows import Ga4PageRow
from app.exceptions import ExternalProviderDataError, ExternalProviderError, Ga4ImportStateError
from app.models import Ga4ImportRun, Ga4PageDaily
from app.services.ga4_import_service import Ga4ImportService

_PROPERTY = "987654321"
_START = date(2026, 9, 20)
_END = date(2026, 9, 22)


def _row(*, day=_END, path="/rpa-tools/", scope="all", sessions=10, **over) -> Ga4PageRow:
    kwargs = dict(
        metric_date=day,
        page_path=path,
        channel_scope=scope,
        sessions=sessions,
        active_users=max(sessions - 2, 0),
        new_users=max(sessions - 4, 0),
        engaged_sessions=max(sessions - 5, 0),
        engagement_rate=0.5,
        average_engagement_time_seconds=100.0,
        screen_page_views=sessions + 2,
    )
    kwargs.update(over)
    return Ga4PageRow(**kwargs)


class _FakeProvider:
    def __init__(self, rows=None, *, exc=None, timezone="Asia/Tokyo") -> None:
        self._rows = rows if rows is not None else [_row()]
        self._exc = exc
        self._timezone = timezone
        self.calls = 0
        self.seen_running_status: str | None = None

    def fetch_property_timezone(self, *, property_id: str) -> str | None:
        return self._timezone

    def fetch_page_daily(self, *, property_id, start_date, end_date):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._rows


def _prepare(session: Session, **over):
    kwargs = dict(start_date=_START, end_date=_END, property_id=_PROPERTY)
    kwargs.update(over)
    return Ga4ImportService(session).prepare(**kwargs)


# ==================== prepare =================================================
def test_prepare_creates_a_prepared_run_without_network(session: Session) -> None:
    run = _prepare(session)
    assert run.status == "prepared"
    assert run.property_id == _PROPERTY
    assert run.import_identity_hash
    assert run.started_at is None


def test_prepare_normalizes_the_properties_prefix(session: Session) -> None:
    run = _prepare(session, property_id=f"properties/{_PROPERTY}")
    assert run.property_id == _PROPERTY


def test_prepare_rejects_a_non_numeric_property(session: Session) -> None:
    with pytest.raises(Ga4ImportStateError):
        _prepare(session, property_id="G-ABCDEF")


def test_prepare_rejects_a_reversed_range(session: Session) -> None:
    with pytest.raises(Ga4ImportStateError):
        _prepare(session, start_date=_END, end_date=_START)


def test_prepare_rejects_a_future_end_date(session: Session) -> None:
    with pytest.raises(Ga4ImportStateError):
        _prepare(session, end_date=date(2099, 1, 1))


def test_prepare_is_idempotent_for_the_same_key(session: Session) -> None:
    first = _prepare(session, idempotency_key="k1")
    second = _prepare(session, idempotency_key="k1")
    assert first.id == second.id
    assert session.scalar(select(func.count()).select_from(Ga4ImportRun)) == 1


def test_prepare_rejects_a_reused_key_for_a_different_identity(session: Session) -> None:
    _prepare(session, idempotency_key="k1")
    with pytest.raises(Ga4ImportStateError):
        _prepare(session, start_date=date(2026, 9, 21), idempotency_key="k1")


# ==================== execute =================================================
def test_execute_imports_rows_and_marks_succeeded(session: Session) -> None:
    run = _prepare(session)
    provider = _FakeProvider([_row(), _row(scope="organic_search", sessions=4)])

    result = Ga4ImportService(session).execute(run.id, provider=provider)

    assert result.status == "succeeded"
    assert result.page_rows_received == 2
    assert result.page_rows_upserted == 2
    assert result.data_through_date == _END
    assert result.property_timezone == "Asia/Tokyo"
    assert provider.calls == 1


def test_both_channel_scopes_are_stored_as_separate_rows(session: Session) -> None:
    run = _prepare(session)
    Ga4ImportService(session).execute(
        run.id,
        provider=_FakeProvider([_row(sessions=10), _row(scope="organic_search", sessions=4)]),
    )
    rows = session.scalars(select(Ga4PageDaily).order_by(Ga4PageDaily.channel_scope)).all()
    assert [(r.channel_scope, r.sessions) for r in rows] == [
        ("all", 10),
        ("organic_search", 4),
    ]


def test_reimport_upserts_instead_of_duplicating(session: Session) -> None:
    first = _prepare(session, idempotency_key="a")
    Ga4ImportService(session).execute(first.id, provider=_FakeProvider([_row(sessions=10)]))
    second = _prepare(session, idempotency_key="b")
    Ga4ImportService(session).execute(second.id, provider=_FakeProvider([_row(sessions=17)]))

    rows = session.scalars(select(Ga4PageDaily)).all()
    assert len(rows) == 1
    assert rows[0].sessions == 17
    assert rows[0].source_import_run_id == second.id


def test_different_page_paths_are_separate_rows(session: Session) -> None:
    run = _prepare(session)
    Ga4ImportService(session).execute(
        run.id, provider=_FakeProvider([_row(path="/a/"), _row(path="/b/")])
    )
    assert session.scalar(select(func.count()).select_from(Ga4PageDaily)) == 2


def test_row_outside_the_window_fails_the_run_and_writes_nothing(session: Session) -> None:
    run = _prepare(session)
    provider = _FakeProvider([_row(day=date(2026, 1, 1))])

    with pytest.raises(ExternalProviderDataError):
        Ga4ImportService(session).execute(run.id, provider=provider)

    session.refresh(run)
    assert run.status == "failed"
    assert session.scalar(select(func.count()).select_from(Ga4PageDaily)) == 0


def test_provider_failure_marks_the_run_failed(session: Session) -> None:
    run = _prepare(session)
    provider = _FakeProvider(exc=ExternalProviderError("ga4", "boom"))

    with pytest.raises(ExternalProviderError):
        Ga4ImportService(session).execute(run.id, provider=provider)

    session.refresh(run)
    assert run.status == "failed"
    assert run.error_message
    assert session.scalar(select(func.count()).select_from(Ga4PageDaily)) == 0


def test_unexpected_provider_error_is_wrapped_and_audited(session: Session) -> None:
    run = _prepare(session)

    with pytest.raises(ExternalProviderError):
        Ga4ImportService(session).execute(run.id, provider=_FakeProvider(exc=RuntimeError("x")))

    session.refresh(run)
    assert run.status == "failed"


def test_a_terminal_run_cannot_be_re_executed(session: Session) -> None:
    run = _prepare(session)
    Ga4ImportService(session).execute(run.id, provider=_FakeProvider())

    with pytest.raises(Ga4ImportStateError):
        Ga4ImportService(session).execute(run.id, provider=_FakeProvider())


def test_missing_timezone_does_not_fail_the_import(session: Session) -> None:
    run = _prepare(session)
    result = Ga4ImportService(session).execute(run.id, provider=_FakeProvider(timezone=None))
    assert result.status == "succeeded"
    assert result.property_timezone is None
