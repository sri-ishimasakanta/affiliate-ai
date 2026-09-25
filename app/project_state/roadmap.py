"""フェーズの状態 (``docs/project-roadmap.json`` の宣言と、その根拠の確かめ)。

根拠のファイルがフェーズの ID を含むこと・commit があることを確かめる。確かめられなければ
``verified: False`` と理由を付ける (宣言を黙って信じない・書き換えない)。

- ``current_phase``: 今進めているフェーズ (``active``)。進めているものが無ければ ``null``。
- ``next_phase``: 次に始めるフェーズ (``planned``。まだ始めていない)。前提がすべて
  ``complete`` であること。
- ``last_completed_phase``: 最後に完了したフェーズ (``complete``)。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

ROADMAP = Path("docs/project-roadmap.json")
STATUSES = ("complete", "active", "planned", "deferred")


def load_roadmap(root: Path) -> dict:
    return json.loads((root / ROADMAP).read_text(encoding="utf-8"))


def verify_phases(
    root: Path, roadmap: dict, *, commit_exists: Callable[[str], bool] | None
) -> dict:
    phases = []
    problems = []
    for phase in roadmap["phases"]:
        issues = []
        if phase["status"] not in STATUSES:
            issues.append(f"unknown status {phase['status']!r}")
        pattern = re.compile(rf"(?<![\w.]){re.escape(phase['id'])}(?![\w])")
        for rel in phase["evidence"].get("files", []):
            path = root / rel
            if not path.exists():
                issues.append(f"{rel} is missing")
            elif rel != str(ROADMAP).replace("\\", "/") and not pattern.search(
                path.read_text(encoding="utf-8", errors="replace")
            ):
                issues.append(f"{rel} does not mention {phase['id']}")
        if commit_exists is not None:
            issues += [
                f"commit {c} not found"
                for c in phase["evidence"].get("commits", [])
                if not commit_exists(c)
            ]
        has_evidence = bool(phase["evidence"].get("files") or phase["evidence"].get("commits"))
        phases.append(
            {
                "id": phase["id"],
                "title": phase["title"],
                "status": phase["status"],
                "verified": has_evidence and not issues,
                "evidence_kind": "repository" if has_evidence else "declared_only",
                "issues": issues,
                **({"note": phase["note"]} if phase.get("note") else {}),
                **({"prerequisites": phase["prerequisites"]} if phase.get("prerequisites") else {}),
            }
        )
        if issues:
            problems.append(f"{phase['id']}: {issues}")
    by_status = {s: [p["id"] for p in phases if p["status"] == s] for s in STATUSES}
    active = by_status["active"]
    current = roadmap.get("current_phase")
    if current is None:
        if active:
            problems.append(f"current_phase is null but {active} are active")
    elif current not in active:
        problems.append(f"current_phase {current} is not the active phase {active}")
    next_phase = roadmap.get("next_phase")
    next_unmet = []
    if next_phase is not None:
        by_id = {p["id"]: p for p in roadmap["phases"]}
        if (by_id.get(next_phase) or {}).get("status") != "planned":
            problems.append(f"next_phase {next_phase} is not a planned phase")
        next_unmet = [
            q
            for q in (by_id.get(next_phase) or {}).get("prerequisites", [])
            if q not in by_status["complete"]
        ]
        if next_unmet:
            problems.append(f"next_phase {next_phase} has unmet prerequisites {next_unmet}")
    last = roadmap.get("last_completed_phase")
    if last is not None and last not in by_status["complete"]:
        problems.append(f"last_completed_phase {last} is not complete")
    return {
        "declared_current_phase": current,
        "next_phase": next_phase,
        "next_phase_prerequisites_unmet": next_unmet,
        "last_completed_phase": last,
        "completed": by_status["complete"],
        "active": active,
        "upcoming": by_status["planned"],
        "deferred": by_status["deferred"],
        "phases": phases,
        "problems": problems,
    }
