"""AffiliateCatalogHygieneService + apply_affiliate_catalog_hygiene CLI (独立した in-memory DB)。

PLAN は既定で write 0。EXECUTE は明示 (--execute) のときだけ、exact な before を確認して
all-or-nothing で適用し、冪等。production の catalog には触れない (in-memory DB だけを使う)。
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

import scripts.apply_affiliate_catalog_hygiene as cli
from app.affiliate.catalog_hygiene import STATUS_DRIFT, STATUS_NOT_FOUND, load_hygiene_config
from app.models import AffiliateProgram, Article, Keyword, KeywordScore, KeywordSignal
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.keyword_score_repository import KeywordScoreRepository
from app.services.affiliate_catalog_hygiene_service import (
    AffiliateCatalogHygieneService,
    HygieneRefusedError,
)
from app.services.content_queue_service import ContentQueueReadOnlyViolationError

_SPEC = load_hygiene_config()
_TRACKING = "https://track.example.invalid/secret-tracking-token"
_LANDING = "https://www.example.invalid/landing"


def _factory(session: Session):
    return lambda: contextlib.nullcontext(session)


def _seed(session: Session) -> dict[str, int]:
    """追跡 config の 11 program を、宣言どおりの現在値 (before) で投入する。"""

    repo = AffiliateProgramRepository(session)
    ids: dict[str, int] = {}
    for change in _SPEC.changes:
        ids[change.program] = repo.create(
            name=change.program,
            provider=change.provider,
            category="test-category",
            commission_type="percentage",
            commission_value=25.0,
            currency=None,
            match_terms=list(change.before),
            tracking_url=_TRACKING,
            landing_page_url=_LANDING,
            status=AffiliateProgramStatus.ACTIVE,
        ).id
    ids["Krisp"] = repo.create(
        name="Krisp",
        provider="Impact",
        match_terms=["Krisp", "AI 議事録", "文字起こし"],
        status=AffiliateProgramStatus.ACTIVE,
    ).id
    keyword = Keyword(keyword="AI 議事録")
    session.add(keyword)
    session.flush()
    KeywordScoreRepository(session).create(
        keyword_id=keyword.id,
        search_demand=1.0,
        commercial_intent=2.0,
        affiliate_opportunity=65.27,
        competition_ease=4.0,
        trend=5.0,
        originality=6.0,
        site_relevance=7.0,
        total_score=57.92,
        score_version="v1",
        input_source="signals",
    )
    session.commit()
    return ids


def _rows(session: Session) -> list[tuple]:
    session.expire_all()
    cols = [c.name for c in AffiliateProgram.__table__.columns]
    return [
        tuple(getattr(p, c) if c != "match_terms" else tuple(p.match_terms or ()) for c in cols)
        for p in session.scalars(select(AffiliateProgram).order_by(AffiliateProgram.id))
    ]


def _terms(session: Session) -> dict[str, tuple[str, ...]]:
    session.expire_all()
    return {p.name: tuple(p.match_terms or ()) for p in session.scalars(select(AffiliateProgram))}


def _other_counts(session: Session) -> dict[str, int]:
    return {
        m.__tablename__: session.scalar(select(func.count()).select_from(m))
        for m in (Keyword, KeywordScore, KeywordSignal, Article)
    }


@pytest.fixture
def seeded(session: Session) -> Session:
    _seed(session)
    return session


# ===================================================================== PLAN
def test_plan_is_the_default_and_performs_zero_writes(
    engine: Engine, seeded: Session, capsys
) -> None:
    before_rows = _rows(seeded)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(_c, _cur, statement, _p, _ctx, _many) -> None:
        statements.append(statement)

    commits: list[int] = []
    event.listen(seeded, "after_commit", lambda _s: commits.append(1))
    try:
        code = cli.run(session_factory=_factory(seeded))  # execute の指定なし = PLAN
    finally:
        event.remove(engine, "before_cursor_execute", _spy)

    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "PLAN (default: zero writes)" in out and "Nothing was written" in out
    assert "changes=11 pending=11 already_applied=0 blocked=0 terms_to_remove=16" in out
    assert statements and all(s.lstrip().split(None, 1)[0].upper() == "SELECT" for s in statements)
    assert commits == []
    assert not seeded.new and not seeded.dirty and not seeded.deleted
    assert _rows(seeded) == before_rows  # 全 column が不変


def test_plan_lists_exact_before_after_and_reasons_without_secrets(seeded: Session, capsys) -> None:
    assert cli.run(session_factory=_factory(seeded)) == 0
    out = capsys.readouterr().out
    assert (
        "[descript-loose-meeting-and-transcription-terms] Descript (provider=PartnerStack)" in out
    )
    assert "remove : ['議事録', '文字起こし', 'AI 文字起こし', '音声文字起こし']" in out
    assert "after  : ['Descript', '動画編集 AI', 'ポッドキャスト 編集']" in out
    assert "PENDING (would remove)" in out
    assert _TRACKING not in out and "example.invalid" not in out


def test_plan_json_output_is_valid_and_secret_free(seeded: Session, capsys) -> None:
    assert cli.run(output_format="json", session_factory=_factory(seeded)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "plan" and payload["applied"] == []
    assert payload["summary"] == {
        "changes": 11,
        "pending": 11,
        "already_applied": 0,
        "blocked": 0,
        "terms_to_remove": 16,
        "executable": True,
    }
    descript = next(c for c in payload["changes"] if c["program"] == "Descript")
    assert descript["status"] == "pending" and descript["current"] == descript["before"]
    assert descript["after"] == ["Descript", "動画編集 AI", "ポッドキャスト 編集"]
    assert _TRACKING not in json.dumps(payload)


def test_plan_guard_stops_any_write_attempted_during_planning(seeded: Session) -> None:
    class _Violating(AffiliateCatalogHygieneService):
        def _evaluate(self, change):
            self._session.add(Keyword(keyword="should never be written"))
            self._session.flush()
            return super()._evaluate(change)

    with pytest.raises(ContentQueueReadOnlyViolationError):
        _Violating(seeded).plan(_SPEC)
    seeded.rollback()
    assert (
        seeded.scalar(select(Keyword).where(Keyword.keyword == "should never be written")) is None
    )


def test_the_cli_defaults_to_plan_and_requires_an_explicit_execute_flag() -> None:
    assert cli._parse_args([]).execute is False
    assert cli._parse_args(["--format", "json"]).execute is False
    assert cli._parse_args(["--execute"]).execute is True
    assert cli.run.__kwdefaults__["execute"] is False


# ================================================================== EXECUTE
def test_execute_applies_only_match_terms_and_nothing_else(seeded: Session, capsys) -> None:
    before_rows = _rows(seeded)
    other_before = _other_counts(seeded)
    assert cli.run(execute=True, session_factory=_factory(seeded)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "EXECUTE" in out and "applied: 11 change(s)" in out

    terms = _terms(seeded)
    for change in _SPEC.changes:
        assert terms[change.program] == change.after, change.program
    assert terms["Krisp"] == ("Krisp", "AI 議事録", "文字起こし")  # 対象外の program は不変

    after_rows = _rows(seeded)
    cols = [c.name for c in AffiliateProgram.__table__.columns]
    for b, a in zip(before_rows, after_rows, strict=True):
        changed = {c for c, x, y in zip(cols, b, a, strict=True) if x != y}
        assert changed <= {"match_terms", "updated_at"}, (
            changed
        )  # URL / status / commission / name は不変
    assert _other_counts(seeded) == other_before  # score / signal / article は触らない
    session_scores = seeded.scalars(select(KeywordScore)).all()
    assert [s.total_score for s in session_scores] == [57.92]  # stored score は不変


def test_execute_preserves_urls_status_commission_and_identity(seeded: Session) -> None:
    ids = {p.name: p.id for p in seeded.scalars(select(AffiliateProgram))}
    cli.run(execute=True, session_factory=_factory(seeded))
    seeded.expire_all()
    for change in _SPEC.changes:
        p = seeded.get(AffiliateProgram, ids[change.program])
        assert (p.name, p.provider, p.status) == (change.program, change.provider, "active")
        assert (p.commission_type, p.commission_value) == ("percentage", 25.0)
        assert p.tracking_url == _TRACKING and p.landing_page_url == _LANDING


def test_execute_is_idempotent_and_the_second_run_does_not_write_or_commit(
    seeded: Session, capsys
) -> None:
    assert cli.run(execute=True, session_factory=_factory(seeded)) == 0
    capsys.readouterr()
    after_first = _rows(seeded)
    commits: list[int] = []
    event.listen(seeded, "after_commit", lambda _s: commits.append(1))
    assert cli.run(execute=True, session_factory=_factory(seeded)) == 0
    out = capsys.readouterr().out
    assert "applied: 0 change(s)" in out and "already_applied=11" in out
    assert commits == []
    assert _rows(seeded) == after_first
    # PLAN も適用済みを示す
    assert cli.run(session_factory=_factory(seeded)) == 0
    assert "ALREADY APPLIED (no-op)" in capsys.readouterr().out


def test_execute_refuses_on_any_drift_and_writes_nothing_all_or_nothing(
    seeded: Session, capsys
) -> None:
    repo = AffiliateProgramRepository(seeded)
    make = repo.get_by_name_and_provider("Make", "make")
    make.match_terms = [*make.match_terms, "手で足した語"]  # 1 件だけ drift
    seeded.commit()
    before_terms = _terms(seeded)

    code = cli.run(execute=True, session_factory=_factory(seeded))
    captured = capsys.readouterr()
    assert code == cli.EXIT_REFUSED
    assert "REFUSED (nothing was written)" in captured.err and "Make (drift)" in captured.err
    assert "DRIFT" in captured.out and "NOT executable" in captured.out
    assert _terms(seeded) == before_terms  # 10 件の正常な変更も適用されない


def test_plan_reports_drift_and_exits_nonzero_without_writing(seeded: Session, capsys) -> None:
    program = AffiliateProgramRepository(seeded).get_by_name_and_provider("Todoist", "direct")
    program.match_terms = ["Todoist"]
    seeded.commit()
    before_rows = _rows(seeded)
    assert cli.run(session_factory=_factory(seeded)) == cli.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "DRIFT (current terms differ from the declared before; NOT applied)" in out
    assert "current: ['Todoist']" in out
    assert _rows(seeded) == before_rows


@pytest.mark.parametrize(
    "mutate",
    [
        lambda terms: [*terms, "追加"],
        lambda terms: list(reversed(terms)),
        lambda terms: [t + " " if t == "業務効率化" else t for t in terms],
        lambda terms: [t for t in terms if t != "業務効率化"][:1],
    ],
)
def test_the_before_check_is_exact(seeded: Session, mutate) -> None:
    program = AffiliateProgramRepository(seeded).get_by_name_and_provider("HubSpot", "Impact")
    program.match_terms = mutate(list(program.match_terms))
    seeded.commit()
    plan = AffiliateCatalogHygieneService(seeded).plan(_SPEC)
    hub = next(o for o in plan.outcomes if o.change.program == "HubSpot")
    assert hub.status == STATUS_DRIFT  # 追加 / 順序 / 表記 / 欠落のどれも before とは一致しない
    assert not plan.executable
    with pytest.raises(HygieneRefusedError):
        AffiliateCatalogHygieneService(seeded).execute(_SPEC)


def test_program_not_found_blocks_execution(seeded: Session, capsys) -> None:
    program = AffiliateProgramRepository(seeded).get_by_name_and_provider(
        "Reclaim.ai", "PartnerStack"
    )
    seeded.delete(program)
    seeded.commit()
    before_terms = _terms(seeded)
    code = cli.run(execute=True, session_factory=_factory(seeded))
    assert code == cli.EXIT_REFUSED
    assert "Reclaim.ai (program_not_found)" in capsys.readouterr().err
    assert _terms(seeded) == before_terms
    plan = AffiliateCatalogHygieneService(seeded).plan(_SPEC)
    assert [o.status for o in plan.blocked] == [STATUS_NOT_FOUND]


def test_provider_is_part_of_the_program_identity(seeded: Session) -> None:
    other = AffiliateProgramRepository(seeded).create(
        name="Make", provider="someone-else", match_terms=["Make", "自動化"], status="active"
    )
    seeded.commit()
    cli.run(execute=True, session_factory=_factory(seeded))
    seeded.expire_all()
    assert tuple(seeded.get(AffiliateProgram, other.id).match_terms) == (
        "Make",
        "自動化",
    )  # 別 provider は不変
    assert _terms(seeded)["Krisp"] == ("Krisp", "AI 議事録", "文字起こし")


def test_execute_rechecks_before_writing_and_rolls_back_partial_changes(
    seeded: Session, monkeypatch
) -> None:
    """plan と書き込みの間に drift が起きても (古い plan を渡されても) 何も残さない。"""

    service = AffiliateCatalogHygieneService(seeded)
    stale_plan = service.plan(_SPEC)
    assert stale_plan.executable and stale_plan.pending == 11
    last = AffiliateProgramRepository(seeded).get_by_name_and_provider(
        "ActiveCampaign", "PartnerStack"
    )
    last.match_terms = [*last.match_terms, "後から足した語"]  # 最後に処理される program だけ drift
    seeded.commit()
    before_terms = _terms(seeded)
    monkeypatch.setattr(AffiliateCatalogHygieneService, "plan", lambda self, spec: stale_plan)
    with pytest.raises(HygieneRefusedError):
        service.execute(_SPEC)
    assert _terms(seeded) == before_terms  # 先に update した 10 件も rollback されている


def test_execute_rolls_back_everything_when_the_commit_fails(seeded: Session, monkeypatch) -> None:
    before_terms = _terms(seeded)

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(seeded, "commit", _boom)
    with pytest.raises(RuntimeError, match="commit failed"):
        AffiliateCatalogHygieneService(seeded).execute(_SPEC)
    monkeypatch.undo()
    assert _terms(seeded) == before_terms


def test_execute_reads_back_the_stored_value_after_commit(seeded: Session) -> None:
    result = AffiliateCatalogHygieneService(seeded).execute(_SPEC)
    assert len(result.applied) == 11 and result.skipped_already_applied == ()
    assert _terms(seeded)["Descript"] == ("Descript", "動画編集 AI", "ポッドキャスト 編集")


def test_partially_applied_state_is_completed_idempotently(seeded: Session) -> None:
    """一部だけ適用済み (現在値 == after) の program は skip し、残りだけを適用する。"""

    change = next(c for c in _SPEC.changes if c.program == "Writesonic")
    program = AffiliateProgramRepository(seeded).get_by_name_and_provider(
        "Writesonic", "FirstPromoter"
    )
    program.match_terms = list(change.after)
    seeded.commit()
    result = AffiliateCatalogHygieneService(seeded).execute(_SPEC)
    assert len(result.applied) == 10 and result.skipped_already_applied == (change.id,)
    assert _terms(seeded)["Writesonic"] == change.after


# ===================================================== config errors / safety
def test_a_bad_config_exits_2_and_never_opens_the_database(tmp_path: Path, capsys) -> None:
    def _no_session():
        raise AssertionError("the DB must not be opened")

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "version": 1,
                "changes": [
                    {
                        "id": "x",
                        "program": "Make",
                        "provider": "make",
                        "before": ["Make", "自動化"],
                        "remove": ["Make"],  # strong term
                        "reason": "r",
                        "evidence": "e",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert cli.run(config_path=bad, session_factory=_no_session) == cli.EXIT_CONFIG
    assert "cannot remove strong terms" in capsys.readouterr().err
    assert (
        cli.run(config_path=tmp_path / "missing.json", session_factory=_no_session)
        == cli.EXIT_CONFIG
    )


def test_the_service_makes_no_http_or_socket_calls(seeded: Session, monkeypatch) -> None:
    import socket

    import httpx

    def _boom(*_a, **_kw):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(httpx.Client, "send", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)
    assert cli.run(session_factory=_factory(seeded)) == 0
    assert cli.run(execute=True, session_factory=_factory(seeded)) == 0


def test_cli_output_is_encodable_on_a_cp932_windows_console(seeded: Session, capsys) -> None:
    """Windows console (cp932) で UnicodeEncodeError にならない (PLAN / EXECUTE / drift / JSON)。"""

    assert cli.run(session_factory=_factory(seeded)) == 0
    plan_out = capsys.readouterr().out
    assert cli.run(output_format="json", session_factory=_factory(seeded)) == 0
    json_out = capsys.readouterr().out
    program = AffiliateProgramRepository(seeded).get_by_name_and_provider("Todoist", "direct")
    program.match_terms = ["Todoist"]
    seeded.commit()
    assert cli.run(session_factory=_factory(seeded)) == cli.EXIT_REFUSED
    drift_out = capsys.readouterr().out
    for text in (plan_out, json_out, drift_out):
        assert text  # 日本語の term は cp932 で表現できる。em dash 等は使わない
        text.encode("cp932")
