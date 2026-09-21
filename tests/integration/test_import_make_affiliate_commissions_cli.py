"""scripts/import_make_affiliate_commissions.py -- Phase E1 CLI の PLAN / EXECUTE。

安全上の注意:
- CLI wiring (``main`` / ``cmd_import``) を経由するテストは、必ず ``monkeypatch``
  で ``m.SessionLocal`` をテスト用インメモリ engine に差し替えてから呼ぶ --
  production DB ファイルには絶対に触れない。
- ``get_settings()`` は ``@lru_cache`` されているため、``monkeypatch.setenv`` の
  直後に ``get_settings.cache_clear()`` を呼ぶ (既存の
  ``tests/integration/test_wordpress_draft_run_service.py`` と同じ規律) --
  呼ばなければ他テストでキャッシュされた古い設定を見てしまう。``finally`` で
  再度 clear し、後続テストへ影響を残さない。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.services.affiliate_commission_import_service as commission_import_service_mod
import scripts.import_make_affiliate_commissions as m
from app.config.settings import get_settings
from app.models import AffiliateCommissionFact
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository

_TOKEN = "synthetic-make-api-token-not-real"
_DATE_ARGS = ["--date-from", "2026-09-01", "--date-to", "2026-09-30"]


def _program(session: Session) -> int:
    p = AffiliateProgramRepository(session).create(
        name="Make", provider="make", status=AffiliateProgramStatus.ACTIVE
    )
    session.commit()
    return p.id


def _fact_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateCommissionFact))


def _row(rid: int) -> dict:
    return {
        "id": rid,
        "organization_id": 1,
        "type": "commission",
        "status": "requested",
        "commission": 5.0,
        "source": "referral",
        "created": "2026-09-01T00:00:00+00:00",
        "payout_requested": None,
        "payout_approved": None,
        "payout_realized": None,
    }


# ==================== CLI wiring: no token argument ======================
def test_cli_has_no_token_style_argument() -> None:
    base = ["--affiliate-program-id", "1"]
    for flag in ("--api-token", "--make-api-token", "--token"):
        with pytest.raises(SystemExit):
            m._parse_args([*base, flag, "x"])


def test_cli_default_mode_is_plan() -> None:
    args = m._parse_args(["--affiliate-program-id", "1"])
    assert args.execute is False


def test_cli_execute_flag_explicit() -> None:
    args = m._parse_args(["--affiliate-program-id", "1", "--execute"])
    assert args.execute is True


def test_cli_date_args_parsed_as_dates() -> None:
    args = m._parse_args(
        [
            "--affiliate-program-id",
            "1",
            "--date-from",
            "2026-09-01",
            "--date-to",
            "2026-09-30",
        ]
    )
    assert str(args.date_from) == "2026-09-01"
    assert str(args.date_to) == "2026-09-30"


# ==================== end-to-end (isolated session) =======================
def test_cli_end_to_end_plan_no_http_no_db_write(engine: Engine, monkeypatch, capsys) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup_session:
        pid = _program(setup_session)

    monkeypatch.setattr(m, "SessionLocal", factory)
    monkeypatch.setenv("MAKE_API_BASE_URL", "https://api.make.test")
    monkeypatch.setenv("MAKE_API_TOKEN", _TOKEN)
    get_settings.cache_clear()
    try:
        exit_code = m.main(["--affiliate-program-id", str(pid)])
    finally:
        get_settings.cache_clear()

    assert exit_code == m.EXIT_OK
    out = capsys.readouterr().out
    assert "no HTTP request made" in out
    assert _TOKEN not in out
    with factory() as verify_session:
        assert _fact_count(verify_session) == 0


def test_cli_end_to_end_execute_isolated_session_and_safe_output(
    engine: Engine, monkeypatch, capsys
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup_session:
        pid = _program(setup_session)

    monkeypatch.setattr(m, "SessionLocal", factory)
    monkeypatch.setenv("MAKE_API_BASE_URL", "https://api.make.test")
    monkeypatch.setenv("MAKE_API_TOKEN", _TOKEN)
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Token {_TOKEN}"
        return httpx.Response(200, json={"commissions": [_row(1)]})

    real_import = commission_import_service_mod.AffiliateCommissionImportService.import_commissions

    def patched_import(self, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_import(self, **kwargs)

    monkeypatch.setattr(
        commission_import_service_mod.AffiliateCommissionImportService,
        "import_commissions",
        patched_import,
    )

    try:
        exit_code = m.main(["--affiliate-program-id", str(pid), *_DATE_ARGS, "--execute"])
    finally:
        get_settings.cache_clear()

    assert exit_code == m.EXIT_OK
    out = capsys.readouterr().out
    assert "run_id" in out
    assert _TOKEN not in out
    with factory() as verify_session:
        assert _fact_count(verify_session) == 1


def test_cli_exception_redaction_hides_token_on_failure(
    engine: Engine, monkeypatch, capsys
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup_session:
        pid = _program(setup_session)

    monkeypatch.setattr(m, "SessionLocal", factory)
    monkeypatch.setenv("MAKE_API_BASE_URL", "https://api.make.test")
    monkeypatch.setenv("MAKE_API_TOKEN", _TOKEN)
    get_settings.cache_clear()

    def _leaky_failure(self, **_kw):
        raise RuntimeError(f"simulated leak containing token {_TOKEN}")

    monkeypatch.setattr(
        commission_import_service_mod.AffiliateCommissionImportService,
        "import_commissions",
        _leaky_failure,
    )

    try:
        exit_code = m.main(["--affiliate-program-id", str(pid), *_DATE_ARGS, "--execute"])
    finally:
        get_settings.cache_clear()

    assert exit_code == m.EXIT_FAILED
    out = capsys.readouterr().out
    assert _TOKEN not in out
    assert "message withheld" in out


# ==================== Phase E1.4: dates are mandatory for --execute ========
def _isolated_cli(engine: Engine, monkeypatch):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup_session:
        pid = _program(setup_session)
    monkeypatch.setenv("MAKE_API_BASE_URL", "https://api.make.test")
    monkeypatch.setenv("MAKE_API_TOKEN", _TOKEN)
    get_settings.cache_clear()
    return factory, pid


@pytest.mark.parametrize(
    "date_args",
    [[], ["--date-from", "2026-09-01"], ["--date-to", "2026-09-30"]],
    ids=["neither", "from-only", "to-only"],
)
def test_cli_execute_without_both_dates_fails_before_db_and_http(
    engine: Engine, monkeypatch, capsys, date_args
) -> None:
    factory, pid = _isolated_cli(engine, monkeypatch)

    def _no_db(*_a, **_kw):
        raise AssertionError("must fail before opening a DB session")

    def _no_http(*_a, **_kw):
        raise AssertionError("must fail before any HTTP client is created")

    monkeypatch.setattr(m, "SessionLocal", _no_db)
    monkeypatch.setattr(httpx, "Client", _no_http)
    try:
        exit_code = m.main(["--affiliate-program-id", str(pid), *date_args, "--execute"])
    finally:
        get_settings.cache_clear()

    assert exit_code == m.EXIT_FAILED
    out = capsys.readouterr().out
    assert "both required" in out
    assert _TOKEN not in out
    with factory() as verify_session:
        assert _fact_count(verify_session) == 0


def test_cli_execute_never_defaults_a_date_range(engine: Engine, monkeypatch, capsys) -> None:
    """--execute は日付が無ければ失敗するだけで、範囲を補完して実行しない。"""

    _factory, pid = _isolated_cli(engine, monkeypatch)
    called: list = []

    def _spy(self, **kwargs):
        called.append(kwargs)
        raise AssertionError("import_commissions must not be reached without dates")

    monkeypatch.setattr(
        commission_import_service_mod.AffiliateCommissionImportService,
        "import_commissions",
        _spy,
    )
    try:
        exit_code = m.main(["--affiliate-program-id", str(pid), "--execute"])
    finally:
        get_settings.cache_clear()
    assert exit_code == m.EXIT_FAILED
    assert called == []


def test_cli_plan_without_dates_is_allowed_zero_http_and_says_dates_required(
    engine: Engine, monkeypatch, capsys
) -> None:
    factory, pid = _isolated_cli(engine, monkeypatch)
    monkeypatch.setattr(m, "SessionLocal", factory)

    def _no_http(*_a, **_kw):
        raise AssertionError("PLAN must not send HTTP")

    monkeypatch.setattr(httpx, "Client", _no_http)
    try:
        exit_code = m.main(["--affiliate-program-id", str(pid)])
    finally:
        get_settings.cache_clear()

    assert exit_code == m.EXIT_OK
    out = capsys.readouterr().out
    assert "dates_complete          = False" in out
    assert "DATES REQUIRED" in out
    assert "no HTTP request made" in out
    assert _TOKEN not in out
    with factory() as verify_session:
        assert _fact_count(verify_session) == 0


def test_cli_plan_with_both_dates_reports_complete(engine: Engine, monkeypatch, capsys) -> None:
    factory, pid = _isolated_cli(engine, monkeypatch)
    monkeypatch.setattr(m, "SessionLocal", factory)
    try:
        exit_code = m.main(["--affiliate-program-id", str(pid), *_DATE_ARGS])
    finally:
        get_settings.cache_clear()
    assert exit_code == m.EXIT_OK
    out = capsys.readouterr().out
    assert "dates_complete          = True" in out
    assert "DATES REQUIRED" not in out


def test_cli_plan_with_one_date_only_fails(engine: Engine, monkeypatch, capsys) -> None:
    factory, pid = _isolated_cli(engine, monkeypatch)
    monkeypatch.setattr(m, "SessionLocal", factory)
    try:
        exit_code = m.main(["--affiliate-program-id", str(pid), "--date-from", "2026-09-01"])
    finally:
        get_settings.cache_clear()
    assert exit_code == m.EXIT_FAILED
    assert "together" in capsys.readouterr().out


# ==================== Phase E1.8: PLAN vs EXECUTE preflight output ==========
_PLAN_ONLY_PHRASES = (
    "READ-ONLY, no HTTP, no DB mutation",
    "would_execute",
    "no HTTP request made",
    "no DB write performed",
    "Make Commission Import Plan",
)


def _patch_execute_with_mock_transport(monkeypatch, handler) -> None:
    real_import = commission_import_service_mod.AffiliateCommissionImportService.import_commissions

    def patched_import(self, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_import(self, **kwargs)

    monkeypatch.setattr(
        commission_import_service_mod.AffiliateCommissionImportService,
        "import_commissions",
        patched_import,
    )


def _run_execute(engine: Engine, monkeypatch, capsys) -> tuple[int, str, sessionmaker]:
    factory, pid = _isolated_cli(engine, monkeypatch)
    monkeypatch.setattr(m, "SessionLocal", factory)
    _patch_execute_with_mock_transport(
        monkeypatch, lambda request: httpx.Response(200, json={"commissions": [_row(1)]})
    )
    try:
        exit_code = m.main(["--affiliate-program-id", str(pid), *_DATE_ARGS, "--execute"])
    finally:
        get_settings.cache_clear()
    return exit_code, capsys.readouterr().out, factory


def test_cli_plan_output_keeps_readonly_zero_http_zero_write_messaging(
    engine: Engine, monkeypatch, capsys
) -> None:
    factory, pid = _isolated_cli(engine, monkeypatch)
    monkeypatch.setattr(m, "SessionLocal", factory)
    try:
        exit_code = m.main(["--affiliate-program-id", str(pid), *_DATE_ARGS])
    finally:
        get_settings.cache_clear()

    assert exit_code == m.EXIT_OK
    out = capsys.readouterr().out
    for phrase in _PLAN_ONLY_PHRASES:
        assert phrase in out, phrase
    assert "would_execute           = False" in out
    assert "no HTTP request made, no DB write performed. Pass --execute to import." in out
    assert "EXECUTE MODE" not in out
    assert "EXECUTING NOW" not in out
    assert _TOKEN not in out


def test_cli_execute_preflight_does_not_claim_no_http_or_no_db_write(
    engine: Engine, monkeypatch, capsys
) -> None:
    exit_code, out, _factory = _run_execute(engine, monkeypatch, capsys)

    assert exit_code == m.EXIT_OK
    for phrase in _PLAN_ONLY_PHRASES:
        assert phrase not in out, phrase
    assert "READ-ONLY" not in out
    assert "Pass --execute to import" not in out


def test_cli_execute_preflight_clearly_indicates_execute_mode(
    engine: Engine, monkeypatch, capsys
) -> None:
    exit_code, out, _factory = _run_execute(engine, monkeypatch, capsys)

    assert exit_code == m.EXIT_OK
    assert "Make Commission Import Preflight (EXECUTE MODE)" in out
    assert "mode                    = EXECUTE" in out
    assert "EXECUTING NOW" in out
    assert "GET requests to the Make API" in out
    assert "write the import run" in out
    # preflight は実行結果より前に出る
    assert out.index("EXECUTE MODE") < out.index("=== Make Commission Import Execution ===")
    assert out.index("EXECUTING NOW") < out.index("run_id")
    assert _TOKEN not in out


def test_cli_execute_behavior_unchanged_after_output_fix(
    engine: Engine, monkeypatch, capsys
) -> None:
    exit_code, out, factory = _run_execute(engine, monkeypatch, capsys)

    assert exit_code == m.EXIT_OK
    for line in (
        "status                  = succeeded",
        "provider                = make",
        "page_count              = 1",
        "response_count          = 1",
        "inserted_count          = 1",
        "updated_count           = 0",
        "unchanged_count         = 0",
    ):
        assert line in out, line
    with factory() as verify_session:
        assert _fact_count(verify_session) == 1


def test_cli_execute_preflight_reports_not_configured_as_fail_closed(
    engine: Engine, monkeypatch, capsys
) -> None:
    factory, pid = _isolated_cli(engine, monkeypatch)
    monkeypatch.setattr(m, "SessionLocal", factory)
    monkeypatch.setenv("MAKE_API_TOKEN", "")
    monkeypatch.setenv("MAKE_API_BASE_URL", "")
    get_settings.cache_clear()

    def _no_http(*_a, **_kw):
        raise AssertionError("must fail closed before any HTTP client is created")

    monkeypatch.setattr(httpx, "Client", _no_http)
    try:
        exit_code = m.main(["--affiliate-program-id", str(pid), *_DATE_ARGS, "--execute"])
    finally:
        get_settings.cache_clear()

    assert exit_code == m.EXIT_FAILED
    out = capsys.readouterr().out
    assert "NOT CONFIGURED" in out
    assert "Execution will fail closed before any HTTP request" in out
    assert "--execute would fail closed" not in out
    with factory() as verify_session:
        assert _fact_count(verify_session) == 0
