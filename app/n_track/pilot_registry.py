"""SaaS の検証の試しの利用者の記録 (N7、2026-10-01。pure)。**作らない。推定しない。決めない。**

数 (利用・費用・評価・払う意思・実際の支払い) は今までどおり N3 の台帳
(``manual_metric_entries``、``subject_kind = pilot``)。ここは数で表せないものを、追記だけの
出来事 (event) として持つ:

- ``register``: 仮名 ``pilot-xx`` の試しの利用者を、人が実在を確かめて登録する (合意は人が
  リポジトリの外で持つ。``agreement_confirmed`` が要る)。開始時刻・用途・知った経路。
- ``onboarding`` (状態)・``usage``・``outcome``・``feedback``・``blocker``: 観測や声。
- ``close`` (試しを終えた)・``withdraw`` (途中でやめた)。終わった後は観測を足せない。
- ``correction``: 前の出来事を指して無効にする (消さない)。登録を無効にすれば、その利用者は
  数えない (間違えて作ったとき)。

すべての出来事に証拠の種類 (``EVIDENCE_KINDS``)・出どころ・入れた人・出どころの区分
(``provenance``) がある。数えるのは ``provenance = human_entry`` で、合意の確認つきで登録された
利用者だけ。テストの fixture (``test_fixture``) は数えない。無い数は「無い」(missing) のまま
で、0 にしない。推定・仮説は記録できるが、証拠として数えない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.n_track import metrics as mm
from app.social.note import safety

SCHEMA = "n7-pilot-event/1"
EVENT_TYPES = ("register", "onboarding", "usage", "outcome", "feedback", "blocker", "close",
               "withdraw", "correction")  # fmt: skip
#: 証拠の種類。``inference`` / ``hypothesis`` は記録できるが、証拠としては数えない。
EVIDENCE_KINDS = ("observed_fact", "human_reported", "measured_metric", "inference", "hypothesis")
COUNTED_EVIDENCE = ("observed_fact", "human_reported", "measured_metric")
PROVENANCES = ("human_entry", "test_fixture")
ACQUISITION = ("own_network", "referral", "community", "inbound", "other")
ONBOARDING_STATES = ("not_started", "in_progress", "completed", "blocked")
BLOCKERS = ("setup", "approvals", "cost", "trust", "other")
WITHDRAW_REASONS = ("pilot_choice", "no_fit", "no_time", "other")
_PILOT = re.compile(r"^pilot-[a-z0-9-]{1,32}$")
#: 試しの判断に使う数 (N3 の台帳の pilot の指標)。無ければ「無い」と出す。
EVIDENCE_METRICS = ("activated", "time_to_first_value_hours", "workflows_completed",
                    "active_days", "onboarding_difficulty", "support_minutes", "value_rating",
                    "would_continue", "operating_cost_jpy", "api_cost_jpy",
                    "willingness_to_pay_jpy", "payment_received_jpy")  # fmt: skip
FEEDBACK_METRICS = ("value_rating", "would_continue")
FRICTION_METRICS = ("friction_setup", "friction_approvals", "friction_cost", "friction_trust")


class PilotError(ValueError):
    pass


@dataclass
class PilotState:
    ref: str
    registered_event: int
    started_at: str
    use_case: str
    acquisition: str
    provenance: str
    status: str = "active"  # active / closed / withdrawn
    onboarding: str = "not_started"
    last_observed_at: str | None = None
    events: int = 0
    feedback_events: int = 0
    usage_events: int = 0
    outcome_events: int = 0
    blockers: tuple = ()
    evidence_kinds: dict | None = None

    @property
    def real(self) -> bool:
        return self.provenance == "human_entry"


def _text(value, *, field: str, required: bool = True, limit: int = 300) -> str | None:
    text = (value or "").strip()
    if not text:
        if required:
            raise PilotError(f"{field} is required")
        return None
    if mm._PERSONAL.search(text):
        raise PilotError(f"{field} looks like it contains personal data (email / phone / URL / "
                         "address / card number)")
    if mm._CREDENTIAL.search(text) or safety.sanitize(text)[1]:
        raise PilotError(f"{field} contains a secret or internal value")
    return text[:limit]


def _time(value: str, *, field: str, now: datetime) -> str:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise PilotError(f"{field} must be an ISO time with a timezone") from None
    if moment.tzinfo is None:
        raise PilotError(f"{field} needs a timezone (e.g. +09:00)")
    if moment > now:
        raise PilotError(f"{field} is in the future (no estimates)")
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def build_event(events: list[dict], *, pilot: str, type_: str, observed_at: str,
                evidence_kind: str, source: str, entered_by: str, now: datetime,
                data: dict | None = None, provenance: str = "human_entry") -> dict:
    """検査して、次に足す出来事を作る (書くのは呼ぶ側)。"""

    data = dict(data or {})
    if not _PILOT.match(pilot or ""):
        raise PilotError("a pilot is a pseudonym like pilot-01 (no names)")
    if type_ not in EVENT_TYPES:
        raise PilotError(f"event type must be one of {EVENT_TYPES}")
    if evidence_kind not in EVIDENCE_KINDS:
        raise PilotError(f"evidence kind must be one of {EVIDENCE_KINDS}")
    if provenance not in PROVENANCES:
        raise PilotError(f"provenance must be one of {PROVENANCES}")
    if type_ in ("register", "onboarding", "close", "withdraw") and (
            evidence_kind not in COUNTED_EVIDENCE):  # fmt: skip
        raise PilotError(f"a {type_} event is a fact (observed_fact / human_reported), not an "
                         "inference or hypothesis")
    by = (entered_by or "").strip()
    if not by or "@" in by:
        raise PilotError("entered_by is a short name (no email)")
    event = {"schema": SCHEMA, "id": len(events) + 1, "pilot": pilot, "type": type_,
             "observed_at": _time(observed_at, field="observed_at", now=now),
             "entered_at": now.astimezone(UTC).isoformat(timespec="seconds"),
             "evidence_kind": evidence_kind,
             "source": _text(source, field="source", limit=200), "entered_by": by[:64],
             "provenance": provenance}  # fmt: skip
    if len(event["source"]) < 3:
        raise PilotError("say where this came from (source)")
    state = fold(events)
    if type_ == "register":
        if pilot in state:
            raise PilotError(f"{pilot} is already registered (no duplicate pilots)")
        if data.get("agreement_confirmed") is not True:
            raise PilotError("register only a real pilot whose agreement the human holds "
                             "(agreement_confirmed)")  # fmt: skip
        if data.get("acquisition") not in ACQUISITION:
            raise PilotError(f"acquisition must be one of {ACQUISITION}")
        event["data"] = {"started_at": _time(data.get("started_at"), field="started_at", now=now),
                         "use_case": _text(data.get("use_case"), field="use_case", limit=200),
                         "acquisition": data["acquisition"], "agreement_confirmed": True}
        return event
    if pilot not in state:
        raise PilotError(f"{pilot} is not registered (register it first)")
    current = state[pilot]
    if type_ == "correction":
        target = int(data.get("supersedes") or 0)
        found = next((e for e in events if e["id"] == target), None)
        if found is None or found["pilot"] != pilot:
            raise PilotError("a correction points to an earlier event of the same pilot")
        event["data"] = {"supersedes": target,
                         "reason": _text(data.get("reason"), field="reason", limit=300)}
        return event
    if current.status != "active":
        raise PilotError(f"{pilot} is {current.status}; only corrections can be added")
    if type_ == "onboarding":
        if data.get("state") not in ONBOARDING_STATES:
            raise PilotError(f"onboarding state must be one of {ONBOARDING_STATES}")
        event["data"] = {"state": data["state"],
                         "note": _text(data.get("note"), field="note", required=False)}
    elif type_ == "blocker":
        if data.get("category") not in BLOCKERS:
            raise PilotError(f"blocker category must be one of {BLOCKERS}")
        event["data"] = {"category": data["category"],
                         "note": _text(data.get("note"), field="note")}
    elif type_ == "withdraw":
        if data.get("reason") not in WITHDRAW_REASONS:
            raise PilotError(f"withdraw reason must be one of {WITHDRAW_REASONS}")
        event["data"] = {"reason": data["reason"],
                         "note": _text(data.get("note"), field="note", required=False)}
    else:  # usage / outcome / feedback / close
        event["data"] = {"note": _text(data.get("note"), field="note")}
    return event


def _active(events: list[dict]) -> list[dict]:
    voided = {e["data"]["supersedes"] for e in events if e["type"] == "correction"}
    return [e for e in events if e["id"] not in voided]


def fold(events: list[dict]) -> dict[str, PilotState]:
    """出来事から利用者ごとの今の状態を作る (無効にされた出来事は除く)。"""

    out: dict[str, PilotState] = {}
    for e in _active(events):
        if e["type"] == "register":
            d = e["data"]
            out[e["pilot"]] = PilotState(ref=e["pilot"], registered_event=e["id"],
                                         started_at=d["started_at"], use_case=d["use_case"],
                                         acquisition=d["acquisition"],
                                         provenance=e["provenance"], evidence_kinds={})
        state = out.get(e["pilot"])
        if state is None or e["type"] == "correction":
            continue
        state.events += 1
        state.evidence_kinds[e["evidence_kind"]] = state.evidence_kinds.get(
            e["evidence_kind"], 0) + 1  # fmt: skip
        if state.last_observed_at is None or e["observed_at"] > state.last_observed_at:
            state.last_observed_at = e["observed_at"]
        if e["evidence_kind"] not in COUNTED_EVIDENCE:
            continue  # 推定・仮説は記録だけ (数えない)
        if e["type"] == "onboarding":
            state.onboarding = e["data"]["state"]
        elif e["type"] == "usage":
            state.usage_events += 1
        elif e["type"] == "outcome":
            state.outcome_events += 1
        elif e["type"] == "feedback":
            state.feedback_events += 1
        elif e["type"] == "blocker":
            state.blockers = (*state.blockers, e["data"]["category"])
        elif e["type"] == "close":
            state.status = "closed"
        elif e["type"] == "withdraw":
            state.status = "withdrawn"
    return out


def report(events: list[dict], metric_rows: list[dict], *, policy: dict,
           confirmations: list[dict] | None = None) -> dict:
    """N7 の要約: 本物の試しの利用者・状態・証拠の範囲・無いもの・N8 に進めるか。"""

    from app.n_track import pilot as pilot_eval

    states = fold(events)
    real = {ref: s for ref, s in states.items() if s.real}
    rows = [r for r in metric_rows if r["subject_kind"] == "pilot"]
    unregistered = sorted({r["subject_ref"] for r in rows} - set(real))
    real_rows = [r for r in rows if r["subject_ref"] in real]
    latest = pilot_eval.latest_by_pilot(real_rows)
    per_pilot, missing = {}, {}
    for ref, state in sorted(real.items()):
        values = latest.get(ref, {})
        lacks = [m for m in EVIDENCE_METRICS if m not in values]
        missing[ref] = lacks
        per_pilot[ref] = {
            "status": state.status, "onboarding": state.onboarding,
            "started_at": state.started_at, "last_observed_at": state.last_observed_at,
            "use_case": state.use_case, "acquisition": state.acquisition,
            "evidence_coverage": f"{len(EVIDENCE_METRICS) - len(lacks)}/{len(EVIDENCE_METRICS)}",
            "feedback": ("recorded" if state.feedback_events or any(
                m in values for m in FEEDBACK_METRICS) else "missing"),
            "payment": ({"state": "observed", "jpy": values["payment_received_jpy"]}
                        if "payment_received_jpy" in values else {"state": "missing"}),
            "repeat_usage": ("missing" if "active_days" not in values else
                             values["active_days"] >= policy["criteria"][
                                 "repeat_usage_active_days_min"]),
            "blockers": sorted(set(state.blockers) | {m.removeprefix("friction_")
                                                      for m in FRICTION_METRICS
                                                      if values.get(m) == 1}),
            "evidence_kinds": state.evidence_kinds,
        }  # fmt: skip
    evaluation = pilot_eval.evaluate(real_rows, policy=policy)
    identity = pilot_eval.policy_identity(policy, confirmations or [])
    minimum = int(policy.get("min_pilots", 3))
    counts = {
        "real_pilots": len(real),
        "active": sum(1 for s in real.values() if s.status == "active"),
        "closed": sum(1 for s in real.values() if s.status == "closed"),
        "withdrawn": sum(1 for s in real.values() if s.status == "withdrawn"),
        "onboarding_completed": sum(1 for s in real.values() if s.onboarding == "completed"),
        "with_feedback": sum(1 for p in per_pilot.values() if p["feedback"] == "recorded"),
        "with_payment_evidence": sum(1 for p in per_pilot.values()
                                     if p["payment"]["state"] == "observed"),
        "repeat_usage": sum(1 for p in per_pilot.values() if p["repeat_usage"] is True),
        "repeat_usage_missing": sum(1 for p in per_pilot.values()
                                    if p["repeat_usage"] == "missing"),
        "test_fixture_pilots_excluded": sum(1 for s in states.values() if not s.real),
    }  # fmt: skip
    return {"counts": counts, "pilots": per_pilot, "missing_evidence": missing,
            "unregistered_metric_refs_excluded": unregistered, "criteria": evaluation,
            "policy": identity,
            "n8_gate": n8_gate(counts, evaluation, identity=identity, minimum=minimum),
            "reading": ("recorded evidence only: missing stays missing (never 0); inference and "
                        "hypothesis entries are kept but not counted; not a decision")}  # fmt: skip


N8_CANDIDATES = ("SaaS", "Managed Service", "Hybrid")


def n8_gate(counts: dict, evaluation: dict, *, identity: dict, minimum: int) -> dict:
    """N8 へ進む判断の材料。本物の試しが足りなければ必ず ``insufficient_evidence``。

    条件 (今の規則): 本物の試しの利用者が ``min_pilots`` 以上・go / no-go のどの条件も証拠が
    足りる (``insufficient`` が無い)・基準が人に確かめられている (確認の記録の hash が今の基準と
    同じ)。基準を確かめただけでは進めない。
    """

    conditions = {
        "real_pilots_at_least_min": counts["real_pilots"] >= minimum,
        "no_insufficient_criteria": evaluation["overall"] != "insufficient_evidence",
        "policy_confirmed": identity["state"] == "confirmed",
    }
    blockers = []
    if counts["real_pilots"] < minimum:
        blockers.append(f"real pilots {counts['real_pilots']} < min_pilots {minimum}")
    if evaluation["overall"] == "insufficient_evidence":
        blockers.append("go / no-go criteria lack evidence (insufficient)")
    if identity["state"] != "confirmed":
        blockers.append(f"the go / no-go thresholds are {identity['state']!r} (policy hash "
                        f"{identity['policy_hash'][:12]}); the human confirms them before "
                        "deciding")  # fmt: skip
    if counts["real_pilots"] < minimum or evaluation["overall"] == "insufficient_evidence":
        state = "insufficient_evidence"
    else:
        state = "ready_for_human_decision" if not blockers else "needs_human_policy"
    return {"state": state, "conditions": conditions, "candidates": list(N8_CANDIDATES),
            "blockers": blockers,
            "criteria_overall": evaluation["overall"],
            "rule_reading": (evaluation["model_signals"].get("reading")
                             if state != "insufficient_evidence" else None),
            "decision": "not decided (the human decides and logs it)"}  # fmt: skip


__all__ = ["ACQUISITION", "BLOCKERS", "COUNTED_EVIDENCE", "EVENT_TYPES", "EVIDENCE_KINDS",
           "EVIDENCE_METRICS", "N8_CANDIDATES", "ONBOARDING_STATES", "PROVENANCES",
           "PilotError", "PilotState", "SCHEMA", "WITHDRAW_REASONS", "build_event", "fold",
           "n8_gate", "report"]
