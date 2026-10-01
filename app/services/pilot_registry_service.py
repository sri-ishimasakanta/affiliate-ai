"""N7 の試しの利用者の記録 (追記だけの出来事のファイル) と要約。**外に送らない。推定しない。**

- 出来事: ``data/n7/pilot_events.jsonl`` (git 管理外の手元のファイル。1 行に 1 つの出来事。
  書き換えない・消さない。直すときは ``correction`` を足す)。
- 数: N3 の台帳 (``manual_metric_entries``、``subject_kind = pilot``)。ここでは読むだけ。
- ``add(..., execute)``: 既定は PLAN (検査だけ。書かない)。
- ``report()``: ``app/n_track/pilot_registry.report`` (本物の利用者だけを数える・無いものは
  無いと出す・N8 に進めるか)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.n_track import pilot as pilot_eval
from app.n_track import pilot_registry as reg

DEFAULT_PATH = Path("data/n7/pilot_events.jsonl")


class PilotRegistryService:
    def __init__(self, path: Path | str = DEFAULT_PATH, *, policy: dict | None = None,
                 confirmations_path: Path | str | None = None) -> None:  # fmt: skip
        self._path = Path(path)
        self._policy = policy if policy is not None else pilot_eval.load_policy()
        self._confirmations = Path(confirmations_path or pilot_eval.CONFIRMATIONS_PATH)

    def events(self) -> list[dict]:
        if not self._path.exists():
            return []
        out = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("schema") != reg.SCHEMA or event.get("id") != len(out) + 1:
                raise reg.PilotError(f"{self._path} line {number} is not the next "
                                     f"{reg.SCHEMA} event (do not edit the file)")
            out.append(event)
        return out

    def add(self, *, execute: bool = False, now: datetime | None = None, **fields) -> dict:
        now = now or datetime.now(UTC)
        events = self.events()
        event = reg.build_event(events, now=now, **fields)
        if not execute:
            return {"recorded": False, "reason": "PLAN (re-run with --execute)", "event": event}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return {"recorded": True, "event": event}

    def show(self, pilot: str) -> dict:
        events = [e for e in self.events() if e["pilot"] == pilot]
        state = reg.fold(self.events()).get(pilot)
        if state is None:
            raise reg.PilotError(f"{pilot} is not registered")
        return {"pilot": pilot, "state": state.__dict__, "events": events}

    def list(self) -> list[dict]:
        return [{"pilot": ref, "status": s.status, "onboarding": s.onboarding,
                 "real": s.real, "started_at": s.started_at,
                 "last_observed_at": s.last_observed_at}
                for ref, s in sorted(reg.fold(self.events()).items())]  # fmt: skip

    def report(self, metric_rows: list[dict]) -> dict:
        return reg.report(self.events(), metric_rows, policy=self._policy,
                          confirmations=pilot_eval.load_confirmations(self._confirmations))

    def policy(self) -> dict:
        """今の基準と、人の確認の状態 (読むだけ)。"""

        identity = pilot_eval.policy_identity(
            self._policy, pilot_eval.load_confirmations(self._confirmations))
        return {**identity, "min_pilots": self._policy.get("min_pilots"),
                "criteria": self._policy.get("criteria"),
                "model_signals": self._policy.get("model_signals")}

    def confirm_policy(self, *, policy_hash: str, by: str, now: datetime | None = None,
                       note: str | None = None, execute: bool = False) -> dict:
        """人の基準の確認を記録する (追記だけ)。既定は PLAN。"""

        now = now or datetime.now(UTC)
        records = pilot_eval.load_confirmations(self._confirmations)
        record = pilot_eval.confirm_policy(self._policy, records, policy_hash_given=policy_hash,
                                           by=by, now=now, note=note)  # fmt: skip
        if not execute:
            return {"recorded": False, "reason": "PLAN (re-run with --execute)", "record": record}
        self._confirmations.write_text(json.dumps(
            {"schema": pilot_eval.CONFIRMATIONS_SCHEMA, "records": [*records, record]},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")  # fmt: skip
        return {"recorded": True, "record": record}


__all__ = ["DEFAULT_PATH", "PilotRegistryService"]
