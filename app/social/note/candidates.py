"""note の候補の発見と採点 (決定的・読むだけ・LLM もネットワークも使わない)。

発見: ``catalog.TOPICS`` の各話題について、根拠が今もリポジトリ / 報告にあるかを確かめる。
根拠の半分未満しか無い話題は候補にしない。同じ出来事 (source event) から 2 つの候補を作らない。

採点 (優先度のためだけ。公開を決めない。各 0〜3 の整数の和 − 重複の罰点):

- ``usefulness``: 型ごとの読み手への役立ち (decision_note / experiment_result 3、ほか 2)
- ``novelty``: 同じ出来事の下書き / 公開がまだ無ければ 3、あれば 0
- ``evidence``: 根拠のそろい具合 (3 × 見つかった数 / 必要な数、切り捨て)
- ``significance``: 完了したフェーズの数 (3 以上 3、2 は 2、1 は 1)
- ``readability``: 初心者が読めるか (話題ごとの編集判断 1〜3)
- ``recency``: 最近の 3 つの完了フェーズのどれかに触れていれば 2、それ以外 1
- ``duplication_penalty``: 重複の危険 (low 0 / medium 1 / high 2)
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from app.social.note.catalog import TOPICS
from app.social.note.models import EvidenceRef, NoteCandidate
from app.social.note.sources import SourceBundle

USEFULNESS = {"decision_note": 3, "experiment_result": 3, "build_log": 2, "milestone_recap": 2}
PENALTY = {"low": 0, "medium": 1, "high": 2}
MIN_COMPLETENESS = 0.5


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _evidence(topic: dict, sources: SourceBundle, root_docs: dict[str, str]):
    found: list[EvidenceRef] = []
    required = 0
    warnings = []
    observed_at = (sources.project_state or {}).get("generated_at")
    for pid in topic["phases"]:
        required += 1
        phase = sources.phase(pid)
        if phase and phase.get("status") == "complete":
            commits = ",".join(phase.get("evidence", {}).get("commits", [])) or "roadmap"
            found.append(
                EvidenceRef(
                    f"phase:{pid}", "committed_manifest", f"docs/project-roadmap.json ({commits})"
                )
            )
        else:
            warnings.append(f"phase {pid} is not complete")
    for did in topic["decisions"]:
        required += 1
        entry = sources.decisions.get(did)
        if entry:
            found.append(EvidenceRef(f"decision:{did}", "operations_doc", entry["file"]))
        else:
            warnings.append(f"decision {did} is not in the decision log")
    for name in topic["facts"]:
        required += 1
        fact = sources.fact(name)
        if fact and fact.get("value") is not None:
            found.append(
                EvidenceRef(
                    f"fact:{name}",
                    fact.get("authority", "runtime_report"),
                    "reports/project_state_latest.json",
                    observed_at,
                )
            )
        else:
            warnings.append(f"fact {name} is not in the project-state report")
    for path, phrase in topic["docs"]:
        required += 1
        text = root_docs.get(path)
        if text is not None and _compact(phrase) in _compact(text):
            found.append(EvidenceRef(f"doc:{path}", "operations_doc", path))
        else:
            warnings.append(f"{path} no longer says: {phrase[:30]}")
    if topic.get("diagnostic"):
        required += 1
        diag = sources.diagnostic
        if diag:
            found.append(
                EvidenceRef(
                    "report:threads_performance_diagnostic",
                    "runtime_record",
                    "reports/threads_performance_diagnostic_latest.json",
                    diag.get("generated_at"),
                )
            )
        else:
            warnings.append("no Threads performance diagnostic report")
    return found, required, warnings


def candidate_id(topic_key: str) -> str:
    return "note-" + hashlib.sha256(topic_key.encode()).hexdigest()[:10]


def score(topic: dict, candidate: NoteCandidate, sources: SourceBundle, used: set[str]) -> dict:
    completed = sources.completed_phases
    recent = set(completed[-3:])
    done = [p for p in topic["phases"] if p in completed]
    parts = {
        "usefulness": USEFULNESS[topic["type"]],
        "novelty": 0 if set(candidate.source_event_ids) & used else 3,
        "evidence": int(3 * candidate.evidence_found / candidate.evidence_required)
        if candidate.evidence_required
        else 0,
        "significance": min(3, len(done)),
        "readability": topic["readability"],
        "recency": 2 if recent & set(done) else 1,
        "duplication_penalty": PENALTY[topic["duplication_risk"]],
    }
    parts["total"] = (
        sum(v for k, v in parts.items() if k != "duplication_penalty")
        - parts["duplication_penalty"]
    )
    return parts


def discover(sources: SourceBundle, *, used_source_events: Iterable[str] = (), topics=TOPICS):
    """候補を採点の高い順 (同点は id 順) に返す。"""

    used = set(used_source_events)
    root_docs = sources.docs
    out, claimed = [], set()
    for topic in topics:
        found, required, warnings = _evidence(topic, sources, root_docs)
        if not required or len(found) / required < MIN_COMPLETENESS:
            continue
        event_id = f"topic:{topic['key']}"
        if event_id in claimed:
            continue
        claimed.add(event_id)
        candidate = NoteCandidate(
            id=candidate_id(topic["key"]),
            content_type=topic["type"],
            working_title=topic["title"],
            premise=topic["premise"],
            source_event_ids=[event_id],
            phases=list(topic["phases"]),
            evidence=found,
            evidence_required=required,
            evidence_found=len(found),
            why_useful=topic["why"],
            duplication_risk=topic["duplication_risk"],
            warnings=warnings,
        )
        candidate.score = score(topic, candidate, sources, used)
        out.append(candidate)
    return sorted(out, key=lambda c: (-c.score["total"], c.id))


def topic_for(candidate: NoteCandidate, topics=TOPICS) -> dict:
    key = candidate.source_event_ids[0].removeprefix("topic:")
    return next(t for t in topics if t["key"] == key)


def doc_paths(topics=TOPICS) -> tuple[str, ...]:
    return tuple(sorted({path for t in topics for path, _ in t["docs"]}))
