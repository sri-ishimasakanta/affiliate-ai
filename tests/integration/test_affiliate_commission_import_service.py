"""AffiliateCommissionImportService — PLAN / EXECUTE / pagination / idempotency /
status evolution / rollback。実ネットワークなし (httpx.MockTransport)。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.exceptions import AffiliateCommissionImportError, EntityNotFoundError
from app.models import (
    AffiliateCommissionFact,
    AffiliateCommissionImportRun,
    AffiliateOutboundClick,
)
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.services.affiliate_commission_import_service import (
    AffiliateCommissionImportService,
)

_BASE = "https://api.make.test"
_DF = date(2026, 9, 1)
_DT = date(2026, 9, 30)
_TOKEN = "synthetic-make-api-token-not-real"


def _settings(**over) -> Settings:
    base = {"make_api_base_url": _BASE, "make_api_token": _TOKEN}
    base.update(over)
    return Settings(**base)


def _program(session: Session, *, name: str = "Make", provider: str = "make") -> int:
    p = AffiliateProgramRepository(session).create(
        name=name, provider=provider, status=AffiliateProgramStatus.ACTIVE
    )
    session.commit()
    return p.id


def _row(rid, **over) -> dict:
    base = {
        "id": rid,
        "organization_id": 5,
        "type": "commission",
        "status": "requested",
        "commission": 10.0,
        "source": "referral",
        "created": "2026-09-01T00:00:00+00:00",
        "payout_requested": None,
        "payout_approved": None,
        "payout_realized": None,
    }
    base.update(over)
    return base


def _transport(pages: list[list[dict]], *, calls: list | None = None) -> httpx.MockTransport:
    """順に ``pages`` を返す (呼び出しごとに 1 ページ消費)。公式契約の
    ``{"commissions": [...]}`` object envelope で返す (Phase E1.2)。"""

    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        idx = state["i"]
        state["i"] += 1
        page = pages[idx] if idx < len(pages) else []
        return httpx.Response(200, json={"commissions": page})

    return httpx.MockTransport(handler)


def _no_http() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request expected")

    return httpx.MockTransport(handler)


def _run_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateCommissionImportRun))


def _fact_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateCommissionFact))


def _click_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateOutboundClick))


# ==================== PLAN ==========================================
def test_plan_zero_http_zero_db_write(session: Session, monkeypatch) -> None:
    pid = _program(session)

    def _raise(*_a, **_kw):
        raise AssertionError("must not be called during PLAN")

    monkeypatch.setattr(session, "commit", _raise)
    monkeypatch.setattr(session, "flush", _raise)

    svc = AffiliateCommissionImportService(session)
    result = svc.plan(affiliate_program_id=pid, settings=_settings())
    assert result.configured is True
    assert result.provider == "make"
    assert result.would_execute is False
    assert _run_count(session) == 0


def test_plan_missing_program_raises(session: Session) -> None:
    svc = AffiliateCommissionImportService(session)
    with pytest.raises(EntityNotFoundError):
        svc.plan(affiliate_program_id=999999, settings=_settings())


def test_plan_date_range_validation(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    with pytest.raises(AffiliateCommissionImportError, match="date_from"):
        svc.plan(
            affiliate_program_id=pid,
            date_from=date(2026, 9, 30),
            date_to=date(2026, 9, 1),
            settings=_settings(),
        )


def test_plan_unconfigured_reports_false(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    result = svc.plan(
        affiliate_program_id=pid,
        settings=_settings(make_api_base_url=None, make_api_token=None),
    )
    assert result.configured is False


# ==================== EXECUTE: config / validation gates ================
def test_execute_missing_token_fails_safely_no_run_created(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    with pytest.raises(AffiliateCommissionImportError, match="not configured"):
        svc.import_commissions(
            date_from=_DF,
            date_to=_DT,
            affiliate_program_id=pid,
            settings=_settings(make_api_base_url=None, make_api_token=None),
            transport=_no_http(),
        )
    assert _run_count(session) == 0
    assert _fact_count(session) == 0


def test_execute_date_range_validation_no_run_created(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    with pytest.raises(AffiliateCommissionImportError, match="date_from"):
        svc.import_commissions(
            affiliate_program_id=pid,
            date_from=date(2026, 9, 30),
            date_to=date(2026, 9, 1),
            settings=_settings(),
            transport=_no_http(),
        )
    assert _run_count(session) == 0


def test_execute_missing_program_raises(session: Session) -> None:
    svc = AffiliateCommissionImportService(session)
    with pytest.raises(EntityNotFoundError):
        svc.import_commissions(
            date_from=_DF,
            date_to=_DT,
            affiliate_program_id=999999,
            settings=_settings(),
            transport=_no_http(),
        )
    assert _run_count(session) == 0


# ==================== EXECUTE: fresh one-page import =====================
def test_execute_one_page_import(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    transport = _transport([[_row(1), _row(2)]])

    run = svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=transport,
    )

    assert run.status == "succeeded"
    assert run.page_count == 1
    assert run.response_count == 2
    assert run.inserted_count == 2
    assert run.updated_count == 0
    assert run.unchanged_count == 0
    assert _fact_count(session) == 2

    fact = (
        session.execute(
            select(AffiliateCommissionFact).where(
                AffiliateCommissionFact.source_commission_id == "1"
            )
        )
        .scalars()
        .first()
    )
    assert fact.affiliate_program_id == pid
    assert fact.provider == "make"
    assert fact.provider_status == "requested"
    assert isinstance(fact.commission_amount, Decimal)
    assert fact.commission_amount == Decimal("10.0")
    assert str(fact.commission_amount).rstrip("0").rstrip(".") == "10"
    assert fact.currency is None  # 通貨は捏造しない
    assert fact.source_import_run_id == run.id
    assert fact.first_seen_at == fact.last_seen_at


# ==================== EXECUTE: complete pagination ========================
def test_execute_pagination_fetches_all_pages(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    calls: list = []
    # page_limit=2: page1 full (2 rows) -> has_more True; page2 partial (1 row) -> done.
    transport = _transport([[_row(1), _row(2)], [_row(3)]], calls=calls)

    run = svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=2,
        settings=_settings(),
        transport=transport,
    )

    assert run.page_count == 2
    assert run.response_count == 3
    assert run.inserted_count == 3
    assert len(calls) == 2
    assert dict(calls[0].url.params)["pg[offset]"] == "0"
    assert dict(calls[1].url.params)["pg[offset]"] == "2"
    assert _fact_count(session) == 3


def test_execute_max_page_safety_limit(session: Session, monkeypatch) -> None:
    import app.services.affiliate_commission_import_service as svc_mod

    monkeypatch.setattr(svc_mod, "MAKE_COMMISSIONS_MAX_PAGES", 2)
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    # every page full -> always has_more -> would loop forever without the cap.
    transport = _transport([[_row(1)], [_row(2)], [_row(3)], [_row(4)]])

    with pytest.raises(AffiliateCommissionImportError, match="max page"):
        svc.import_commissions(
            date_from=_DF,
            date_to=_DT,
            affiliate_program_id=pid,
            page_limit=1,
            settings=_settings(),
            transport=transport,
        )
    run = session.execute(select(AffiliateCommissionImportRun)).scalars().first()
    assert run.status == "failed"
    assert _fact_count(session) == 0  # 全ページ取り込みが完了するまで DB へは一切書かない


def test_duplicate_row_within_same_run_across_pages_handled_safely(
    session: Session,
) -> None:
    """offset ページネーション中に同じ source_commission_id が 2 ページに
    跨って重複出現しても (Make 側データの増減による offset drift)、二重 insert
    で DB 一意性制約に落ちず、2 回目は正しく update/unchanged として扱われる。"""

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    # page1 (limit=1, full -> has_more) が id=1、page2 が再び id=1 (drift)。
    transport = _transport([[_row(1)], [_row(1)]])

    run = svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=1,
        settings=_settings(),
        transport=transport,
    )

    assert run.status == "succeeded"
    assert run.inserted_count == 1
    assert run.unchanged_count == 1
    assert _fact_count(session) == 1


# ==================== replay idempotency / status evolution =============
def test_replay_same_rows_is_idempotent_no_duplicates(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)

    svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1)]]),
    )
    second = svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1)]]),
    )

    assert second.inserted_count == 0
    assert second.unchanged_count == 1
    assert second.updated_count == 0
    assert _fact_count(session) == 1


def test_status_evolution_updates_existing_row(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)

    svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1, status="requested", payout_requested=None)]]),
    )
    second = svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport(
            [[_row(1, status="approved", payout_requested="2026-09-05T00:00:00+00:00")]]
        ),
    )

    assert second.updated_count == 1
    assert second.inserted_count == 0
    assert _fact_count(session) == 1
    fact = session.execute(select(AffiliateCommissionFact)).scalars().first()
    assert fact.provider_status == "approved"
    assert fact.payout_requested_at is not None
    assert fact.last_seen_at is not None


def test_source_drift_on_identity_field_fails_closed(session: Session) -> None:
    """同じ source_commission_id で organization_id/type/created が変わるのは
    矛盾した historical state -- fail closed し、既存行は変更しない。"""

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)

    svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1, organization_id=5)]]),
    )
    with pytest.raises(AffiliateCommissionImportError, match="drift"):
        svc.import_commissions(
            date_from=_DF,
            date_to=_DT,
            affiliate_program_id=pid,
            page_limit=10,
            settings=_settings(),
            transport=_transport([[_row(1, organization_id=999)]]),
        )
    fact = session.execute(select(AffiliateCommissionFact)).scalars().first()
    assert fact.source_organization_id == "5"  # 変更されない


# ==================== nullable fields =====================================
def test_nullable_commission_amount_persisted(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1, commission=None)]]),
    )
    fact = session.execute(select(AffiliateCommissionFact)).scalars().first()
    assert fact.commission_amount is None


def test_commission_amount_exact_decimal_through_full_persistence(
    session: Session,
) -> None:
    """Phase E1.1: JSON -> client (parse_float=Decimal) -> validator -> service
    -> Numeric(14,4) column -> 読み戻し、のフルパスで厳密な Decimal が保たれる
    ことを証明する。0.1 + 0.2 は binary float では 0.3 に厳密には一致しない。"""

    assert 0.1 + 0.2 != 0.3

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1, commission=0.1), _row(2, commission=0.2)]]),
    )

    facts = session.execute(select(AffiliateCommissionFact)).scalars().all()
    total = sum((f.commission_amount for f in facts), Decimal("0"))
    assert total == Decimal("0.3")
    for f in facts:
        assert isinstance(f.commission_amount, Decimal)


def test_commission_amount_beyond_four_decimal_places_not_rounded_end_to_end(
    session: Session,
) -> None:
    """Phase E1.2: JSON -> client -> validator -> service -> DB -> 読み戻しの
    フルパスで、4 桁を超える小数が丸められずに exact のまま保持される。
    (SQLite でも float を経由しない -- ExactDecimal)"""

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1, commission=12.123456), _row(2, commission=0.000012345678)]]),
    )
    session.expire_all()
    by_id = {
        f.source_commission_id: f.commission_amount
        for f in session.execute(select(AffiliateCommissionFact)).scalars().all()
    }
    assert by_id["1"] == Decimal("12.123456")
    assert by_id["2"] == Decimal("0.000012345678")


def test_commission_amount_over_supported_scale_fails_run_no_facts(
    session: Session,
) -> None:
    """DB scale (18 桁小数) を超える値は黙って切り詰めず、run ごと fail closed。"""

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            '{"commissions": [{"id": 1, "organization_id": 1, "type": "commission", '
            '"status": "requested", "commission": 1.1234567890123456789, '
            '"source": null, "created": "2026-09-01T00:00:00+00:00", '
            '"payout_requested": null, "payout_approved": null, "payout_realized": null}]}'
        )
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    with pytest.raises(AffiliateCommissionImportError, match="decimal places"):
        svc.import_commissions(
            date_from=_DF,
            date_to=_DT,
            affiliate_program_id=pid,
            page_limit=10,
            settings=_settings(),
            transport=httpx.MockTransport(handler),
        )
    run = session.execute(select(AffiliateCommissionImportRun)).scalars().first()
    assert run.status == "failed"
    assert _fact_count(session) == 0


def test_object_envelope_pagination_final_short_page_terminates(
    session: Session,
) -> None:
    """object envelope の 3 ページ (2, 2, 1 件) -- 最後の short page で終了する。"""

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    calls: list = []
    run = svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=2,
        settings=_settings(),
        transport=_transport([[_row(1), _row(2)], [_row(3), _row(4)], [_row(5)]], calls=calls),
    )
    assert run.page_count == 3
    assert len(calls) == 3
    assert [dict(c.url.params)["pg[offset]"] for c in calls] == ["0", "2", "4"]
    assert run.inserted_count == 5


def test_nullable_source_persisted(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1, source=None)]]),
    )
    fact = session.execute(select(AffiliateCommissionFact)).scalars().first()
    assert fact.source is None


def test_payout_timestamps_persisted(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport(
            [
                [
                    _row(
                        1,
                        payout_requested="2026-09-01T00:00:00+00:00",
                        payout_approved="2026-09-05T00:00:00+00:00",
                        payout_realized="2026-09-10T00:00:00+00:00",
                    )
                ]
            ]
        ),
    )
    fact = session.execute(select(AffiliateCommissionFact)).scalars().first()
    assert fact.payout_requested_at is not None
    assert fact.payout_approved_at is not None
    assert fact.payout_realized_at is not None


def test_no_currency_fabrication(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1)]]),
    )
    fact = session.execute(select(AffiliateCommissionFact)).scalars().first()
    assert fact.currency is None


def test_no_local_click_conversion_false_join(session: Session) -> None:
    """commission import は AffiliateOutboundClick を一切参照/作成しない。"""

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1)]]),
    )
    assert _click_count(session) == 0
    assert not hasattr(AffiliateCommissionFact, "affiliate_outbound_click_id")


# ==================== malformed response / HTTP error / rollback ========
def test_malformed_response_shape_fails_run_no_facts(session: Session) -> None:
    """missing "commissions" key -- 公式契約の object envelope に反する。"""

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "the commissions key"})

    with pytest.raises(AffiliateCommissionImportError):
        svc.import_commissions(
            date_from=_DF,
            date_to=_DT,
            affiliate_program_id=pid,
            page_limit=10,
            settings=_settings(),
            transport=httpx.MockTransport(handler),
        )
    run = session.execute(select(AffiliateCommissionImportRun)).scalars().first()
    assert run.status == "failed"
    assert run.error_message is not None
    assert _fact_count(session) == 0


def test_bare_array_response_fails_run_no_facts(session: Session) -> None:
    """Phase E1.2: bare array は公式契約ではない -- object envelope が必須。"""

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_row(1), _row(2)])

    with pytest.raises(AffiliateCommissionImportError, match="object"):
        svc.import_commissions(
            date_from=_DF,
            date_to=_DT,
            affiliate_program_id=pid,
            page_limit=10,
            settings=_settings(),
            transport=httpx.MockTransport(handler),
        )
    run = session.execute(select(AffiliateCommissionImportRun)).scalars().first()
    assert run.status == "failed"
    assert _fact_count(session) == 0


def test_http_error_fails_run_no_facts(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(AffiliateCommissionImportError):
        svc.import_commissions(
            date_from=_DF,
            date_to=_DT,
            affiliate_program_id=pid,
            page_limit=10,
            settings=_settings(),
            transport=httpx.MockTransport(handler),
        )
    run = session.execute(select(AffiliateCommissionImportRun)).scalars().first()
    assert run.status == "failed"
    assert run.http_status == 500
    assert _fact_count(session) == 0


def test_partial_page_failure_rolls_back_whole_import(session: Session) -> None:
    """2 行目が malformed なら、1 行目も一切書き込まない (部分取り込みをしない)。"""

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)

    def handler(request: httpx.Request) -> httpx.Response:
        # 2 は type/status 欠落。
        return httpx.Response(200, json={"commissions": [_row(1), {"id": 2}]})

    with pytest.raises(AffiliateCommissionImportError):
        svc.import_commissions(
            date_from=_DF,
            date_to=_DT,
            affiliate_program_id=pid,
            page_limit=10,
            settings=_settings(),
            transport=httpx.MockTransport(handler),
        )
    assert _fact_count(session) == 0
    run = session.execute(select(AffiliateCommissionImportRun)).scalars().first()
    assert run.status == "failed"


# ==================== provider source id uniqueness (DB-level) ==========
def test_unique_constraint_prevents_duplicate_provider_source_id(session: Session) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.repositories.affiliate_commission_fact_repository import (
        AffiliateCommissionFactRepository,
    )

    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    run = svc.import_commissions(
        date_from=_DF,
        date_to=_DT,
        affiliate_program_id=pid,
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1)]]),
    )
    existing = session.execute(select(AffiliateCommissionFact)).scalars().first()

    repo = AffiliateCommissionFactRepository(session)
    with pytest.raises(IntegrityError):
        repo.add(
            affiliate_program_id=pid,
            provider="make",
            source_commission_id="1",  # 同じ (provider, source_commission_id)
            source_organization_id=None,
            event_type="commission",
            provider_status="requested",
            commission_amount=None,
            currency=None,
            source=None,
            occurred_at=existing.occurred_at,
            payout_requested_at=None,
            payout_approved_at=None,
            payout_realized_at=None,
            first_seen_at=existing.occurred_at,
            last_seen_at=existing.occurred_at,
            source_import_run_id=run.id,
        )
    session.rollback()
    assert _fact_count(session) == 1


# ==================== Phase E1.4: dateFrom/dateTo are mandatory ===========
@pytest.mark.parametrize(
    ("date_from", "date_to"),
    [(None, None), (_DF, None), (None, _DT)],
    ids=["neither", "from-only", "to-only"],
)
def test_execute_missing_or_partial_dates_fail_before_http_and_run(
    session: Session, date_from, date_to
) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    with pytest.raises(AffiliateCommissionImportError, match="both required"):
        svc.import_commissions(
            affiliate_program_id=pid,
            date_from=date_from,
            date_to=date_to,
            settings=_settings(),
            transport=_no_http(),  # 1 回でも HTTP が送られれば AssertionError
        )
    assert _run_count(session) == 0
    assert _fact_count(session) == 0


def test_execute_date_check_happens_before_program_lookup(session: Session) -> None:
    """日付検証は DB 参照 (program 存在確認) よりも先 -- 存在しない program でも
    先に日付エラーになる (fail fast)。"""

    svc = AffiliateCommissionImportService(session)
    with pytest.raises(AffiliateCommissionImportError, match="both required"):
        svc.import_commissions(
            affiliate_program_id=999999,
            settings=_settings(),
            transport=_no_http(),
        )


def test_execute_never_invents_a_default_date_range(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    calls: list = []
    svc.import_commissions(
        affiliate_program_id=pid,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        page_limit=10,
        settings=_settings(),
        transport=_transport([[_row(1)]], calls=calls),
    )
    params = dict(calls[0].url.params)
    assert params["dateFrom"] == "2026-01-01"
    assert params["dateTo"] == "2026-01-31"
    run = session.execute(select(AffiliateCommissionImportRun)).scalars().first()
    assert run.requested_date_from == date(2026, 1, 1)
    assert run.requested_date_to == date(2026, 1, 31)


def test_execute_sends_both_dates_on_every_page(session: Session) -> None:
    pid = _program(session)
    svc = AffiliateCommissionImportService(session)
    calls: list = []
    svc.import_commissions(
        affiliate_program_id=pid,
        date_from=_DF,
        date_to=_DT,
        page_limit=2,
        settings=_settings(),
        transport=_transport([[_row(1), _row(2)], [_row(3)]], calls=calls),
    )
    assert len(calls) == 2
    for c in calls:
        p = dict(c.url.params)
        assert p["dateFrom"] == "2026-09-01"
        assert p["dateTo"] == "2026-09-30"
        # pg[returnTotalCount] は別途 live 検証するまで有効化しない。
        assert set(p) == {"dateFrom", "dateTo", "pg[offset]", "pg[limit]"}


# ==================== Phase E1.4: PLAN stays zero-HTTP =====================
def test_plan_without_dates_allowed_reports_incomplete_and_sends_no_http(
    session: Session, monkeypatch
) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("PLAN must not create an HTTP client")

    monkeypatch.setattr(httpx, "Client", _boom)
    pid = _program(session)
    result = AffiliateCommissionImportService(session).plan(
        affiliate_program_id=pid, settings=_settings()
    )
    assert result.dates_complete is False
    assert result.date_from is None and result.date_to is None  # 補完しない
    assert result.would_execute is False
    assert _run_count(session) == 0


def test_plan_with_both_dates_reports_complete(session: Session) -> None:
    pid = _program(session)
    result = AffiliateCommissionImportService(session).plan(
        affiliate_program_id=pid, date_from=_DF, date_to=_DT, settings=_settings()
    )
    assert result.dates_complete is True


@pytest.mark.parametrize(("date_from", "date_to"), [(_DF, None), (None, _DT)])
def test_plan_with_one_date_only_is_rejected(session: Session, date_from, date_to) -> None:
    pid = _program(session)
    with pytest.raises(AffiliateCommissionImportError, match="together"):
        AffiliateCommissionImportService(session).plan(
            affiliate_program_id=pid,
            date_from=date_from,
            date_to=date_to,
            settings=_settings(),
        )


# ==================== Phase E1.4: "commissions": null / pagination =========
def _null_transport(*, calls: list | None = None) -> httpx.MockTransport:
    live_empty = {
        "commissions": None,
        "pg": {
            "limit": 2,
            "offset": 0,
            "returnTotalCount": False,
            "sortBy": "id",
            "sortDir": "asc",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, json=live_empty)

    return httpx.MockTransport(handler)


def test_null_commissions_first_page_is_empty_success_and_stops(session: Session) -> None:
    pid = _program(session)
    calls: list = []
    run = AffiliateCommissionImportService(session).import_commissions(
        affiliate_program_id=pid,
        date_from=_DF,
        date_to=_DT,
        page_limit=2,
        settings=_settings(),
        transport=_null_transport(calls=calls),
    )
    assert run.status == "succeeded"
    assert run.page_count == 1
    assert run.response_count == 0
    assert run.inserted_count == 0
    assert len(calls) == 1  # null で即終了 -- 追加ページを取りに行かない
    assert _fact_count(session) == 0


def test_empty_array_first_page_stops_pagination(session: Session) -> None:
    pid = _program(session)
    calls: list = []
    run = AffiliateCommissionImportService(session).import_commissions(
        affiliate_program_id=pid,
        date_from=_DF,
        date_to=_DT,
        page_limit=2,
        settings=_settings(),
        transport=_transport([[]], calls=calls),
    )
    assert run.page_count == 1
    assert run.response_count == 0
    assert len(calls) == 1


def test_null_page_after_full_page_terminates(session: Session) -> None:
    """全件 (limit と同数) の直後の空ページが null でも終了する。"""

    pid = _program(session)
    calls: list = []
    run = AffiliateCommissionImportService(session).import_commissions(
        affiliate_program_id=pid,
        date_from=_DF,
        date_to=_DT,
        page_limit=2,
        settings=_settings(),
        transport=_transport([[_row(1), _row(2)], None], calls=calls),
    )
    assert run.page_count == 2
    assert run.inserted_count == 2
    assert [dict(c.url.params)["pg[offset]"] for c in calls] == ["0", "2"]


def test_pg_metadata_does_not_drive_pagination(session: Session) -> None:
    """pg が returnTotalCount=True / 巨大な limit を示していても、終端判定は
    受領件数 < limit のまま (pg は echo/metadata で使わない)。"""

    pid = _program(session)
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "commissions": [_row(1)],
                "pg": {"limit": 9999, "offset": 0, "returnTotalCount": True, "total": 500},
            },
        )

    run = AffiliateCommissionImportService(session).import_commissions(
        affiliate_program_id=pid,
        date_from=_DF,
        date_to=_DT,
        page_limit=2,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    assert run.page_count == 1  # 1 件 < limit(2) -> 終了 (total=500 は無視)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body",
    [{"pg": {}}, {"commissions": "x"}, {"commissions": {}}, {"commissions": 5}, [1, 2]],
    ids=["missing-key", "string", "object", "number", "bare-array"],
)
def test_malformed_commissions_shapes_still_fail_run_and_write_nothing(
    session: Session, body
) -> None:
    pid = _program(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(AffiliateCommissionImportError):
        AffiliateCommissionImportService(session).import_commissions(
            affiliate_program_id=pid,
            date_from=_DF,
            date_to=_DT,
            page_limit=2,
            settings=_settings(),
            transport=httpx.MockTransport(handler),
        )
    run = session.execute(select(AffiliateCommissionImportRun)).scalars().first()
    assert run.status == "failed"
    assert _fact_count(session) == 0
