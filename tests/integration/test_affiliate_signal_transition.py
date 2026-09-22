"""C2.5.10: affiliate_opportunity の再導出 / 再スコア transition (PLAN / EXECUTE) の検証。

PLAN は write 0 / commit 0、EXECUTE は「pool 全件の導出」+「値が変わった既存 score だけの
再スコア」。対象は DB から決まる (ID を直書きしない)。scratch / in-memory DB のみを使う。
"""

import contextlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

import scripts.rederive_affiliate_opportunity as cli
from app.keyword.scoring import COMPONENT_NAMES
from app.models import Keyword, KeywordScore, KeywordScoreSignal, KeywordSignal
from app.models.enums import AffiliateProgramStatus, KeywordStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.affiliate_signal_transition_service import (
    DECISION_BLOCKED_INCOMPLETE,
    DECISION_NEVER_SCORED,
    DECISION_RESCORE,
    DECISION_UNCHANGED,
    AffiliateSignalTransitionService,
    TransitionRefusedError,
)
from app.services.keyword_scoring_service import KeywordScoringService

NOW = datetime(2026, 9, 22, tzinfo=UTC)
#: 実際の照合結果とわざとずらした affiliate_opportunity (stale)
STALE = 11.0
OTHER_VALUE = 40.0


# ---------------------------------------------------------------- fixtures
def _catalog(session: Session) -> None:
    repo = AffiliateProgramRepository(session)
    repo.create(
        name="HubSpot", provider="Impact", commission_type="percentage",
        commission_value=30.0, match_terms=["HubSpot", "CRM"],
        status=AffiliateProgramStatus.ACTIVE,
    )
    repo.create(
        name="Pipedrive", provider="PartnerStack", commission_type="percentage",
        commission_value=20.0, match_terms=["Pipedrive", "CRM"],
        status=AffiliateProgramStatus.ACTIVE,
    )
    session.commit()


def _keyword(session: Session, text: str) -> Keyword:
    entity = Keyword(keyword=text)
    entity.status = KeywordStatus.ANALYZED
    session.add(entity)
    session.commit()
    return entity


def _signals(session: Session, keyword_id: int, *, affiliate: float) -> None:
    repo = KeywordSignalRepository(session)
    for component in COMPONENT_NAMES:
        repo.create(
            keyword_id=keyword_id,
            component=component,
            normalized_value=affiliate if component == "affiliate_opportunity" else OTHER_VALUE,
            provider="test",
            observed_at=NOW,
            raw_data={},
            source_reference="test",
        )
    session.commit()


def _score(session: Session, keyword_id: int) -> None:
    KeywordScoringService(session).score_keyword_from_latest_signals(keyword_id)


def _counts(session: Session) -> tuple[int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(KeywordSignal)),
        session.scalar(select(func.count()).select_from(KeywordScore)),
        session.scalar(select(func.count()).select_from(KeywordScoreSignal)),
    )


@pytest.fixture
def pool(session: Session) -> dict[str, int]:
    """4 通りの keyword を用意する。

    changed  : score があり、再導出すると affiliate 値が変わる (stale を保存してある)
    unchanged: score があり、再導出しても値が変わらない (catalog に match しない = 0.0)
    unscored : signal はあるが score が無い
    blocked  : score はあるが、あとから competition_ease の signal を消した (付け直せない)
    """

    _catalog(session)
    ids = {}
    for name, text, affiliate in (
        ("changed", "crm おすすめ", STALE),
        ("unchanged", "みかん 品種", 0.0),
        ("unscored", "crm 比較", STALE),
        ("blocked", "crm 導入", STALE),
    ):
        keyword = _keyword(session, text)
        ids[name] = keyword.id
        _signals(session, keyword.id, affiliate=affiliate)
    for name in ("changed", "unchanged", "blocked"):
        _score(session, ids[name])
    # blocked: score を作ったあとに必須 component を 1 つ落とす
    stale_signal = KeywordSignalRepository(session).get_latest(
        ids["blocked"], "competition_ease"
    )
    session.delete(stale_signal)
    session.commit()
    return ids


# ================================================================ PLAN
def test_plan_performs_no_writes_and_no_commits(session: Session, pool: dict) -> None:
    statements: list[str] = []
    commits: list[int] = []

    def _record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    def _commit(_conn):
        commits.append(1)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    event.listen(engine, "commit", _commit)
    before = _counts(session)
    try:
        plan = AffiliateSignalTransitionService(session).plan()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        event.remove(engine, "commit", _commit)

    assert plan.keyword_count == 4
    assert _counts(session) == before
    non_select = [s for s in statements if s.lstrip().split(None, 1)[0].upper() != "SELECT"]
    assert non_select == []
    assert commits == []


def test_plan_classifies_each_keyword_from_the_database(session: Session, pool: dict) -> None:
    plan = AffiliateSignalTransitionService(session).plan()
    by_id = {o.keyword_id: o for o in plan.outcomes}

    changed = by_id[pool["changed"]]
    assert changed.decision == DECISION_RESCORE
    assert changed.current_score_value == STALE
    assert changed.proposed_value != STALE and changed.proposed_value > 0
    assert changed.current_total is not None and changed.proposed_total is not None
    assert changed.proposed_total != changed.current_total

    assert by_id[pool["unchanged"]].decision == DECISION_UNCHANGED
    assert by_id[pool["unchanged"]].proposed_value == 0.0
    assert by_id[pool["unscored"]].decision == DECISION_NEVER_SCORED
    assert by_id[pool["blocked"]].decision == DECISION_BLOCKED_INCOMPLETE
    assert by_id[pool["blocked"]].missing_components == ("competition_ease",)

    assert plan.rescore_ids == (pool["changed"],)
    assert plan.expected_signal_inserts == 4
    assert plan.expected_score_inserts == 1
    assert plan.expected_score_signal_inserts == 7
    assert plan.has_work is True


def test_plan_reports_expected_rank_changes(session: Session, pool: dict) -> None:
    plan = AffiliateSignalTransitionService(session).plan()
    ranking = plan.ranking()
    # score を持つ 3 件だけが順位の対象
    assert {row[0] for row in ranking} == {pool["changed"], pool["unchanged"], pool["blocked"]}
    assert all(isinstance(row[2], int) and isinstance(row[3], int) for row in ranking)


# ================================================================ EXECUTE
def test_execute_derives_once_per_keyword_and_rescores_only_changed(
    session: Session, pool: dict
) -> None:
    svc = AffiliateSignalTransitionService(session)
    before_signals, before_scores, before_links = _counts(session)
    max_signal_id_before = session.scalar(select(func.max(KeywordSignal.id)))
    statuses_before = {
        k.id: k.status for k in session.scalars(select(Keyword)).all()
    }

    result = svc.execute(allow_blocked=True)

    assert result.already_current is False
    assert result.derived_keyword_ids == tuple(sorted(pool.values()))
    assert result.rescored_keyword_ids == (pool["changed"],)
    signals, scores, links = _counts(session)
    assert signals - before_signals == 4  # pool 全件にちょうど 1 行ずつ
    assert scores - before_scores == 1
    assert links - before_links == len(COMPONENT_NAMES)
    assert result.inserted_signals == 4
    assert result.inserted_scores == 1
    assert result.inserted_score_signals == 7

    # 追記された signal は全て affiliate_opportunity (件数ではなく max(id) を境界にする)
    new_signals = session.scalars(
        select(KeywordSignal).where(KeywordSignal.id > max_signal_id_before)
    ).all()
    assert {str(s.component) for s in new_signals} == {"affiliate_opportunity"}
    assert sorted(s.keyword_id for s in new_signals) == sorted(pool.values())

    # keyword status は変わらない
    assert {k.id: k.status for k in session.scalars(select(Keyword)).all()} == statuses_before


def test_execute_leaves_non_affiliate_signals_untouched(session: Session, pool: dict) -> None:
    others = {
        (s.keyword_id, s.component, s.normalized_value, s.id)
        for s in session.scalars(
            select(KeywordSignal).where(KeywordSignal.component != "affiliate_opportunity")
        ).all()
    }

    AffiliateSignalTransitionService(session).execute(allow_blocked=True)

    after = {
        (s.keyword_id, s.component, s.normalized_value, s.id)
        for s in session.scalars(
            select(KeywordSignal).where(KeywordSignal.component != "affiliate_opportunity")
        ).all()
    }
    assert after == others


def test_execute_does_not_score_a_previously_unscored_keyword(
    session: Session, pool: dict
) -> None:
    AffiliateSignalTransitionService(session).execute(allow_blocked=True)
    scored = {s.keyword_id for s in session.scalars(select(KeywordScore)).all()}
    assert pool["unscored"] not in scored


def test_execute_updates_only_the_rescored_keyword_cache(session: Session, pool: dict) -> None:
    before = {k.id: k.opportunity_score for k in session.scalars(select(Keyword)).all()}
    AffiliateSignalTransitionService(session).execute(allow_blocked=True)
    after = {k.id: k.opportunity_score for k in session.scalars(select(Keyword)).all()}
    assert [kid for kid in before if before[kid] != after[kid]] == [pool["changed"]]


# ================================================================ guards
def test_execute_refuses_an_unexpected_pool_size_without_writing(
    session: Session, pool: dict
) -> None:
    svc = AffiliateSignalTransitionService(session)
    before = _counts(session)
    with pytest.raises(TransitionRefusedError, match="keyword pool is 4, expected 30"):
        svc.execute(expect_keywords=30, allow_blocked=True)
    assert _counts(session) == before


def test_execute_refuses_an_unexpected_rescore_count_without_writing(
    session: Session, pool: dict
) -> None:
    svc = AffiliateSignalTransitionService(session)
    before = _counts(session)
    with pytest.raises(TransitionRefusedError, match="re-score set is 1 keyword"):
        svc.execute(expect_rescores=8, allow_blocked=True)
    assert _counts(session) == before


def test_execute_refuses_a_stale_score_it_cannot_refresh(session: Session, pool: dict) -> None:
    svc = AffiliateSignalTransitionService(session)
    before = _counts(session)
    with pytest.raises(TransitionRefusedError, match="missing required signals"):
        svc.execute()  # allow_blocked なし
    assert _counts(session) == before


def test_execute_rolls_back_and_scores_nothing_when_derivation_fails(
    session: Session, pool: dict, monkeypatch
) -> None:
    svc = AffiliateSignalTransitionService(session)
    before = _counts(session)
    calls = {"n": 0}
    original = svc._signal_service.derive_affiliate_opportunity

    def _boom(keyword_id: int):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("derivation exploded")
        return original(keyword_id)

    monkeypatch.setattr(svc._signal_service, "derive_affiliate_opportunity", _boom)
    with pytest.raises(RuntimeError, match="derivation exploded"):
        svc.execute(allow_blocked=True)

    signals, scores, links = _counts(session)
    # 途中まで append された signal は残るが (append-only history)、scoring は 1 件も走らない
    assert scores == before[1] and links == before[2]
    assert signals == before[0] + 2


def test_execute_stops_before_scoring_when_the_row_delta_is_unexpected(
    session: Session, pool: dict, monkeypatch
) -> None:
    svc = AffiliateSignalTransitionService(session)
    before = _counts(session)
    original = svc._signal_service.derive_affiliate_opportunity

    def _double(keyword_id: int):
        original(keyword_id)
        return original(keyword_id)  # 想定外に 2 行入れる

    monkeypatch.setattr(svc._signal_service, "derive_affiliate_opportunity", _double)
    with pytest.raises(TransitionRefusedError, match="unexpected row counts after derivation"):
        svc.execute(allow_blocked=True)
    assert _counts(session)[1] == before[1]  # score は 1 件も増えていない


# ================================================================ idempotency
def test_a_second_execute_is_a_clean_no_op(session: Session, pool: dict) -> None:
    svc = AffiliateSignalTransitionService(session)
    svc.execute(allow_blocked=True)
    after_first = _counts(session)

    second = svc.execute(allow_blocked=True)

    assert second.already_current is True
    assert second.derived_keyword_ids == () and second.rescored_keyword_ids == ()
    assert (second.inserted_signals, second.inserted_scores, second.inserted_score_signals) == (
        0, 0, 0,
    )
    assert _counts(session) == after_first  # history を重ねない
    assert second.plan.has_work is False
    assert second.plan.rescore == []


def test_plan_after_a_transition_reports_nothing_to_do(session: Session, pool: dict) -> None:
    svc = AffiliateSignalTransitionService(session)
    svc.execute(allow_blocked=True)
    plan = svc.plan()
    assert plan.has_work is False
    assert plan.rescore_ids == ()
    assert plan.signal_value_changes == 0
    assert {o.decision for o in plan.outcomes} == {
        DECISION_UNCHANGED, DECISION_NEVER_SCORED, DECISION_BLOCKED_INCOMPLETE
    }


# ================================================================ CLI
def _factory(session: Session):
    return lambda: contextlib.nullcontext(session)


def test_cli_defaults_to_plan_and_writes_nothing(session: Session, pool: dict, capsys) -> None:
    before = _counts(session)

    code = cli.run(session_factory=_factory(session))  # execute の指定なし = PLAN

    out = capsys.readouterr().out
    assert code == cli.EXIT_REFUSED  # blocked な score があるので PLAN は 1 を返す
    assert "PLAN (default: zero writes)" in out
    assert "Nothing was written" in out
    assert _counts(session) == before


def test_cli_plan_reports_the_rescore_set_and_rank_changes(
    session: Session, pool: dict, capsys
) -> None:
    cli.run(session_factory=_factory(session))
    out = capsys.readouterr().out
    assert "rescore=1" in out and "never_scored=1" in out and "blocked=1" in out
    assert "expected inserts: signals=4 scores=1 score_signals=7" in out
    assert "re-score set" in out and "crm おすすめ" in out
    assert "expected rank changes" in out
    assert "missing: competition_ease" in out


def test_cli_json_output_is_machine_readable(session: Session, pool: dict, capsys) -> None:
    code = cli.run(output_format="json", session_factory=_factory(session))
    payload = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_REFUSED
    assert payload["mode"] == "plan"
    assert payload["summary"]["keywords_considered"] == 4
    assert payload["summary"]["rescore"] == 1
    assert payload["summary"]["rescore_keyword_ids"] == [pool["changed"]]
    assert payload["summary"]["expected_score_signal_inserts"] == 7
    assert payload["written"] is None
    assert len(payload["keywords"]) == 4
    changed = next(k for k in payload["keywords"] if k["keyword_id"] == pool["changed"])
    assert changed["decision"] == "rescore"
    assert changed["current_score_value"] == STALE
    assert changed["proposed_value"] != STALE


def test_cli_execute_writes_and_reports_the_counts(
    session: Session, pool: dict, capsys
) -> None:
    code = cli.run(execute=True, allow_blocked=True, session_factory=_factory(session))

    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "EXECUTE" in out
    assert "written: 4 affiliate signal(s), 1 score(s), 7 score-signal link(s)" in out
    assert f"re-scored keyword ids: [{pool['changed']}]" in out


def test_cli_execute_refuses_a_mismatched_expectation_without_writing(
    session: Session, pool: dict, capsys
) -> None:
    before = _counts(session)

    code = cli.run(
        execute=True, expect_rescores=8, allow_blocked=True, session_factory=_factory(session)
    )

    assert code == cli.EXIT_REFUSED
    assert "REFUSED (nothing was written)" in capsys.readouterr().err
    assert _counts(session) == before


def test_cli_execute_refuses_an_unexpected_pool_size_without_writing(
    session: Session, pool: dict, capsys
) -> None:
    before = _counts(session)

    code = cli.run(
        execute=True, expect_keywords=30, allow_blocked=True, session_factory=_factory(session)
    )

    assert code == cli.EXIT_REFUSED
    assert _counts(session) == before


def test_cli_second_execute_is_a_no_op(session: Session, pool: dict, capsys) -> None:
    assert cli.run(execute=True, allow_blocked=True, session_factory=_factory(session)) == 0
    capsys.readouterr()
    after_first = _counts(session)

    code = cli.run(execute=True, allow_blocked=True, session_factory=_factory(session))

    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "already current: nothing was written" in out
    assert _counts(session) == after_first


def test_cli_output_is_encodable_on_a_cp932_windows_console(
    session: Session, pool: dict, capsys
) -> None:
    """Windows console (cp932) で UnicodeEncodeError にならない (PLAN / JSON / EXECUTE / 拒否)。"""

    cli.run(session_factory=_factory(session))
    plan_out = capsys.readouterr().out
    cli.run(output_format="json", session_factory=_factory(session))
    json_out = capsys.readouterr().out
    cli.run(execute=True, expect_rescores=99, session_factory=_factory(session))
    refused = capsys.readouterr()
    cli.run(execute=True, allow_blocked=True, session_factory=_factory(session))
    execute_out = capsys.readouterr().out

    for text in (plan_out, json_out, refused.out, refused.err, execute_out):
        text.encode("cp932")  # raises UnicodeEncodeError on failure


def test_cli_never_prints_catalog_urls_or_credentials(
    session: Session, pool: dict, capsys
) -> None:
    program = AffiliateProgramRepository(session).get_by_name_and_provider("HubSpot", "Impact")
    program.tracking_url = "https://track.example.invalid/secret-token"
    program.landing_page_url = "https://www.example.invalid/landing"
    session.commit()

    cli.run(session_factory=_factory(session))
    plan_out = capsys.readouterr().out
    cli.run(output_format="json", session_factory=_factory(session))
    json_out = capsys.readouterr().out
    cli.run(execute=True, allow_blocked=True, session_factory=_factory(session))
    execute_out = capsys.readouterr().out

    for text in (plan_out, json_out, execute_out):
        assert "secret-token" not in text
        assert "example.invalid" not in text
        assert "http" not in text
