"""note の 1 本を人と仕上げて、承認し、公開の証拠を記録する (N1、pure)。

公開そのものは人が note の編集画面で行う。ここは次のことだけを扱う:

- 下書きの読み書き (``draft_from_dict``)
- 人が直した Markdown の取り込み (``apply_edit``。承認は消え、検査をやり直す)
- 確認用のまとめ (``review_packet`` / ``render_packet`` / ``plain_text``)
- 承認 (``approve``。本文の hash に結びつく。リンク・タグ・公開の形・サムネイルの画像の
  ファイル (名前・大きさ・sha256) も承認に入る。効かなくなった承認は ``approval_history``)
- 公開の記録 (``record_publication``。承認した hash と同じ本文を人が公開したときだけ。URL は
  https で、方針の host に限る)

有料 (``access_mode == "paid"``) は記録するだけ。値段を決める・販売を始めるのは人が note で行う。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
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
               corpus: dict | None = None, editor: str = "human",
               now: datetime | None = None) -> NoteDraft:  # fmt: skip
    """直した本文を取り込む。``editor`` は誰が直したか (偽らない。Claude の手直しは claude)。"""

    if editor not in EDITORS:
        raise ValueError(f"editor must be one of {EDITORS}")
    if draft.status in ("published", "rejected"):
        raise NoteStatusError(f"a {draft.status} draft cannot be edited")
    title, sections = parse_markdown(markdown)
    draft.working_title, draft.sections = title, sections
    draft.edited_by = editor
    draft.edited_by_human = draft.edited_by_human or editor == "human"
    # 本文が変われば承認は効かない (前の承認は履歴に残す)
    _archive_approval(draft, "the body was edited", now or datetime.now(UTC))
    draft.status = "draft"
    recheck(draft, commissions_known=commissions_known, corpus=corpus)
    return draft


def reopen(draft: NoteDraft, *, reason: str, now: datetime) -> NoteDraft:
    """承認を取り消して下書きへ戻す (画像を差し替える等)。前の承認は履歴に残る。"""

    if draft.status not in ("approved", "review_ready"):
        raise NoteStatusError(f"only an approved or review_ready draft can be reopened "
                              f"(it is {draft.status})")  # fmt: skip
    if not reason.strip():
        raise NoteStatusError("a reason is required")
    _archive_approval(draft, reason.strip()[:300], now)
    draft.status = "draft"
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


def add_evidence(draft: NoteDraft, *, text: str, kind: str, decision_ids: list[str],
                 decisions: dict[str, dict], docs: list[tuple[str, str]] | None = None,
                 root: Path | None = None) -> NoteDraft:  # fmt: skip
    """本文の事実の文を裏付ける根拠を足す。本文と hash は変えない。

    根拠は決定の記録 (``decision_ids``) か、運用のドキュメントの言い回し (``docs``:
    (リポジトリの中の相対パス, 今もその文書にある言い回し))。どちらも今あることを確かめる。
    """

    if draft.status in ("approved", "published", "rejected"):
        raise NoteStatusError(f"the evidence of a {draft.status} draft cannot change")
    docs = list(docs or [])
    missing = [d for d in decision_ids if d not in decisions]
    if (not decision_ids and not docs) or missing:
        raise ValueError(f"decisions not in the decision log: {missing or 'none given'}")
    refs = [EvidenceRef(f"decision:{d}", "operations_doc", decisions[d]["file"])
            for d in decision_ids]  # fmt: skip
    for rel, phrase in docs:
        path = (root or Path(".")) / rel
        if not path.is_file() or not phrase or phrase not in path.read_text(encoding="utf-8"):
            raise ValueError(f"{rel} does not contain the phrase {phrase!r}")
        refs.append(EvidenceRef(f"doc:{rel}", "operations_doc", f"{rel} ({phrase[:60]})"))
    draft.claims.append(Claim(text.strip(), kind, tuple(refs)))
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


# -- サムネイルの画像 -------------------------------------------------------------------------
#: 中身の先頭のバイトから分かる形式だけを受け付ける (拡張子は信じない)。
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
APPROVAL_SCHEMA = "note-approval/2"


def _mime_of(data: bytes) -> str | None:
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def inspect_image(path: Path | str) -> dict:
    """承認・公開に使う画像の指紋 (名前・大きさ・sha256・中身から分かる形式)。

    無い・読めない・空・形式が分からない、はすべて拒む (fail closed)。記録するのは
    ファイルの名前だけで、手元のパスは記録しない。
    """

    file = Path(path)
    if not file.is_file():
        raise NoteStatusError(f"image file is missing: {file.name}")
    try:
        data = file.read_bytes()
    except OSError as exc:
        raise NoteStatusError(f"image file is unreadable: {file.name} "
                              f"({type(exc).__name__})") from exc  # fmt: skip
    if not data:
        raise NoteStatusError(f"image file is empty: {file.name}")
    mime = _mime_of(data)
    if mime is None:
        raise NoteStatusError(f"unsupported image type: {file.name} (PNG / JPEG / GIF / WebP)")
    return {"filename": file.name, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "mime_type": mime}  # fmt: skip


def image_required(draft: NoteDraft) -> bool:
    """サムネイルの指示がある下書きは、公開に使う画像そのものを承認に含める。"""

    return bool(draft.thumbnail_brief)


def approved_images(draft: NoteDraft) -> list[dict]:
    """承認に拘束された画像 (古い承認には無い。補わない)。"""

    return list((draft.approval or {}).get("images") or [])


def check_image(draft: NoteDraft, path: Path | str) -> dict:
    """手元の画像が、承認したときの画像と同じか (大きさと sha256)。名前が同じでも中身が
    変われば同じではない。"""

    approved = approved_images(draft)
    current = inspect_image(path)
    if not approved:
        return {"state": "no_image_bound", "current": current,
                "note": "the current approval binds no image"}  # fmt: skip
    same = any(a["sha256"] == current["sha256"] and a["bytes"] == current["bytes"]
               for a in approved)  # fmt: skip
    return {"state": "matches_approval" if same else "changed_since_approval",
            "current": current, "approved": approved,
            "note": ("this is the approved image" if same else
                     "the image differs from the approved one: reopen and approve again")}


def _archive_approval(draft: NoteDraft, reason: str, now: datetime) -> None:
    """効かなくなった承認を消さずに残す (過去の承認の意味は書き換えない)。"""

    if draft.approval:
        draft.approval_history.append({**draft.approval, "superseded_at":
                                       now.isoformat(timespec="seconds"),
                                       "superseded_reason": reason})  # fmt: skip
    draft.approval = None


def review_packet(draft: NoteDraft) -> dict:
    links = links_of(draft)
    return {
        "draft_id": draft.id, "status": draft.status, "title": draft.working_title,
        "access_mode": draft.access_mode, "content_hash": draft.content_hash,
        "links": links, "links_hash": _links_hash(links),
        "image_required": image_required(draft),
        "images": [{k: i.get(k) for k in ("filename", "bytes", "sha256", "mime_type")}
                   for i in approved_images(draft)],
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
             f"- links ({len(packet['links'])}): " + (", ".join(packet["links"]) or "none")]
    if packet["images"]:
        lines += [f"- approved image: {i['filename']} · {i['bytes']} bytes · {i['mime_type']} · "
                  f"sha256 {i['sha256']}" for i in packet["images"]]  # fmt: skip
    elif packet["image_required"]:
        lines.append("- image: **required** (the thumbnail brief is set) — not bound yet; the "
                     "human approves with the final image file (`approve ... --image <file>`)")
    else:
        lines.append("- images: none")
    lines += ["- the system binds the local image file only; the human checks that the image "
              "uploaded to note is the same one"] if packet["image_required"] else []  # fmt: skip
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
            links_approved: bool = False, image: Path | str | None = None,
            note: str | None = None,
            after_publication_at: datetime | None = None) -> NoteDraft:  # fmt: skip
    """人の承認を記録する (``note-approval/2``)。

    承認に入るもの (それぞれ別に追える): 本文 (``content_hash``。本文と題名だけ、今までと同じ)・
    外部リンク (一覧と hash。``links_approved`` が要る)・タグ・公開の形・サムネイルの指示・
    **画像** (``image`` のファイルの名前・大きさ・sha256・形式)。サムネイルの指示がある下書きは、
    画像のファイルを渡さないと承認できない (真偽値だけの「画像も承認」は作らない)。
    """

    if draft.errors:
        raise NoteStatusError(f"draft has errors: {draft.errors}")
    links = links_of(draft)
    if links and not links_approved:
        raise NoteStatusError(f"{len(links)} external link(s) need explicit approval")
    if not approved_by.strip():
        raise NoteStatusError("approved_by is required")
    images = [inspect_image(image)] if image is not None else []
    if image_required(draft) and not images:
        raise NoteStatusError("this draft uses a thumbnail: approve it with the final image "
                              "file (--image <file>)")  # fmt: skip
    extra: dict = {}
    if note and note.strip():
        extra["approval_note"] = note.strip()[:500]
    if after_publication_at is not None:
        # 公開の後に、公開に使ったものを確かめて承認した (事後の承認)。承認の時刻は実際の時刻
        # (``approved_at``) のまま。公開の時刻は人が伝えた note の表示の時刻。
        if after_publication_at.tzinfo is None:
            raise NoteStatusError("the publication time needs a timezone (e.g. +09:00)")
        if after_publication_at > now:
            raise NoteStatusError("the reported publication time is in the future")
        extra["retrospective"] = {
            "approved_after_publication": True,
            "note_published_at": after_publication_at.isoformat(timespec="minutes"),
            "note_published_at_source": "reported by the human (note display)"}
    return transition(draft, "approved", approval={**extra,
        "approval_schema": APPROVAL_SCHEMA,
        "approved_by": approved_by.strip(), "approved_at": now.isoformat(timespec="seconds"),
        "content_hash": content_hash, "access_mode": draft.access_mode,
        "links": links, "links_hash": _links_hash(links), "links_approved": bool(links),
        "tags": list(draft.tags), "thumbnail_brief": draft.thumbnail_brief,
        "edited_by": draft.edited_by,
        "image_required": image_required(draft),
        "images_approved": bool(images), "images": images})  # fmt: skip


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


NOTE_IMAGE_MATCH = ("human responsibility: the system binds the approved local image file; "
                    "it cannot check the image uploaded to note")


def record_publication(draft: NoteDraft, *, url: str, observed_at: datetime,
                       published_hash: str, now: datetime, policy: dict,
                       image: Path | str | None = None) -> NoteDraft:  # fmt: skip
    """人が note で公開した後に、その証拠を記録する。承認した本文と違えば記録しない。

    承認に画像が拘束されていれば、公開に使った手元の画像 (``image``) が承認の画像と同じで
    ないと記録しない (名前が同じでも中身が変われば別物)。承認した画像の指紋は公開の記録に
    引き継ぐ。note に上がった画像との一致は、仕組みでは確かめられないので人の責任。
    """

    if draft.status != "approved" or not draft.approval:
        raise NoteStatusError("only an approved draft can be recorded as published")
    if published_hash != draft.approval["content_hash"] or published_hash != draft.content_hash:
        raise NoteStatusError("the published body is not the approved version (hash mismatch); "
                              "edit, re-submit and re-approve first")  # fmt: skip
    if draft.approval.get("access_mode", "free") != draft.access_mode:
        raise NoteStatusError("the access mode changed after approval")
    bound = approved_images(draft)
    if bound:
        if image is None:
            raise NoteStatusError("the approval binds an image: pass the image file used for "
                                  "the publication (--image <file>)")  # fmt: skip
        state = check_image(draft, image)
        if state["state"] != "matches_approval":
            current = state["current"]
            raise NoteStatusError(f"the image is not the approved one ({current['filename']}, "
                                  f"sha256 {current['sha256'][:12]}); reopen and approve the "
                                  "new image first")  # fmt: skip
    return transition(draft, "published", publication={
        "url": check_publication_url(url, policy),
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "recorded_at": now.isoformat(timespec="seconds"),
        "content_hash": published_hash, "access_mode": draft.access_mode,
        "published_by": "human (note editor)",
        "approved_images": [{k: i.get(k) for k in ("filename", "bytes", "sha256", "mime_type")}
                            for i in bound],
        "note_image_match": NOTE_IMAGE_MATCH if bound else
        "no image was bound at approval"})  # fmt: skip


def reject(draft: NoteDraft, *, reason: str, now: datetime) -> NoteDraft:
    if not reason.strip():
        raise NoteStatusError("a reason is required")
    transition(draft, "rejected")
    draft.warnings = sorted(set(draft.warnings) | {
        f"rejected at {now.isoformat(timespec='seconds')}: {reason.strip()[:200]}"})
    return draft


__all__ = ["APPROVAL_SCHEMA", "NOTE_IMAGE_MATCH", "add_evidence", "apply_edit", "approve",
           "approved_images", "check_image", "check_publication_url", "draft_from_dict",
           "image_required", "inspect_image", "reopen",
           "links_of", "load_policy", "parse_markdown", "plain_text", "record_publication",
           "recheck", "reject", "render_packet", "review_packet", "set_access_mode", "set_meta",
           "submit"]
