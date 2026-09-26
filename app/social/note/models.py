"""note の候補と下書きの形 (pure)。

- 主張 (``Claim``) は種類を持つ: 観測した事実・実装の決定・仮説・解釈・予定。観測した事実と
  決定は、出どころ (``EvidenceRef``) が 1 つ以上ないと下書きに入れられない。
- 状態は小さく: ``draft`` → ``review_ready`` → ``approved`` → ``published`` /
  ``rejected``。``published`` は公開の証拠 (公開 URL と確認した時刻) が無ければ付けられない。
  ``approved`` は人の承認の記録 (誰が・いつ・何の hash) が無ければ付けられない。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

GENERATOR_VERSION = "note-foundation/1"

CONTENT_TYPES = ("build_log", "decision_note", "experiment_result", "milestone_recap")
CLAIM_KINDS = ("observed_fact", "decision", "hypothesis", "interpretation", "planned")
SOURCED_KINDS = ("observed_fact", "decision")
STATUSES = ("draft", "review_ready", "approved", "published", "rejected")
TRANSITIONS = {
    "draft": ("review_ready", "rejected"),
    "review_ready": ("approved", "rejected", "draft"),
    "approved": ("published", "rejected", "draft"),
    "published": (),
    "rejected": (),
}


class NoteStatusError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceRef:
    """主張の出どころ。``authority`` は T7 の出どころの強さ (``precedence``) の名前。"""

    source: str
    authority: str
    locator: str
    observed_at: str | None = None


@dataclass(frozen=True)
class Claim:
    text: str
    kind: str
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in CLAIM_KINDS:
            raise ValueError(f"unknown claim kind {self.kind!r}")


@dataclass
class NoteCandidate:
    id: str
    content_type: str
    working_title: str
    premise: str
    source_event_ids: list[str]
    phases: list[str]
    evidence: list[EvidenceRef]
    evidence_required: int
    evidence_found: int
    why_useful: str
    duplication_risk: str
    score: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def evidence_completeness(self) -> float:
        return (
            round(self.evidence_found / self.evidence_required, 2)
            if self.evidence_required
            else 0.0
        )

    def as_dict(self) -> dict:
        data = asdict(self)
        data["evidence_completeness"] = self.evidence_completeness
        return data


@dataclass
class NoteDraft:
    id: str
    candidate_id: str
    content_type: str
    working_title: str
    premise: str
    audience: str
    summary: str
    sections: list[dict]
    claims: list[Claim]
    phases: list[str]
    source_event_ids: list[str]
    wordpress_refs: list[str]
    threads_refs: list[str]
    created_at: str
    status: str = "draft"
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    human_review_required: bool = True
    approval: dict | None = None
    publication: dict | None = None
    generator_version: str = GENERATOR_VERSION

    @property
    def body(self) -> str:
        parts = []
        for section in self.sections:
            parts.append(f"## {section['heading']}")
            parts.extend(section["paragraphs"])
        return "\n\n".join(parts)

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            {"title": self.working_title, "body": self.body}, ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        data = asdict(self)
        data["body"] = self.body
        data["content_hash"] = self.content_hash
        return data


def transition(draft: NoteDraft, target: str, *, approval=None, publication=None) -> NoteDraft:
    """状態を 1 つ進める。人の承認・公開の証拠が無い遷移は拒む。"""

    if target not in STATUSES:
        raise NoteStatusError(f"unknown status {target!r}")
    if target not in TRANSITIONS[draft.status]:
        raise NoteStatusError(f"{draft.status} -> {target} is not allowed")
    if target == "review_ready" and draft.errors:
        raise NoteStatusError(f"draft has errors: {draft.errors}")
    if target == "approved":
        needed = {"approved_by", "approved_at", "content_hash"}
        if not approval or not needed <= set(approval):
            raise NoteStatusError("approval needs approved_by, approved_at and content_hash")
        if approval["content_hash"] != draft.content_hash:
            raise NoteStatusError("approval is for a different content hash")
        draft.approval = dict(approval)
    if target == "published":
        needed = {"url", "observed_at"}
        if not publication or not needed <= set(publication) or not publication["url"]:
            raise NoteStatusError("published needs publication evidence (url, observed_at)")
        draft.publication = dict(publication)
    draft.status = target
    return draft
