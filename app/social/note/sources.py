"""note の材料を読む (読むだけ。外部に問い合わせない)。

出どころの順 (強い順。T7 の ``precedence`` と同じ名前を使う):

1. ``runtime_report``: プロジェクトの状態の報告 (``reports/project_state_latest.json``。T7 が
   本番を読んで作った事実)
2. ``committed_manifest``: ``docs/project-roadmap.json`` (完了したフェーズと commit)
3. ``decision_log``: ``docs/decision-log/*.md`` (長く効く決定)
4. ``operations_doc``: ``docs/operations/*.md`` (決まった言い回しがあることだけを確かめる)
5. ``local_db`` (任意): 重複の検査のための WordPress の記事本文と Threads の公開文 (読むだけ)

生のログ (worker のログなど)・秘密を含みうるファイル・推測は材料にしない。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROADMAP = Path("docs/project-roadmap.json")
DECISION_LOG = Path("docs/decision-log")
PROJECT_STATE = Path("reports/project_state_latest.json")
DIAGNOSTIC = Path("reports/threads_performance_diagnostic_latest.json")
_ENTRY = re.compile(
    r"<!-- decision:(?P<id>[\w.-]+) -->\s*\n### (?P<title>[^\n]+)\n"
    r"(?P<body>.*?)(?=<!-- decision:|\Z)",
    re.S,
)
_FIELD = re.compile(r"^- (?P<key>[^:：]+): (?P<value>.*)$", re.M)
_FIELD_NAMES = {
    "記録": "recorded",
    "領域": "area",
    "理由": "rationale",
    "根拠": "evidence",
    "結果の状態": "resulting_state",
    "次にすること": "follow_up",
}


@dataclass
class SourceBundle:
    roadmap: dict
    decisions: dict[str, dict]
    project_state: dict | None
    diagnostic: dict | None
    docs: dict[str, str] = field(default_factory=dict)

    @property
    def completed_phases(self) -> list[str]:
        return [p["id"] for p in self.roadmap.get("phases", []) if p.get("status") == "complete"]

    def phase(self, pid: str) -> dict | None:
        return next((p for p in self.roadmap.get("phases", []) if p["id"] == pid), None)

    def fact(self, name: str):
        facts = (self.project_state or {}).get("facts") or {}
        return facts.get(name)


def parse_decision_log(text: str) -> dict[str, dict]:
    entries = {}
    for match in _ENTRY.finditer(text):
        fields = {
            _FIELD_NAMES[m["key"].strip()]: m["value"].strip()
            for m in _FIELD.finditer(match["body"])
            if m["key"].strip() in _FIELD_NAMES
        }
        entries.setdefault(
            match["id"], {"id": match["id"], "title": match["title"].strip(), **fields}
        )
    return entries


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_sources(root: Path, *, doc_names: tuple[str, ...] = ()) -> SourceBundle:
    decisions: dict[str, dict] = {}
    log_dir = root / DECISION_LOG
    if log_dir.is_dir():
        for path in sorted(log_dir.glob("*.md")):
            for key, entry in parse_decision_log(path.read_text(encoding="utf-8")).items():
                decisions.setdefault(key, {**entry, "file": path.relative_to(root).as_posix()})
    docs = {}
    for name in doc_names:
        path = root / name
        if path.exists():
            docs[name] = path.read_text(encoding="utf-8")
    return SourceBundle(
        roadmap=_read_json(root / ROADMAP) or {"phases": []},
        decisions=decisions,
        project_state=_read_json(root / PROJECT_STATE),
        diagnostic=_read_json(root / DIAGNOSTIC),
        docs=docs,
    )


def duplication_corpus(conn) -> dict[str, str]:
    """重複の検査の相手: WordPress の記事本文と Threads の公開文 (SELECT だけ)。"""

    from sqlalchemy import text

    corpus = {}
    for row in conn.execute(text("select id, body from articles where body is not null")):
        corpus[f"wordpress:article:{row.id}"] = row.body
    for row in conn.execute(
        text("select id, exact_published_text from threads_publications where status = 'published'")
    ):
        corpus[f"threads:publication:{row.id}"] = row.exact_published_text or ""
    # N2: note で公開済みの本文 (台帳。migration の前の DB には表が無いので飛ばす)
    try:
        rows = conn.execute(
            text("select draft_id, body_text from note_pieces where status = 'published'")
        ).fetchall()
    except Exception:  # noqa: BLE001 - 表が無い DB でも他の相手で検査を続ける
        rows = []
    for row in rows:
        corpus[f"note:{row.draft_id}"] = row.body_text or ""
    return corpus
