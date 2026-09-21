"""scripts/import_search_console.py — 管理用インポート CLI の orchestration。

Google へは一切通信しない (fake provider / stub settings / in-memory session)。
provider 自体のテストは C1-B で網羅済みなので重複しない。
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Article,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
    SearchConsoleQueryDaily,
    WordPressPublicationRun,
)
from app.search_console.date_window import recent_window
from app.search_console.rows import SearchConsolePageRow, SearchConsoleQueryRow
from scripts.import_search_console import (
    EXIT_INVALID_RANGE,
    EXIT_NOT_CONFIGURED,
    EXIT_OK,
    EXIT_STATE,
    FIRST_IMPORT_IDEMPOTENCY_KEY,
    run,
)

_PROP = "sc-domain:example.test"
_PAGE = "https://example.test/article/"
_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)  # 固定: PT 2026-09-07 05:00


def _settings(*, prop: str | None = _PROP, cred: str | None = "/outside/repo/sa.json"):
    return SimpleNamespace(
        search_console_property_uri=prop,
        search_console_credentials_file=cred,
    )


class _FakeProvider:
    def __init__(
        self,
        *,
        page_rows=(),
        query_rows=(),
        must_not_be_called: bool = False,
    ) -> None:
        self._page = list(page_rows)
        self._query = list(query_rows)
        self._must_not = must_not_be_called
        self.page_calls = 0
        self.query_calls = 0
        self.search_console_request_count = 2
        self.token_refresh_count = 1

    def fetch_page_daily(self, *, property_uri, start_date, end_date):
        self.page_calls += 1
        if self._must_not:
            raise AssertionError("provider must not be invoked")
        return list(self._page)

    def fetch_query_daily(self, *, property_uri, start_date, end_date):
        self.query_calls += 1
        if self._must_not:
            raise AssertionError("provider must not be invoked")
        return list(self._query)


def _sf(session: Session):
    return lambda: contextlib.nullcontext(session)


def _counts(session: Session) -> tuple[int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(SearchConsoleImportRun)),
        session.scalar(select(func.count()).select_from(SearchConsolePageDaily)),
        session.scalar(select(func.count()).select_from(SearchConsoleQueryDaily)),
    )


def _win() -> tuple[date, date]:
    return recent_window(days=7, end_lag_days=1, now=_NOW)


def _page_row(d: date) -> SearchConsolePageRow:
    return SearchConsolePageRow(
        metric_date=d, page=_PAGE, clicks=3, impressions=100, ctr=0.03, position=12.5
    )


def _query_row(d: date) -> SearchConsoleQueryRow:
    return SearchConsoleQueryRow(
        metric_date=d, page=_PAGE, query="業務効率化 ツール", clicks=2,
        impressions=60, ctr=0.033, position=9.1,
    )


# ==========================================================================
# plan mode
# ==========================================================================
def test_plan_mode_prints_pt_window_and_makes_no_db_write_no_google(
    session: Session, capsys
) -> None:
    provider = _FakeProvider(must_not_be_called=True)
    code = run(
        execute=False,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: provider,
        now=_NOW,
    )
    out = capsys.readouterr().out
    start, end = _win()
    assert code == EXIT_OK
    assert f"start_date (PT)        = {start.isoformat()}" in out
    assert f"end_date (PT)          = {end.isoformat()}" in out
    assert "plan only" in out
    assert _counts(session) == (0, 0, 0)
    assert provider.page_calls == 0 and provider.query_calls == 0


def test_plan_mode_not_configured_without_property(session: Session) -> None:
    code = run(
        execute=False,
        settings=_settings(prop=None),
        session_factory=_sf(session),
        now=_NOW,
    )
    assert code == EXIT_NOT_CONFIGURED


def test_plan_mode_not_configured_without_credentials(session: Session) -> None:
    code = run(
        execute=False,
        settings=_settings(cred=None),
        session_factory=_sf(session),
        now=_NOW,
    )
    assert code == EXIT_NOT_CONFIGURED


# ==========================================================================
# execute mode
# ==========================================================================
def test_execute_zero_row_success_uses_property_and_idempotency_key(
    session: Session, capsys
) -> None:
    provider = _FakeProvider(page_rows=[], query_rows=[])
    code = run(
        execute=True,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: provider,
        now=_NOW,
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert provider.page_calls == 1 and provider.query_calls == 1

    row = session.scalars(select(SearchConsoleImportRun)).one()
    assert row.status == "succeeded"
    assert row.property_uri == _PROP
    assert row.idempotency_key == FIRST_IMPORT_IDEMPOTENCY_KEY
    start, end = _win()
    assert row.start_date == start and row.end_date == end
    assert row.page_rows_received == 0 and row.query_rows_received == 0
    assert _counts(session) == (1, 0, 0)
    assert "zero-row import: SUCCESS" in out


def test_execute_nonzero_result_reporting_and_persistence(
    session: Session, capsys
) -> None:
    start, end = _win()
    provider = _FakeProvider(
        page_rows=[_page_row(start)], query_rows=[_query_row(end)]
    )
    code = run(
        execute=True,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: provider,
        now=_NOW,
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK

    row = session.scalars(select(SearchConsoleImportRun)).one()
    assert row.status == "succeeded"
    assert row.page_rows_upserted == 1 and row.query_rows_upserted == 1
    assert "page_rows_upserted    = 1" in out
    assert "query_rows_upserted   = 1" in out

    p = session.scalars(select(SearchConsolePageDaily)).one()
    assert p.property_uri == _PROP and p.source_import_run_id == row.id
    assert start <= p.metric_date <= end
    q = session.scalars(select(SearchConsoleQueryDaily)).one()
    assert q.source_import_run_id == row.id and q.query == "業務効率化 ツール"


def test_execute_output_excludes_secrets(session: Session, capsys) -> None:
    provider = _FakeProvider(page_rows=[], query_rows=[])
    run(
        execute=True,
        settings=_settings(cred="/outside/repo/super-secret-sa.json"),
        session_factory=_sf(session),
        provider_factory=lambda: provider,
        now=_NOW,
    )
    out = capsys.readouterr().out
    for banned in (
        "private_key",
        "BEGIN PRIVATE KEY",
        "Bearer",
        "access_token",
        "Authorization",
        "super-secret-sa.json",
        "/outside/repo",
    ):
        assert banned not in out


def test_repeat_execute_same_key_is_idempotent_no_second_provider_call(
    session: Session,
) -> None:
    p1 = _FakeProvider(page_rows=[], query_rows=[])
    assert run(
        execute=True,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: p1,
        now=_NOW,
    ) == EXIT_OK
    counts_after_first = _counts(session)
    assert counts_after_first == (1, 0, 0)

    p2 = _FakeProvider(must_not_be_called=True)
    code = run(
        execute=True,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: p2,
        now=_NOW,
    )
    assert code == EXIT_OK
    assert p2.page_calls == 0 and p2.query_calls == 0
    assert _counts(session) == counts_after_first  # no duplicate run, no new metrics
    assert session.scalars(select(SearchConsoleImportRun)).one().status == "succeeded"


def test_execute_does_not_mutate_article_or_wordpress(session: Session) -> None:
    art_before = session.scalar(select(func.count()).select_from(Article))
    pub_before = session.scalar(
        select(func.count()).select_from(WordPressPublicationRun)
    )
    provider = _FakeProvider(page_rows=[_page_row(_win()[0])], query_rows=[])
    run(
        execute=True,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: provider,
        now=_NOW,
    )
    assert session.scalar(select(func.count()).select_from(Article)) == art_before
    assert (
        session.scalar(select(func.count()).select_from(WordPressPublicationRun))
        == pub_before
    )


def test_main_rejects_unknown_flag() -> None:
    from scripts.import_search_console import main

    with pytest.raises(SystemExit) as exc:
        main(["--not-a-flag"])
    assert exc.value.code == 2

# ==========================================================================
# Phase C1.1: refreshable import (explicit range / rolling window)
# ==========================================================================
# run の監査 idempotency (実行ごとに新しい run) とメトリクス行の idempotency
# ((property, date, page[, query]) の UPSERT) は別物であることを検証する。
class _RecordingProvider(_FakeProvider):
    def __init__(self, *, page_rows=(), query_rows=()) -> None:
        super().__init__(page_rows=page_rows, query_rows=query_rows)
        self.page_args: list[tuple] = []
        self.query_args: list[tuple] = []

    def fetch_page_daily(self, *, property_uri, start_date, end_date):
        self.page_args.append((property_uri, start_date, end_date))
        return super().fetch_page_daily(
            property_uri=property_uri, start_date=start_date, end_date=end_date
        )

    def fetch_query_daily(self, *, property_uri, start_date, end_date):
        self.query_args.append((property_uri, start_date, end_date))
        return super().fetch_query_daily(
            property_uri=property_uri, start_date=start_date, end_date=end_date
        )


def _prow(d: date, clicks: int = 3, page: str = _PAGE) -> SearchConsolePageRow:
    return SearchConsolePageRow(
        metric_date=d, page=page, clicks=clicks, impressions=100, ctr=clicks / 100, position=8.0
    )


def _qrow(d: date, clicks: int = 2, query: str = "業務効率化 ツール") -> SearchConsoleQueryRow:
    return SearchConsoleQueryRow(
        metric_date=d,
        page=_PAGE,
        query=query,
        clicks=clicks,
        impressions=50,
        ctr=clicks / 50,
        position=7.5,
    )


def _refresh(session: Session, provider, **range_kwargs) -> int:
    return run(
        execute=True,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: provider,
        now=_NOW,
        **range_kwargs,
    )


def _runs(session: Session) -> list[SearchConsoleImportRun]:
    return list(session.scalars(select(SearchConsoleImportRun).order_by(SearchConsoleImportRun.id)))


_INVALID_RANGES = [
    pytest.param({"start_date": "2026-09-01"}, id="start-only"),
    pytest.param({"end_date": "2026-09-05"}, id="end-only"),
    pytest.param({"start_date": "2026-13-01", "end_date": "2026-09-05"}, id="invalid-month"),
    pytest.param({"start_date": "2026-02-30", "end_date": "2026-09-05"}, id="impossible-day"),
    pytest.param({"start_date": "20260901", "end_date": "2026-09-05"}, id="compact-format"),
    pytest.param({"start_date": "2026-9-1", "end_date": "2026-09-05"}, id="unpadded"),
    pytest.param({"start_date": "not-a-date", "end_date": "2026-09-05"}, id="garbage"),
    pytest.param({"start_date": "2026-09-05", "end_date": "2026-09-01"}, id="start-after-end"),
    pytest.param({"start_date": "2026-09-01", "end_date": "2026-09-08"}, id="future-end-PT"),
    pytest.param({"days": 7}, id="days-only"),
    pytest.param({"lag": 1}, id="lag-only"),
    pytest.param({"days": 0, "lag": 1}, id="days-zero"),
    pytest.param({"days": 7, "lag": -1}, id="negative-lag"),
    pytest.param(
        {"start_date": "2026-09-01", "end_date": "2026-09-05", "days": 7, "lag": 1},
        id="conflicting-modes",
    ),
    pytest.param({"start_date": "2026-09-01", "days": 7}, id="conflicting-partial"),
]


def _forbid_everything(monkeypatch) -> dict:
    """設定 / DB / provider のどれに触れても失敗させる。"""

    import scripts.import_search_console as m

    touched: dict[str, bool] = {}

    def _boom(name):
        def inner(*_a, **_kw):
            touched[name] = True
            raise AssertionError(f"{name} must not be touched before range validation")

        return inner

    monkeypatch.setattr(m, "get_settings", _boom("get_settings"))
    monkeypatch.setattr(m, "SessionLocal", _boom("SessionLocal"))
    return touched


@pytest.mark.parametrize("execute", [False, True], ids=["plan", "execute"])
@pytest.mark.parametrize("range_kwargs", _INVALID_RANGES)
def test_invalid_range_is_rejected_before_settings_db_or_provider(
    monkeypatch, capsys, range_kwargs, execute
) -> None:
    touched = _forbid_everything(monkeypatch)

    def _no_session():
        touched["session_factory"] = True
        raise AssertionError("session must not be opened")

    def _no_provider():
        touched["provider_factory"] = True
        raise AssertionError("provider must not be created")

    code = run(
        execute=execute,
        settings=None,  # settings=None なら get_settings() が呼ばれる -> 呼ばれたら失敗
        session_factory=_no_session,
        provider_factory=_no_provider,
        now=_NOW,
        **range_kwargs,
    )
    out = capsys.readouterr().out
    assert code == EXIT_INVALID_RANGE
    assert "INVALID RANGE" in out
    assert touched == {}


def test_main_rejects_half_a_date_pair_before_any_io(monkeypatch, capsys) -> None:
    from scripts.import_search_console import main

    touched = _forbid_everything(monkeypatch)
    assert main(["--start-date", "2026-09-01"]) == EXIT_INVALID_RANGE
    assert main(["--days", "0", "--lag", "1", "--execute"]) == EXIT_INVALID_RANGE
    assert "INVALID RANGE" in capsys.readouterr().out
    assert touched == {}


def test_main_rejects_non_integer_days_at_argparse() -> None:
    from scripts.import_search_console import main

    with pytest.raises(SystemExit) as exc:
        main(["--days", "abc", "--lag", "1"])
    assert exc.value.code == 2


def test_end_date_equal_to_pt_today_is_allowed(session: Session, capsys) -> None:
    provider = _FakeProvider(must_not_be_called=True)
    code = run(
        execute=False,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: provider,
        now=_NOW,
        start_date="2026-09-07",
        end_date="2026-09-07",  # PT 今日 (UTC の同日でも PT でも未来ではない)
    )
    assert code == EXIT_OK
    assert "end_date (PT)          = 2026-09-07" in capsys.readouterr().out


def test_plan_with_explicit_range_is_zero_http_zero_db(session: Session, capsys) -> None:
    provider = _FakeProvider(must_not_be_called=True)
    code = run(
        execute=False,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: provider,
        now=_NOW,
        start_date="2026-09-01",
        end_date="2026-09-05",
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "mode                   = explicit-range" in out
    assert "start_date (PT)        = 2026-09-01" in out
    assert "end_date (PT)          = 2026-09-05" in out
    assert "every execute appends a new audited run" in out
    assert FIRST_IMPORT_IDEMPOTENCY_KEY not in out
    assert "plan only" in out
    assert _counts(session) == (0, 0, 0)
    assert provider.page_calls == 0 and provider.query_calls == 0


def test_plan_with_rolling_window_uses_pt_helper_and_is_zero_http_zero_db(
    session: Session, capsys
) -> None:
    provider = _FakeProvider(must_not_be_called=True)
    start, end = recent_window(days=28, end_lag_days=3, now=_NOW)
    code = run(
        execute=False,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: provider,
        now=_NOW,
        days=28,
        lag=3,
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "mode                   = rolling-window" in out
    assert f"start_date (PT)        = {start.isoformat()}" in out
    assert f"end_date (PT)          = {end.isoformat()}" in out
    assert _counts(session) == (0, 0, 0)
    assert provider.page_calls == 0 and provider.query_calls == 0


def test_plan_without_range_is_still_the_first_import_plan(session: Session, capsys) -> None:
    code = run(
        execute=False,
        settings=_settings(),
        session_factory=_sf(session),
        provider_factory=lambda: _FakeProvider(must_not_be_called=True),
        now=_NOW,
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "mode                   = first-import" in out
    assert f"idempotency_key        = {FIRST_IMPORT_IDEMPOTENCY_KEY}" in out


def test_execute_passes_exact_requested_dates_to_provider_and_run(session: Session) -> None:
    provider = _RecordingProvider(
        page_rows=[_prow(date(2026, 9, 2))], query_rows=[_qrow(date(2026, 9, 3))]
    )
    assert _refresh(session, provider, start_date="2026-09-01", end_date="2026-09-05") == EXIT_OK

    expected = (_PROP, date(2026, 9, 1), date(2026, 9, 5))
    assert provider.page_args == [expected]
    assert provider.query_args == [expected]
    (row,) = _runs(session)
    assert row.status == "succeeded"
    assert (row.start_date, row.end_date) == (date(2026, 9, 1), date(2026, 9, 5))
    assert row.idempotency_key is None  # refresh は固定 first-import key を使わない
    assert _counts(session) == (1, 1, 1)


def test_execute_rolling_window_passes_pt_window_to_provider(session: Session) -> None:
    provider = _RecordingProvider()
    assert _refresh(session, provider, days=28, lag=3) == EXIT_OK
    start, end = recent_window(days=28, end_lag_days=3, now=_NOW)
    assert provider.page_args == [(_PROP, start, end)]
    (row,) = _runs(session)
    assert (row.start_date, row.end_date) == (start, end)
    assert row.idempotency_key is None


def test_repeat_refresh_of_same_range_creates_new_audited_run_without_duplicate_metrics(
    session: Session,
) -> None:
    d = date(2026, 9, 3)
    p1 = _RecordingProvider(page_rows=[_prow(d, clicks=3)], query_rows=[_qrow(d, clicks=2)])
    assert _refresh(session, p1, start_date="2026-09-01", end_date="2026-09-05") == EXIT_OK
    run1 = _runs(session)[0]

    # Search Console のデータが settle して値が変わった想定。
    p2 = _RecordingProvider(page_rows=[_prow(d, clicks=9)], query_rows=[_qrow(d, clicks=4)])
    assert _refresh(session, p2, start_date="2026-09-01", end_date="2026-09-05") == EXIT_OK

    runs = _runs(session)
    assert len(runs) == 2  # 実行ごとに新しい監査 run
    assert runs[0].id == run1.id and runs[1].id != run1.id
    assert all(r.status == "succeeded" for r in runs)
    assert all(r.idempotency_key is None for r in runs)
    assert len(p2.page_args) == 1 and len(p2.query_args) == 1  # 2 回目も実際に取得する

    # メトリクス行は重複せず UPSERT。最新値と最新 run を指す。
    assert _counts(session) == (2, 1, 1)
    page = session.scalars(select(SearchConsolePageDaily)).one()
    query = session.scalars(select(SearchConsoleQueryDaily)).one()
    assert page.clicks == 9 and page.source_import_run_id == runs[1].id
    assert query.clicks == 4 and query.source_import_run_id == runs[1].id
    # 1 回目の run の記録は不変。
    assert runs[0].page_rows_upserted == 1 and runs[0].query_rows_upserted == 1


def test_overlapping_ranges_upsert_shared_days_and_add_only_new_days(session: Session) -> None:
    d4, d5, d6 = date(2026, 9, 4), date(2026, 9, 5), date(2026, 9, 6)
    a = _RecordingProvider(
        page_rows=[_prow(d4, 1), _prow(d5, 1)], query_rows=[_qrow(d4, 1), _qrow(d5, 1)]
    )
    assert _refresh(session, a, start_date="2026-09-01", end_date="2026-09-05") == EXIT_OK
    assert _counts(session) == (1, 2, 2)

    b = _RecordingProvider(
        page_rows=[_prow(d4, 5), _prow(d5, 5), _prow(d6, 5)],
        query_rows=[_qrow(d4, 5), _qrow(d5, 5), _qrow(d6, 5)],
    )
    assert _refresh(session, b, start_date="2026-09-03", end_date="2026-09-06") == EXIT_OK

    assert _counts(session) == (2, 3, 3)  # d4/d5 は更新、d6 だけ追加
    pages = {r.metric_date: r for r in session.scalars(select(SearchConsolePageDaily))}
    assert set(pages) == {d4, d5, d6}
    assert all(r.clicks == 5 for r in pages.values())
    identities = [
        (r.property_uri, r.metric_date, r.page)
        for r in session.scalars(select(SearchConsolePageDaily))
    ]
    assert len(identities) == len(set(identities))
    q_identities = [
        (r.property_uri, r.metric_date, r.page, r.query)
        for r in session.scalars(select(SearchConsoleQueryDaily))
    ]
    assert len(q_identities) == len(set(q_identities))


def test_refresh_of_first_import_window_is_a_separate_run_and_never_uses_first_key(
    session: Session,
) -> None:
    first = _FakeProvider(page_rows=[], query_rows=[])
    assert (
        run(
            execute=True,
            settings=_settings(),
            session_factory=_sf(session),
            provider_factory=lambda: first,
            now=_NOW,
        )
        == EXIT_OK
    )
    start, end = _win()

    again = _RecordingProvider(page_rows=[_prow(start, 4)], query_rows=[])
    assert (
        _refresh(session, again, start_date=start.isoformat(), end_date=end.isoformat()) == EXIT_OK
    )
    assert len(again.page_args) == 1  # first-import key の解決で握りつぶされない

    runs = _runs(session)
    assert [r.idempotency_key for r in runs] == [FIRST_IMPORT_IDEMPOTENCY_KEY, None]
    assert runs[0].import_identity_hash == runs[1].import_identity_hash
    assert _counts(session) == (2, 1, 0)


def test_refresh_reuses_an_unfinished_prepared_run_instead_of_duplicating(
    session: Session,
) -> None:
    from app.services.search_console_import_service import SearchConsoleImportService

    prepared = SearchConsoleImportService(session).prepare(
        property_uri=_PROP,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 5),
        now=_NOW,
    )
    provider = _RecordingProvider(page_rows=[_prow(date(2026, 9, 2))], query_rows=[])
    assert _refresh(session, provider, start_date="2026-09-01", end_date="2026-09-05") == EXIT_OK

    (row,) = _runs(session)
    assert row.id == prepared.id and row.status == "succeeded"


def test_refresh_fails_closed_when_same_range_is_already_running(session: Session) -> None:
    from app.services.search_console_import_service import SearchConsoleImportService

    svc = SearchConsoleImportService(session)
    prepared = svc.prepare(
        property_uri=_PROP, start_date=date(2026, 9, 1), end_date=date(2026, 9, 5), now=_NOW
    )
    prepared.status = "running"
    session.commit()

    provider = _FakeProvider(must_not_be_called=True)
    code = _refresh(session, provider, start_date="2026-09-01", end_date="2026-09-05")
    assert code == EXIT_STATE
    assert provider.page_calls == 0 and provider.query_calls == 0
    assert len(_runs(session)) == 1


def test_refresh_output_excludes_secrets(session: Session, capsys) -> None:
    provider = _FakeProvider(page_rows=[], query_rows=[])
    run(
        execute=True,
        settings=_settings(cred="/outside/repo/super-secret-sa.json"),
        session_factory=_sf(session),
        provider_factory=lambda: provider,
        now=_NOW,
        start_date="2026-09-01",
        end_date="2026-09-05",
    )
    out = capsys.readouterr().out
    for banned in (
        "private_key",
        "BEGIN PRIVATE KEY",
        "Bearer",
        "access_token",
        "Authorization",
        "super-secret-sa.json",
        "/outside/repo",
    ):
        assert banned not in out
