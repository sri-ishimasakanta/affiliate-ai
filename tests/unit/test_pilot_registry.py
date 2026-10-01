"""N7: 試しの利用者の記録と要約 (2026-10-01)。

**このテストの利用者・数はすべて、データの形を確かめるための合成の fixture。** 本物の利用者では
ない (本番の ``data/n7/`` には書かない。tmp のファイルだけ)。

pin する契約:

- 数えるのは ``human_entry`` で、合意の確認つきで登録された利用者だけ。``test_fixture`` は数えない。
  登録の無い ``pilot-xx`` の数 (台帳) は数えず、名前を出して外す。
- 無い数は ``missing`` (0 にしない)。支払いの証拠が無ければ ``missing`` (false にしない)。
- 同じ利用者を 2 度登録できない。出来事は追記だけで、直すときは ``correction`` (消さない)。
  登録を無効にすると数えない。終えた・やめた後は観測を足せない (直すことだけ)。
- 推定・仮説は記録できるが数えない。状態を変える出来事は事実の種類だけ。
- N8: 本物の試しが足りない・証拠が足りない → 必ず ``insufficient_evidence``。人が閾値を確かめる
  まで ``ready_for_human_decision`` にならない。決めない。
- 名前・メール・電話・URL・秘密は拒む。CLI は既定で書かない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.n_track import pilot as pilot_eval
from app.n_track import pilot_registry as reg
from app.services.pilot_registry_service import PilotRegistryService

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
START = "2026-10-10T10:00:00+09:00"
POLICY = pilot_eval.load_policy()
CONFIRMED = {**POLICY, "status": "confirmed"}
GOOD = {"activated": 1, "time_to_first_value_hours": 24, "workflows_completed": 3,
        "active_days": 12, "onboarding_difficulty": 2, "support_minutes": 30,
        "value_rating": 5, "would_continue": 1, "operating_cost_jpy": 500, "api_cost_jpy": 300,
        "willingness_to_pay_jpy": 3000}  # fmt: skip


def _service(tmp_path, policy=POLICY):
    return PilotRegistryService(tmp_path / "pilot_events.jsonl", policy=policy)


def _register(service, ref, *, provenance="human_entry", agreement=True):
    return service.add(pilot=ref, type_="register", observed_at=START,
                       evidence_kind="human_reported", source="pilot agreement",
                       entered_by="human",
                       data={"started_at": START, "use_case": "承認の流れ",
                             "acquisition": "referral",
                             "agreement_confirmed": agreement},
                       provenance=provenance, execute=True, now=NOW)  # fmt: skip


def _event(service, ref, type_, *, data=None, kind="human_reported", execute=True):
    return service.add(pilot=ref, type_=type_, observed_at="2026-10-20T10:00:00+09:00",
                       evidence_kind=kind, source="pilot interview", entered_by="human",
                       data=data or {"note": "週に 2 回使った"}, execute=execute, now=NOW)


def _rows(pilots: dict[str, dict]) -> list[dict]:
    rows, i = [], 0
    for ref, metrics in pilots.items():
        for metric, value in metrics.items():
            i += 1
            rows.append({"id": i, "subject_kind": "pilot", "subject_ref": ref, "metric": metric,
                         "value": value, "observed_at": NOW, "period_start": None,
                         "period_end": None})
    return rows


def test_no_pilots_means_insufficient_evidence(tmp_path) -> None:
    out = _service(tmp_path).report([])
    assert out["counts"]["real_pilots"] == 0
    assert out["n8_gate"]["state"] == "insufficient_evidence"
    assert out["n8_gate"]["rule_reading"] is None and "not decided" in out["n8_gate"]["decision"]


def test_test_fixtures_and_unregistered_refs_are_never_real(tmp_path) -> None:
    service = _service(tmp_path)
    _register(service, "pilot-01", provenance="test_fixture")
    out = service.report(_rows({"pilot-01": GOOD, "pilot-02": GOOD, "pilot-03": GOOD}))
    assert out["counts"]["real_pilots"] == 0 and out["counts"]["test_fixture_pilots_excluded"] == 1
    assert out["unregistered_metric_refs_excluded"] == ["pilot-01", "pilot-02", "pilot-03"]
    assert out["criteria"]["pilots"] == 0 and out["n8_gate"]["state"] == "insufficient_evidence"


def test_registration_needs_a_confirmed_agreement_and_is_unique(tmp_path) -> None:
    service = _service(tmp_path)
    with pytest.raises(reg.PilotError, match="agreement"):
        _register(service, "pilot-01", agreement=False)
    _register(service, "pilot-01")
    with pytest.raises(reg.PilotError, match="already registered"):
        _register(service, "pilot-01")
    with pytest.raises(reg.PilotError, match="not registered"):
        _event(service, "pilot-09", "usage")


def test_missing_is_not_zero_and_payment_is_missing_not_false(tmp_path) -> None:
    service = _service(tmp_path)
    _register(service, "pilot-01")
    out = service.report(_rows({"pilot-01": {"activated": 1, "active_days": 0}}))
    pilot = out["pilots"]["pilot-01"]
    assert pilot["payment"] == {"state": "missing"}
    assert pilot["repeat_usage"] is False  # 0 日は観測した値 (false)
    assert "payment_received_jpy" in out["missing_evidence"]["pilot-01"]
    assert "value_rating" in out["missing_evidence"]["pilot-01"]
    assert pilot["feedback"] == "missing" and pilot["evidence_coverage"] == "2/12"
    other = service.report(_rows({"pilot-01": {"activated": 1}}))["pilots"]["pilot-01"]
    assert other["repeat_usage"] == "missing"  # 数が無ければ missing (0 にしない)
    paid = service.report(_rows({"pilot-01": {"payment_received_jpy": 0}}))["pilots"]["pilot-01"]
    assert paid["payment"] == {"state": "observed", "jpy": 0}  # 0 円も観測した値


def test_events_are_append_only_and_corrections_supersede(tmp_path) -> None:
    service = _service(tmp_path)
    _register(service, "pilot-01")
    _event(service, "pilot-01", "blocker", data={"category": "setup", "note": "設定で迷った"})
    path = tmp_path / "pilot_events.jsonl"
    before = path.read_text("utf-8")
    service.add(pilot="pilot-01", type_="correction", observed_at=START,
                evidence_kind="human_reported", source="pilot interview", entered_by="human",
                data={"supersedes": 2, "reason": "別の利用者の話だった"}, execute=True, now=NOW)
    assert path.read_text("utf-8").startswith(before)  # 前の行はそのまま
    assert reg.fold(service.events())["pilot-01"].blockers == ()
    path.write_text(before.replace('"id": 2', '"id": 7'), encoding="utf-8")
    with pytest.raises(reg.PilotError, match="do not edit"):
        service.events()


def test_voiding_a_registration_removes_the_pilot(tmp_path) -> None:
    service = _service(tmp_path)
    _register(service, "pilot-01")
    service.add(pilot="pilot-01", type_="correction", observed_at=START,
                evidence_kind="human_reported", source="human", entered_by="human",
                data={"supersedes": 1, "reason": "登録の間違い"}, execute=True, now=NOW)
    assert service.report([])["counts"]["real_pilots"] == 0


def test_closed_and_withdrawn_pilots_take_only_corrections(tmp_path) -> None:
    service = _service(tmp_path)
    _register(service, "pilot-01")
    _register(service, "pilot-02")
    _event(service, "pilot-01", "close", data={"note": "期間を終えた"})
    _event(service, "pilot-02", "withdraw", data={"reason": "no_time", "note": None})
    for ref in ("pilot-01", "pilot-02"):
        with pytest.raises(reg.PilotError, match="only corrections"):
            _event(service, ref, "usage")
    counts = service.report([])["counts"]
    assert (counts["real_pilots"], counts["closed"], counts["withdrawn"], counts["active"]) == (
        2, 1, 1, 0)


def test_feedback_provenance_and_uncounted_inference(tmp_path) -> None:
    service = _service(tmp_path)
    _register(service, "pilot-01")
    result = _event(service, "pilot-01", "feedback", data={"note": "承認の門がわかりやすい"})
    event = result["event"]
    assert (event["evidence_kind"], event["source"], event["entered_by"], event["provenance"]) == (
        "human_reported", "pilot interview", "human", "human_entry")
    _event(service, "pilot-01", "usage", kind="inference", data={"note": "たぶん毎日使う"})
    state = reg.fold(service.events())["pilot-01"]
    assert state.feedback_events == 1 and state.usage_events == 0  # 推定は数えない
    assert state.evidence_kinds == {"human_reported": 2, "inference": 1}
    with pytest.raises(reg.PilotError, match="is a fact"):
        _event(service, "pilot-01", "close", kind="hypothesis", data={"note": "終わったはず"})


def test_n8_gate_needs_real_evidence_and_confirmed_thresholds(tmp_path) -> None:
    service = _service(tmp_path)
    for ref in ("pilot-01", "pilot-02", "pilot-03"):
        _register(service, ref)
    rows = _rows({ref: GOOD for ref in ("pilot-01", "pilot-02", "pilot-03")})
    proposed = service.report(rows)["n8_gate"]
    assert proposed["state"] == "needs_human_policy" and proposed["rule_reading"]
    confirmed = _service(tmp_path, CONFIRMED).report(rows)["n8_gate"]
    assert confirmed["state"] == "ready_for_human_decision"
    assert confirmed["criteria_overall"] == "evidence_supports_go"
    assert confirmed["candidates"] == ["SaaS", "Managed Service", "Hybrid"]
    assert "not decided" in confirmed["decision"]
    two = _service(tmp_path, CONFIRMED).report(rows[: len(GOOD) * 2])  # 2 人分の数だけ
    assert two["n8_gate"]["state"] == "insufficient_evidence"


@pytest.mark.parametrize("field, value", [
    ("note", "連絡先は taro@example.com"), ("note", "電話 090-1234-5678"),
    ("note", "https://example.com/me を見た"), ("use_case", "Bearer abcdefghijklmnopqrstuvwx")])
def test_personal_data_and_secrets_are_refused(tmp_path, field, value) -> None:
    service = _service(tmp_path)
    if field == "use_case":
        with pytest.raises(reg.PilotError):
            service.add(pilot="pilot-01", type_="register", observed_at=START,
                        evidence_kind="human_reported", source="pilot agreement",
                        entered_by="human", data={"started_at": START, "use_case": value,
                                                  "acquisition": "referral",
                                                  "agreement_confirmed": True}, now=NOW)
        return
    _register(service, "pilot-01")
    with pytest.raises(reg.PilotError):
        _event(service, "pilot-01", "feedback", data={"note": value})


def test_names_and_future_times_are_refused(tmp_path) -> None:
    service = _service(tmp_path)
    with pytest.raises(reg.PilotError, match="pseudonym"):
        _register(service, "Yamada")
    with pytest.raises(reg.PilotError, match="future"):
        service.add(pilot="pilot-01", type_="register",
                    observed_at=(NOW + timedelta(days=1)).isoformat(),
                    evidence_kind="human_reported", source="pilot agreement",
                    entered_by="human", data={"started_at": START, "use_case": "x",
                                              "acquisition": "referral",
                                              "agreement_confirmed": True}, now=NOW)


def test_the_cli_plans_by_default_and_reports(tmp_path, capsys) -> None:
    from scripts.manage_pilots import main

    path = tmp_path / "events.jsonl"
    args = ["register", "pilot-01", "--started-at", START, "--use-case", "承認の流れ",
            "--acquisition", "referral", "--agreement-confirmed", "--source", "pilot agreement",
            "--by", "human"]
    assert main(args, path=path, now=NOW) == 0
    assert "PLAN" in capsys.readouterr().out and not path.exists()
    assert main([*args, "--execute"], path=path, now=NOW) == 0
    capsys.readouterr()
    assert main(["show", "pilot-01"], path=path, now=NOW) == 0
    assert '"status": "active"' in capsys.readouterr().out
    assert main(["usage", "pilot-01", "--source", "x", "--by", "human", "--note", "a"],
                path=path, now=NOW) == 2  # 出どころが短すぎる
    assert "refused" in capsys.readouterr().out
    lines = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    assert len(lines) == 1 and lines[0]["provenance"] == "human_entry"
