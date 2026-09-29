"""note チャネルのローカルの状態 (読むだけ。note には問い合わせない)。

``reports/note/`` (``scripts/propose_note_content.py`` が書く、git 管理外) を読み、候補の数・
下書きの状態・公開の数・人の確認待ちを数える。公開の数は、公開の証拠 (``publication.url``) を
持つ下書きだけ。ファイルが無ければ 0 件として扱う (note は外部の依存にしない)。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

NOTE_DIR = Path("reports/note")
#: N 系の段階 (定義は docs/operations/n-track-plan.md)。
NOTE_PHASES = tuple(f"N{i}" for i in range(9))


def collect_note_channel(root: Path, roadmap_phases: list[dict]) -> dict:
    status = {p["id"]: p["status"] for p in roadmap_phases}
    candidates_path = root / NOTE_DIR / "candidates_latest.json"
    candidates = []
    generated_at = None
    if candidates_path.exists():
        data = json.loads(candidates_path.read_text(encoding="utf-8"))
        candidates = data.get("candidates") or []
        generated_at = data.get("generated_at")
    drafts = []
    for path in sorted((root / NOTE_DIR / "drafts").glob("*.json")):
        try:
            drafts.append(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            continue
    by_status = Counter(d.get("status", "draft") for d in drafts)
    published = [d for d in drafts if (d.get("publication") or {}).get("url")]
    return {
        "phases": {pid: status.get(pid) for pid in NOTE_PHASES},
        "candidates": len(candidates),
        "candidates_generated_at": generated_at,
        "top_candidate": candidates[0].get("working_title") if candidates else None,
        "drafts": dict(sorted(by_status.items())),
        "published_with_evidence": len(published),
        "pending_human_review": by_status.get("review_ready", 0),
        "source": "reports/note/ (local files; note itself is never contacted)",
    }
