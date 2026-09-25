"""フェーズの状態 (``docs/project-roadmap.json`` の宣言と、その根拠の確かめ)。

根拠のファイルがフェーズの ID を含むこと・commit があることを確かめる。確かめられなければ
``verified: False`` と理由を付ける (宣言を黙って信じない・書き換えない)。
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
    if roadmap.get("current_phase") not in active:
        problems.append(
            f"current_phase {roadmap.get('current_phase')} is not the active phase {active}"
        )
    return {
        "declared_current_phase": roadmap.get("current_phase"),
        "completed": by_status["complete"],
        "active": active,
        "upcoming": by_status["planned"],
        "deferred": by_status["deferred"],
        "phases": phases,
        "problems": problems,
    }
