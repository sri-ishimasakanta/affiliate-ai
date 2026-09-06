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
    EXIT_NOT_CONFIGURED,
    EXIT_OK,
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
