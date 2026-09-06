"""SearchConsoleImportService.prepare / execute: lifecycle / idempotency / upsert /
provider failure。Google API へは一切通信しない (FakeSearchConsoleProvider を注入)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.exceptions import (
    EntityNotFoundError,
    ExternalProviderDataError,
    ExternalProviderError,
    SearchConsoleImportStateError,
)
from app.models import (
    Article,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
    SearchConsoleQueryDaily,
    WordPressPublicationRun,
)
from app.search_console.rows import SearchConsolePageRow, SearchConsoleQueryRow
from app.services.search_console_import_service import SearchConsoleImportService

_PROP = "sc-domain:example.test"
_PAGE = "https://example.test/article/"
_D1 = date(2026, 9, 1)
_D2 = date(2026, 9, 5)


class FakeSearchConsoleProvider:
    def __init__(
        self,
        *,
        page_rows: list[SearchConsolePageRow] | None = None,
        query_rows: list[SearchConsoleQueryRow] | None = None,
        page_exc: BaseException | None = None,
        query_exc: BaseException | None = None,
    ) -> None:
        self._page_rows = page_rows if page_rows is not None else []
        self._query_rows = query_rows if query_rows is not None else []
        self._page_exc = page_exc
        self._query_exc = query_exc
        self.page_calls = 0
        self.query_calls = 0

    def fetch_page_daily(self, *, property_uri, start_date, end_date):
        self.page_calls += 1
        if self._page_exc is not None:
            raise self._page_exc
        return list(self._page_rows)

    def fetch_query_daily(self, *, property_uri, start_date, end_date):
        self.query_calls += 1
        if self._query_exc is not None:
            raise self._query_exc
        return list(self._query_rows)


def _svc(session: Session) -> SearchConsoleImportService:
    return SearchConsoleImportService(session)


def _prepare(session: Session, **over):
    kw = dict(property_uri=_PROP, start_date=_D1, end_date=_D2)
    kw.update(over)
    return _svc(session).prepare(**kw)


def _page(d: date, page: str, clicks: int, impr: int, ctr: float, pos: float):
    return SearchConsolePageRow(
        metric_date=d, page=page, clicks=clicks, impressions=impr, ctr=ctr, position=pos
    )


def _query(d: date, page: str, q: str, clicks: int, impr: int, ctr: float, pos: float):
    return SearchConsoleQueryRow(
        metric_date=d, page=page, query=q, clicks=clicks, impressions=impr, ctr=ctr,
        position=pos,
    )


def _run_count(session) -> int:
    return session.scalar(select(func.count()).select_from(SearchConsoleImportRun))


def _page_count(session) -> int:
    return session.scalar(select(func.count()).select_from(SearchConsolePageDaily))


def _query_count(session) -> int:
    return session.scalar(select(func.count()).select_from(SearchConsoleQueryDaily))


# ==================== prepare =========================================
def test_prepare_happy(session: Session) -> None:
    run = _prepare(session, idempotency_key="sc-k1")
    assert run.status == "prepared"
    assert run.property_uri == _PROP
    assert run.start_date == _D1 and run.end_date == _D2
    assert len(run.import_identity_hash) == 64
    assert run.page_rows_received is None and run.page_rows_upserted is None
    assert run.started_at is None and run.finished_at is None
    assert _run_count(session) == 1


def test_prepare_idempotent_same_key_returns_existing(session: Session) -> None:
    a = _prepare(session, idempotency_key="sc-k1")
    b = _prepare(session, idempotency_key="sc-k1")
    assert a.id == b.id
    assert _run_count(session) == 1


def test_prepare_same_key_different_identity_rejected(session: Session) -> None:
    _prepare(session, idempotency_key="sc-k1")
    with pytest.raises(SearchConsoleImportStateError):
        _prepare(session, idempotency_key="sc-k1", end_date=date(2026, 9, 6))
    assert _run_count(session) == 1


def test_prepare_active_run_same_identity_returns_existing(session: Session) -> None:
    a = _prepare(session)
    b = _prepare(session)
    assert a.id == b.id
    assert _run_count(session) == 1


def test_prepare_rejects_reversed_dates(session: Session) -> None:
    with pytest.raises(SearchConsoleImportStateError):
        _prepare(session, start_date=_D2, end_date=_D1)
    assert _run_count(session) == 0


def test_prepare_rejects_future_end_date(session: Session) -> None:
    future = (datetime.now(UTC).date().replace(year=datetime.now(UTC).year + 1))
    with pytest.raises(SearchConsoleImportStateError):
        _prepare(session, start_date=_D1, end_date=future)
    assert _run_count(session) == 0


def test_prepare_accepts_window_longer_than_400_days(session: Session) -> None:
    """期間の長さそのものに対する application 側の arbitrary cap は無い。"""

    from datetime import timedelta

    end = datetime.now(UTC).date() - timedelta(days=3)
    start = end - timedelta(days=600)  # > 400 日
    run = _prepare(session, start_date=start, end_date=end, idempotency_key="wide-k1")
    assert run.status == "prepared"
    assert run.start_date == start and run.end_date == end
    assert len(run.import_identity_hash) == 64
    assert _run_count(session) == 1
    assert run.started_at is None  # no provider call during prepare


def test_prepare_wide_window_identity_is_deterministic(session: Session) -> None:
    from datetime import timedelta

    end = datetime.now(UTC).date() - timedelta(days=3)
    start = end - timedelta(days=600)
    a = _prepare(session, start_date=start, end_date=end, idempotency_key="wide-a")
    b = _prepare(session, start_date=start, end_date=end, idempotency_key="wide-a")
    assert a.id == b.id
    assert a.import_identity_hash == b.import_identity_hash
    assert _run_count(session) == 1


def test_prepare_rejects_missing_property(session: Session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.search_console_import_service.get_settings",
        lambda: type("S", (), {"search_console_property_uri": None})(),
    )
    with pytest.raises(SearchConsoleImportStateError):
        _svc(session).prepare(start_date=_D1, end_date=_D2)
    assert _run_count(session) == 0


# ==================== execute happy / zero ============================
def test_execute_happy_upserts_metrics_and_succeeds(session: Session) -> None:
    run = _prepare(session)
    provider = FakeSearchConsoleProvider(
        page_rows=[_page(_D1, _PAGE, 3, 100, 0.03, 12.5)],
        query_rows=[
            _query(_D1, _PAGE, "業務効率化 ツール", 2, 60, 0.0333, 9.1),
            _query(_D1, _PAGE, "make 自動化", 1, 40, 0.025, 15.2),
        ],
    )
    out = _svc(session).execute(run.id, provider=provider)

    assert out.status == "succeeded"
    assert provider.page_calls == 1 and provider.query_calls == 1
    assert out.page_rows_received == 1 and out.query_rows_received == 2
    assert out.page_rows_upserted == 1 and out.query_rows_upserted == 2
    assert out.started_at is not None and out.finished_at is not None
    assert out.error_message is None
    assert out.response_snapshot["page_rows_upserted"] == 1
    assert "raw" not in out.response_snapshot  # no raw blobs

    assert _page_count(session) == 1
    assert _query_count(session) == 2
    p = session.scalars(select(SearchConsolePageDaily)).first()
    assert p.property_uri == _PROP and p.page == _PAGE
    assert p.clicks == 3 and p.impressions == 100
    assert p.ctr == pytest.approx(0.03) and p.position == pytest.approx(12.5)
    assert p.source_import_run_id == run.id


def test_execute_zero_page_rows(session: Session) -> None:
    run = _prepare(session)
    provider = FakeSearchConsoleProvider(page_rows=[], query_rows=[])
    out = _svc(session).execute(run.id, provider=provider)
    assert out.status == "succeeded"
    assert out.page_rows_received == 0 and out.query_rows_received == 0
    assert _page_count(session) == 0 and _query_count(session) == 0


def test_execute_zero_query_rows_only(session: Session) -> None:
    run = _prepare(session)
    provider = FakeSearchConsoleProvider(
        page_rows=[_page(_D1, _PAGE, 0, 5, 0.0, 40.0)], query_rows=[]
    )
    out = _svc(session).execute(run.id, provider=provider)
    assert out.status == "succeeded"
    assert _page_count(session) == 1 and _query_count(session) == 0


# ==================== upsert semantics ===============================
def test_execute_reimport_updates_row_without_duplicate(session: Session) -> None:
    run1 = _prepare(session, idempotency_key="k1")
    p1 = FakeSearchConsoleProvider(
        page_rows=[_page(_D1, _PAGE, 3, 100, 0.03, 12.5)],
        query_rows=[_query(_D1, _PAGE, "q", 1, 10, 0.1, 5.0)],
    )
    _svc(session).execute(run1.id, provider=p1)
    assert _page_count(session) == 1 and _query_count(session) == 1

    # second run, same identity window, settled values
    run2 = _prepare(session, idempotency_key="k2")
    p2 = FakeSearchConsoleProvider(
        page_rows=[_page(_D1, _PAGE, 5, 120, 0.0417, 10.9)],
        query_rows=[_query(_D1, _PAGE, "q", 2, 12, 0.1667, 4.2)],
    )
    _svc(session).execute(run2.id, provider=p2)

    assert _page_count(session) == 1  # still one row, not duplicated
    assert _query_count(session) == 1
    p = session.scalars(select(SearchConsolePageDaily)).first()
    assert p.clicks == 5 and p.impressions == 120
    assert p.source_import_run_id == run2.id  # latest run occupies the row
    q = session.scalars(select(SearchConsoleQueryDaily)).first()
    assert q.clicks == 2 and q.source_import_run_id == run2.id


# ==================== provider failures / validation =================
def test_execute_provider_error_marks_failed_no_metrics(session: Session) -> None:
    run = _prepare(session)
    provider = FakeSearchConsoleProvider(
        page_exc=ExternalProviderError("search_console", "quota exceeded")
    )
    with pytest.raises(ExternalProviderError):
        _svc(session).execute(run.id, provider=provider)
    persisted = session.get(SearchConsoleImportRun, run.id)
    assert persisted.status == "failed"
    assert persisted.finished_at is not None
    assert _page_count(session) == 0 and _query_count(session) == 0


def test_execute_unexpected_provider_exception_marks_failed(session: Session) -> None:
    run = _prepare(session)
    provider = FakeSearchConsoleProvider(query_exc=RuntimeError("boom"))
    with pytest.raises(ExternalProviderError):
        _svc(session).execute(run.id, provider=provider)
    assert session.get(SearchConsoleImportRun, run.id).status == "failed"
    assert _page_count(session) == 0


def test_execute_invalid_page_row_marks_failed(session: Session) -> None:
    run = _prepare(session)
    bad = _page(_D1, "not-a-url", 1, 10, 0.1, 5.0)
    provider = FakeSearchConsoleProvider(page_rows=[bad], query_rows=[])
    with pytest.raises(ExternalProviderDataError):
        _svc(session).execute(run.id, provider=provider)
    assert session.get(SearchConsoleImportRun, run.id).status == "failed"
    assert _page_count(session) == 0


def test_execute_invalid_ctr_marks_failed(session: Session) -> None:
    run = _prepare(session)
    bad = _page(_D1, _PAGE, 1, 10, 1.5, 5.0)  # ctr > 1
    provider = FakeSearchConsoleProvider(page_rows=[bad])
    with pytest.raises(ExternalProviderDataError):
        _svc(session).execute(run.id, provider=provider)
    assert session.get(SearchConsoleImportRun, run.id).status == "failed"


def test_execute_empty_query_string_marks_failed(session: Session) -> None:
    run = _prepare(session)
    provider = FakeSearchConsoleProvider(
        page_rows=[_page(_D1, _PAGE, 1, 10, 0.1, 5.0)],
        query_rows=[_query(_D1, _PAGE, "  ", 1, 10, 0.1, 5.0)],
    )
    with pytest.raises(ExternalProviderDataError):
        _svc(session).execute(run.id, provider=provider)
    assert session.get(SearchConsoleImportRun, run.id).status == "failed"
    assert _page_count(session) == 0  # nothing persisted on validation failure


# ==================== lifecycle guards ==============================
def test_execute_missing_run_404(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _svc(session).execute(999999, provider=FakeSearchConsoleProvider())


def test_execute_rejects_already_running(session: Session) -> None:
    run = _prepare(session)
    run.status = "running"
    session.flush()
    with pytest.raises(SearchConsoleImportStateError):
        _svc(session).execute(run.id, provider=FakeSearchConsoleProvider())


def test_execute_rejects_already_succeeded(session: Session) -> None:
    run = _prepare(session)
    _svc(session).execute(run.id, provider=FakeSearchConsoleProvider())
    with pytest.raises(SearchConsoleImportStateError):
        _svc(session).execute(run.id, provider=FakeSearchConsoleProvider())


# ==================== isolation ====================================
def test_import_does_not_touch_article_or_wordpress_tables(session: Session) -> None:
    art_before = session.scalar(select(func.count()).select_from(Article))
    pub_before = session.scalar(select(func.count()).select_from(WordPressPublicationRun))
    run = _prepare(session)
    _svc(session).execute(
        run.id,
        provider=FakeSearchConsoleProvider(
            page_rows=[_page(_D1, _PAGE, 1, 10, 0.1, 5.0)]
        ),
    )
    assert session.scalar(select(func.count()).select_from(Article)) == art_before
    assert (
        session.scalar(select(func.count()).select_from(WordPressPublicationRun))
        == pub_before
    )
