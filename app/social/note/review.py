"""note の 1 本を人と仕上げて、承認し、公開の証拠を記録する (N1、pure)。

公開そのものは人が note の編集画面で行う。ここは次のことだけを扱う:

- 下書きの読み書き (``draft_from_dict``)
- 人が直した Markdown の取り込み (``apply_edit``。承認は消え、検査をやり直す)
- 確認用のまとめ (``review_packet`` / ``render_packet`` / ``plain_text``)
- 承認 (``approve``。本文の hash に結びつく。リンク・画像・公開の形も承認に入る)
- 公開の記録 (``record_publication``。承認した hash と同じ本文を人が公開したときだけ。URL は
  https で、方針の host に限る)

有料 (``access_mode == "paid"``) は記録するだけ。値段を決める・販売を始めるのは人が note で行う。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from app.social.note import safety
from app.social.note.models import (
    ACCESS_MODES,
    Claim,
    EvidenceRef,
    NoteDraft,
    NoteStatusError,
    transition,
)

POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "note_channel_policy.json"
_URL = re.compile(r"https?://[^\s)>\]」』]+")


def load_policy(path: Path | None = None) -> dict:
    return json.loads((path or POLICY_PATH).read_text(encoding="utf-8"))


# -- 読み書き ---------------------------------------------------------------------------------
def draft_from_dict(data: dict) -> NoteDraft:
    fields = {k: v for k, v in data.items() if k not in ("body", "content_hash")}
    fields["claims"] = [
        Claim(c["text"], c["kind"], tuple(EvidenceRef(**e) for e in c.get("evidence") or ()))
        for c in data.get("claims") or []
    ]
    known = set(NoteDraft.__dataclass_fields__)
    return NoteDraft(**{k: v for k, v in fields.items() if k in known})


def links_of(draft: NoteDraft) -> list[str]:
    """公開の前に人が承認する外部リンク (本文の URL と、下書きの WordPress の参照)。"""

    found = _URL.findall(draft.body)
    return sorted(set(found) | set(draft.wordpress_refs or []))


# -- 人の手直し ---------------------------------------------------------------------------------
def parse_markdown(text: str) -> tuple[str, list[dict]]:
    """``# 題名`` と ``## 見出し`` の Markdown を題名と節にする。根拠・検査の節から後は読まない。"""

    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    title, sections, current = None, [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip() == "---" or line.startswith("## 根拠") or line.startswith("## 検査"):
            break
        if line.startswith("# ") and title is None:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            current = {"heading": line[3:].strip(), "role": "edited", "paragraphs": []}
            sections.append(current)
            continue
        if not line.strip() or (line.startswith("_") and line.endswith("_") and title):
            continue
        if current is None:
            raise ValueError("body text before the first '## ' heading")
        current["paragraphs"].append(line.strip())
    if not title:
        raise ValueError("no '# ' title line")
    sections = [s for s in sections if s["paragraphs"]]
    if not sections:
        raise ValueError("no section with text")
    return title, sections


def recheck(draft: NoteDraft, *, commissions_known: bool, corpus: dict | None = None) -> None:
    """本文の検査をやり直す (人が直したあと)。"""

    errors, warnings = safety.check_body(draft.body, commissions_known=commissions_known)
    cleaned, found = safety.sanitize(draft.working_title + "\n" + draft.body)
    if found:
        errors.append(f"secret or internal values in the text: {sorted(set(found))}")
    _text, internal = safety.reader_facing(draft.body)
    if internal:
        warnings.append(f"internal wording in the body: {internal}")
    if draft.edited_by:
        warnings.append(f"the body was edited by {draft.edited_by}; the evidence list reflects "
                        "the generated version (re-check every factual sentence)")  # fmt: skip
    if corpus:
        dup = safety.duplication(draft.body, corpus)
        warnings.append(f"duplication {dup['verdict']} (max containment "
                        f"{dup['max_containment']} vs {dup['closest_source']})")  # fmt: skip
        if dup["verdict"] == "duplicate":
            errors.append(f"duplicates {dup['closest_source']}")
    draft.errors = errors
    draft.warnings = sorted(set(warnings))


EDITORS = ("human", "claude")


def apply_edit(draft: NoteDraft, markdown: str, *, commissions_known: bool,
               corpus: dict | None = None, editor: str = "human") -> NoteDraft:  # fmt: skip
    """直した本文を取り込む。``editor`` は誰が直したか (偽らない。Claude の手直しは claude)。"""

    if editor not in EDITORS:
        raise ValueError(f"editor must be one of {EDITORS}")
    if draft.status in ("published", "rejected"):
        raise NoteStatusError(f"a {draft.status} draft cannot be edited")
    title, sections = parse_markdown(markdown)
    draft.working_title, draft.sections = title, sections
    draft.edited_by = editor
    draft.edited_by_human = draft.edited_by_human or editor == "human"
    draft.status, draft.approval = "draft", None  # 本文が変われば承認は効かない
    recheck(draft, commissions_known=commissions_known, corpus=corpus)
    return draft


def set_meta(draft: NoteDraft, *, tags: list[str] | None = None,
             thumbnail_brief: str | None = None) -> NoteDraft:  # fmt: skip
    """推奨のタグとサムネイルの指示。承認の前だけ変えられる (承認に入る)。"""

    if draft.status in ("approved", "published", "rejected"):
        raise NoteStatusError(f"the meta of a {draft.status} draft cannot change")
    if tags is not None:
        clean = [t.strip().lstrip("#") for t in tags if t.strip().lstrip("#")]
        if len(clean) > 10 or any(len(t) > 30 or " " in t for t in clean):
            raise ValueError("up to 10 tags, each without spaces and at most 30 characters")
        draft.tags = clean
    if thumbnail_brief is not None:
        _cleaned, found = safety.sanitize(thumbnail_brief)
        if found:
            raise ValueError(f"the thumbnail brief contains internal values {found}")
        draft.thumbnail_brief = thumbnail_brief.strip()[:600] or None
    return draft


def set_access_mode(draft: NoteDraft, mode: str) -> NoteDraft:
    if mode not in ACCESS_MODES:
        raise ValueError(f"access mode must be one of {ACCESS_MODES}")
    if draft.status in ("approved", "published", "rejected"):
        raise NoteStatusError(f"the access mode of a {draft.status} draft cannot change")
    draft.access_mode = mode
    return draft


# -- 確認用のまとめ -----------------------------------------------------------------------------
def plain_text(draft: NoteDraft) -> str:
    """note の編集画面に貼る形 (記号を使わない。見出しは行だけ)。"""

    parts = [draft.working_title, ""]
    for section in draft.sections:
        parts += [section["heading"], "", *[p + "\n" for p in section["paragraphs"]]]
    return "\n".join(parts).rstrip() + "\n"


def _links_hash(links: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(links)).encode("utf-8")).hexdigest()


def review_packet(draft: NoteDraft) -> dict:
    links = links_of(draft)
    return {
        "draft_id": draft.id, "status": draft.status, "title": draft.working_title,
        "access_mode": draft.access_mode, "content_hash": draft.content_hash,
        "links": links, "links_hash": _links_hash(links), "images": [],
        "errors": list(draft.errors), "warnings": list(draft.warnings),
        "edited_by_human": draft.edited_by_human, "edited_by": draft.edited_by,
        "headings": [s["heading"] for s in draft.sections],
        "tags": list(draft.tags), "thumbnail_brief": draft.thumbnail_brief,
        "human_approves": ["final title", "final body (content_hash)", "external links",
                           "images / thumbnail", "tags", "access mode",
                           "the act of publishing (in note)"],
        "can_submit": draft.status == "draft" and not draft.errors,
        "can_approve": draft.status == "review_ready" and not draft.errors,
    }  # fmt: skip


def render_packet(draft: NoteDraft) -> str:
    packet = review_packet(draft)
    lines = ["<!-- REVIEW PACKET — nothing here is published. The human publishes in note. -->",
             "", f"# {draft.working_title}", "", draft.body, "", "---", "",
             "## 確認すること (公開の前に消す)", "",
             f"- status: {draft.status} · access mode: {draft.access_mode}",
             f"- content_hash: {draft.content_hash}",
             f"- edited by: {draft.edited_by or 'generated (not edited)'}",
             "- headings: " + " / ".join(packet["headings"]),
             "- recommended tags: " + (" ".join(f"#{t}" for t in draft.tags) or "none"),
             f"- thumbnail brief: {draft.thumbnail_brief or 'none'}",
             f"- links ({len(packet['links'])}): " + (", ".join(packet["links"]) or "none"),
             "- images: none"]  # fmt: skip
    lines += [f"- error: {e}" for e in draft.errors] or ["- errors: none"]
    lines += [f"- warning: {w}" for w in draft.warnings]
    lines += ["", "## 根拠 (事実の文を確かめるため。公開の前に消す)", ""]
    for claim in draft.claims:
        refs = ", ".join(f"{e.source} ({e.locator})" for e in claim.evidence) or "—"
        lines.append(f"- [{claim.kind}] {claim.text} — {refs}")
    lines += ["", "人が承認するもの: " + "・".join(packet["human_approves"])]
    return "\n".join(lines) + "\n"


# -- 承認と公開の記録 ---------------------------------------------------------------------------
def submit(draft: NoteDraft) -> NoteDraft:
    return transition(draft, "review_ready")


def approve(draft: NoteDraft, *, content_hash: str, approved_by: str, now: datetime,
            links_approved: bool = False, images_approved: bool = False) -> NoteDraft:  # fmt: skip
    if draft.errors:
        raise NoteStatusError(f"draft has errors: {draft.errors}")
    links = links_of(draft)
    if links and not links_approved:
        raise NoteStatusError(f"{len(links)} external link(s) need explicit approval")
    if not approved_by.strip():
        raise NoteStatusError("approved_by is required")
    return transition(draft, "approved", approval={
        "approved_by": approved_by.strip(), "approved_at": now.isoformat(timespec="seconds"),
        "content_hash": content_hash, "access_mode": draft.access_mode,
        "links": links, "links_hash": _links_hash(links),
        "tags": list(draft.tags), "thumbnail_brief": draft.thumbnail_brief,
        "edited_by": draft.edited_by,
        "images_approved": images_approved, "images": []})  # fmt: skip


def check_publication_url(url: str, policy: dict) -> str:
    parsed = urlparse(url.strip())
    hosts = {h.lower() for h in policy.get("publication_url_hosts") or []}
    if parsed.scheme != "https":
        raise NoteStatusError("the publication URL must be https")
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in hosts):
        raise NoteStatusError(f"host {host!r} is not in publication_url_hosts {sorted(hosts)}")
    if parsed.username or parsed.password or not parsed.path.strip("/"):
        raise NoteStatusError("the publication URL must point to a piece (no credentials)")
    return url.strip()


def record_publication(draft: NoteDraft, *, url: str, observed_at: datetime,
                       published_hash: str, now: datetime, policy: dict) -> NoteDraft:  # fmt: skip
    """人が note で公開した後に、その証拠を記録する。承認した本文と違えば記録しない。"""

    if draft.status != "approved" or not draft.approval:
        raise NoteStatusError("only an approved draft can be recorded as published")
    if published_hash != draft.approval["content_hash"] or published_hash != draft.content_hash:
        raise NoteStatusError("the published body is not the approved version (hash mismatch); "
                              "edit, re-submit and re-approve first")  # fmt: skip
    if draft.approval.get("access_mode", "free") != draft.access_mode:
        raise NoteStatusError("the access mode changed after approval")
    return transition(draft, "published", publication={
        "url": check_publication_url(url, policy),
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "recorded_at": now.isoformat(timespec="seconds"),
        "content_hash": published_hash, "access_mode": draft.access_mode,
        "published_by": "human (note editor)"})  # fmt: skip


def reject(draft: NoteDraft, *, reason: str, now: datetime) -> NoteDraft:
    if not reason.strip():
        raise NoteStatusError("a reason is required")
    transition(draft, "rejected")
    draft.warnings = sorted(set(draft.warnings) | {
        f"rejected at {now.isoformat(timespec='seconds')}: {reason.strip()[:200]}"})
    return draft


__all__ = ["apply_edit", "approve", "check_publication_url", "draft_from_dict", "links_of",
           "load_policy", "parse_markdown", "plain_text", "record_publication", "recheck",
           "reject", "render_packet", "review_packet", "set_access_mode", "set_meta", "submit"]
