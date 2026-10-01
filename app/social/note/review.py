"""note の 1 本を人と仕上げて、承認し、公開の証拠を記録する (N1、pure)。

公開そのものは人が note の編集画面で行う。ここは次のことだけを扱う:

- 下書きの読み書き (``draft_from_dict``)
- 人が直した Markdown の取り込み (``apply_edit``。承認は消え、検査をやり直す)
- 確認用のまとめ (``review_packet`` / ``render_packet`` / ``plain_text``)
- 承認 (``approve``。本文の hash に結びつく。リンク・タグ・公開の形・サムネイルの画像の
  ファイル (名前・大きさ・sha256) も承認に入る。効かなくなった承認は ``approval_history``)
- 承認した画像の写し (``snapshot_image``、``note-approval/3``。承認の時に、画像を中身の sha256 の
  名前で ``reports/note/approved-images/<下書き>/`` へ写す。上書きしない・消さない。承認の後の
  正本はこの写しで、元のファイル (``artifacts/`` など) は出どころの記録だけ)
- 公開の記録 (``record_publication``。承認した hash と同じ本文を人が公開したときだけ。URL は
  https で、方針の host に限る。画像は承認の写しで確かめる)

有料 (``access_mode == "paid"``) は記録するだけ。値段を決める・販売を始めるのは人が note で行う。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from app.social.note import paid, safety
from app.social.note.models import (
    ACCESS_MODES,
    TRANSITIONS,
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
    errors += [f"paid: {p}" for p in paid.problems(draft)]
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
    if paid.is_paid(draft):
        raise NoteStatusError("a paid draft is edited by re-importing its free and paid parts "
                              "(import-paid --replace), so the paid boundary stays explicit")
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


def _refresh_paid_errors(draft: NoteDraft) -> None:
    draft.errors = [e for e in draft.errors if not e.startswith("paid: ")]
    draft.errors += [f"paid: {p}" for p in paid.problems(draft)]


def set_paid_terms(draft: NoteDraft, *, root: Path | str | None = None,
                   product_id: str | None = None, amount: int | None = None,
                   currency: str | None = None,
                   paid_from_section: int | None = None) -> NoteDraft:  # fmt: skip
    """有料の記事の販売の条件を決める (承認の前だけ。承認の後は取り消してから)。

    売る物は ``root`` の今の release candidate から完全な hash を読む (手で hash を書かない)。
    """

    if draft.status in ("approved", "published", "rejected"):
        raise NoteStatusError(f"the commercial terms of a {draft.status} draft cannot change "
                              "(reopen first)")  # fmt: skip
    if not paid.is_paid(draft):
        raise NoteStatusError("set the access mode to paid first")
    if amount is not None or currency is not None:
        draft.price = paid.check_price(amount, currency)
    if product_id is not None:
        if root is None:
            raise NoteStatusError("binding a product needs the repository root")
        draft.product = paid.bind_product(root, product_id)
    if paid_from_section is not None:
        draft.paid_from_section = paid_from_section
    _refresh_paid_errors(draft)
    return draft


def sections_from_markdown(text: str) -> list[dict]:
    """``## 見出し`` ごとの節にする (有料の記事の取り込み用)。コードの枠は 1 つの段落に、
    箇条書きの続きの行 (字下げ) は前の項目にまとめる。"""

    sections, current, fence = [], None, None
    for raw in text.splitlines():
        line = raw.rstrip()
        if fence is not None:
            fence.append(line)
            if line.startswith("```"):
                current["paragraphs"].append("\n".join(fence))
                fence = None
            continue
        if line.startswith("## "):
            current = {"heading": line[3:].strip(), "role": "imported", "paragraphs": []}
            sections.append(current)
            continue
        if not line.strip():
            continue
        if current is None:
            raise ValueError("text before the first '## ' heading")
        if line.startswith("```"):
            fence = [line]
        elif (raw.startswith("  ") and current["paragraphs"]
              and current["paragraphs"][-1].startswith("- ")):
            prev, nxt = current["paragraphs"][-1], line.strip()
            # 日本語の途中で折り返した行は空白なしでつなぐ (英数字の間だけ空白)
            glue = "" if (not prev[-1].isascii() and not nxt[0].isascii()) else " "
            current["paragraphs"][-1] = prev + glue + nxt
        else:
            current["paragraphs"].append(line.strip())
    if fence is not None:
        raise ValueError("a code block is not closed")
    return [s for s in sections if s["paragraphs"]]


PAID_CONTENT_TYPE = "paid_product_guide"


def paid_draft_id(product_id: str, version: str) -> str:
    return "draft-" + hashlib.sha256(f"paid:{product_id}@{version}".encode()).hexdigest()[:10]


def import_paid_draft(*, title: str, free_markdown: str, paid_markdown: str,
                      root: Path | str, product_id: str, amount: int, currency: str,
                      now: datetime, existing: NoteDraft | None = None) -> NoteDraft:  # fmt: skip
    """有料の記事の下書きを、無料の部分と有料の部分から作る (または作り直す)。

    境界は「無料の部分の節の後」。売る物は ``root`` の今の release candidate から読む。作り直す
    ときは前の承認を履歴に残し、``draft`` に戻す。承認はしない。
    """

    from app.products.spec import load_product

    free_sections = sections_from_markdown(free_markdown)
    paid_sections = sections_from_markdown(paid_markdown)
    if not free_sections or not paid_sections:
        raise NoteStatusError("a paid article needs a free part and a paid part")
    for section in free_sections:
        section["role"] = "free"
    for section in paid_sections:
        section["role"] = "paid"
    binding = paid.bind_product(root, product_id)
    price = paid.check_price(amount, currency)
    spec = load_product(Path(root), product_id).spec
    evidence = EvidenceRef(f"product:{product_id}@{binding['version']}", "repository",
                           f"products/{product_id}/product.json (manifest "
                           f"{binding['manifest_hash'][:12]})")  # fmt: skip
    claim = Claim("有料の部分は、売る product の版の資産 (手引き・チェックリスト・設定の例・利用の"
                  "条件) を含む", "decision", (evidence,))  # fmt: skip
    if existing is not None:
        if existing.status in ("published", "rejected"):
            raise NoteStatusError(f"a {existing.status} draft cannot be re-imported")
        _archive_approval(existing, "re-imported (content or commercial terms)", now)
        draft = existing
        draft.status = "draft"
    else:
        draft = NoteDraft(
            id=paid_draft_id(product_id, binding["version"]),
            candidate_id=f"product:{product_id}", content_type=PAID_CONTENT_TYPE,
            working_title=title.strip(), premise=spec["problem"], audience=spec["target_user"],
            summary=spec["intended_outcome"], sections=[], claims=[], phases=["N4", "N5"],
            source_event_ids=[f"product:{product_id}@{binding['version']}"], wordpress_refs=[],
            threads_refs=[], created_at=now.isoformat(timespec="seconds"))  # fmt: skip
    draft.working_title = title.strip()
    draft.sections = free_sections + paid_sections
    draft.claims = [claim]
    draft.access_mode = "paid"
    draft.paid_from_section = len(free_sections)
    draft.price, draft.product = price, binding
    draft.edited_by = "claude"
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


#: 本文の箇条書きの印。本文の文字として ``content_hash`` に入る (承認の対象は今までと同じ)。
#: 表示 (``display_blocks`` / ``paste_html``) では印を外して箇条書きにする (2026-10-01)。
LIST_MARK = "・"


def body_lines(draft: NoteDraft) -> list[str]:
    """承認した本文の行 (見出しと段落。空の行なし)。公開ページの照合の基準。"""

    lines = []
    for section in draft.sections:
        lines.append(section["heading"].strip())
        lines += [p.strip() for p in section["paragraphs"] if p.strip()]
    return lines


def display_blocks(draft: NoteDraft) -> list[dict]:
    """note に貼るときの表示の形: 見出し・段落・箇条書き。本文の文字と hash は変えない。

    「・」で始まる段落が続けば 1 つの箇条書きにまとめ、表示では印を外す。
    """

    blocks: list[dict] = []
    for index, section in enumerate(draft.sections):
        if paid.is_paid(draft) and index == draft.paid_from_section:
            blocks.append({"type": "paid_line"})  # 表示だけ (本文の文字ではない)
        blocks.append({"type": "heading", "text": section["heading"].strip()})
        for raw in section["paragraphs"]:
            text = raw.strip()
            if not text:
                continue
            mark = next((m for m in (LIST_MARK, "- [ ] ", "- ") if text.startswith(m)
                         and text[len(m):].strip()), None)  # fmt: skip
            if text.startswith("```"):
                blocks.append({"type": "code", "text": text})
            elif text.startswith("### "):
                blocks.append({"type": "subheading", "text": text})
            elif mark is not None:
                item = text[len(mark):].strip()
                last = blocks[-1]
                if last["type"] == "list" and last.get("mark", LIST_MARK) == mark:
                    last["items"].append(item)
                else:
                    blocks.append({"type": "list", "items": [item]}
                                  if mark == LIST_MARK else
                                  {"type": "list", "mark": mark, "items": [item]})
            else:
                blocks.append({"type": "paragraph", "text": text})
    return blocks


def canonical_lines(blocks: list[dict]) -> list[str]:
    """表示の形を、承認した本文の行に戻す (箇条書きの項目には「・」を付け直す)。"""

    lines = []
    for block in blocks:
        if block["type"] == "paid_line":
            continue
        if block["type"] == "list":
            lines += [block.get("mark", LIST_MARK) + item for item in block["items"]]
        else:
            lines.append(block["text"])
    return lines


def compare_published(draft: NoteDraft, blocks: list[dict]) -> dict:
    """公開ページから読んだ形 (見出し・段落・ネイティブの箇条書き) が承認した本文と同じか。

    箇条書きの項目は「・」の印なしでも、印つきの本文の行と同じとみなす (表示の違いだけ)。
    """

    expected, got = body_lines(draft), canonical_lines(blocks)
    for index, (want, have) in enumerate(zip(expected, got, strict=False)):
        if want != have:
            return {"match": False, "line": index + 1, "expected": want, "published": have}
    if len(expected) != len(got):
        return {"match": False, "line": min(len(expected), len(got)) + 1,
                "expected_lines": len(expected), "published_lines": len(got)}  # fmt: skip
    return {"match": True, "lines": len(expected)}


def paste_html(draft: NoteDraft) -> str:
    """note の編集画面に貼る形 (HTML): 見出しは h2、箇条書きは ul / li。ブラウザで開いて本文を
    選んでコピーし、note に貼る (題名は note の題名の欄に別に入れる)。文字は承認した本文と同じ。"""

    import html

    parts = ["<!doctype html>", '<html lang="ja"><head><meta charset="utf-8">',
             f"<title>{html.escape(draft.working_title)}</title></head><body>",
             "<!-- paste-ready: the title goes in note's title field; copy everything below -->"]
    for block in display_blocks(draft):
        if block["type"] == "heading":
            parts.append(f"<h2>{html.escape(block['text'])}</h2>")
        elif block["type"] == "list":
            box = "□ " if block.get("mark") == "- [ ] " else ""
            parts.append("<ul>" + "".join(f"<li>{box}{html.escape(i)}</li>" for i in block["items"])
                         + "</ul>")  # fmt: skip
        elif block["type"] == "paid_line":
            parts.append("<hr><p>--- ここで note の有料ラインを入れる (この行は貼らない) ---</p>"
                         "<hr>")
        elif block["type"] == "subheading":
            parts.append(f"<h3>{html.escape(block['text'][4:])}</h3>")
        elif block["type"] == "code":
            inner = block["text"].split("\n")[1:-1]
            parts.append("<pre><code>" + html.escape("\n".join(inner)) + "</code></pre>")
        else:
            parts.append(f"<p>{html.escape(block['text'])}</p>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def plain_text_parts(draft: NoteDraft) -> tuple[str, str]:
    """有料の記事の貼り付け用 (無料の部分, 有料の部分)。無料の記事は (全文, "")。"""

    if not paid.is_paid(draft) or draft.paid_from_section is None:
        return plain_text(draft), ""

    def render(sections):
        parts = []
        for section in sections:
            parts += [section["heading"], "", *[p + "\n" for p in section["paragraphs"]]]
        return "\n".join(parts).rstrip() + "\n"

    index = draft.paid_from_section
    return render(draft.sections[:index]), render(draft.sections[index:])


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
#: -3 (2026-10-01): 画像は承認の時に中身の sha256 の名前で写し、写しを正本にする。
#: -2 までの承認 (写しが無い) は legacy として有効なまま (後から写しを作らない)。
APPROVAL_SCHEMA = "note-approval/3"
#: 承認した画像の写しの置き場 (root からの相対。git 管理外の ``reports/`` の下)。
SNAPSHOT_DIR = Path("reports/note/approved-images")
SNAPSHOT_SCHEMA = "note-image-snapshot/1"
CANONICAL_SNAPSHOT = "approval_snapshot"
CANONICAL_LEGACY = "legacy_no_snapshot"
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _mime_of(data: bytes) -> str | None:
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_image(path: Path | str) -> tuple[bytes, dict]:
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
    return data, {"filename": file.name, "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "mime_type": mime}  # fmt: skip


def inspect_image(path: Path | str) -> dict:
    """承認・公開に使う画像の指紋 (名前・大きさ・sha256・中身から分かる形式)。

    無い・読めない・空・形式が分からない、はすべて拒む (fail closed)。記録するのは
    ファイルの名前だけで、手元のパスは記録しない。
    """

    return _read_image(path)[1]


def _same(info: dict, other: dict) -> bool:
    return all(info.get(k) == other.get(k) for k in ("bytes", "sha256", "mime_type"))


def snapshot_image(source: Path | str, *, root: Path | str, draft_id: str) -> dict:
    """承認する画像を、中身の sha256 の名前で写す (上書きしない)。写しを読み直して確かめる。

    同じ中身の写しが既にあれば、中身を確かめてそのまま使う (``reused``)。既にある写しの中身が
    名前の hash と違えば拒む (直さない・上書きしない)。返すのは、元のファイルの指紋と写しの
    参照 (root からの相対パス。手元の絶対パスは記録しない)。
    """

    if not _SAFE_ID.match(draft_id or ""):
        raise NoteStatusError(f"unsafe draft id for the image snapshot: {draft_id!r}")
    data, info = _read_image(source)
    rel = SNAPSHOT_DIR / draft_id / f"{info['sha256']}.{_EXTENSIONS[info['mime_type']]}"
    target = Path(root) / rel
    created = False
    if not target.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NoteStatusError(f"the image snapshot store could not be created "
                                  f"({type(exc).__name__}); nothing was approved") from exc
        try:
            with open(target, "xb") as handle:  # 既にあれば作らない (上書きしない)
                created = True
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            created = False  # 同時に作られた: 下で中身を確かめる
        except OSError as exc:
            if created:
                target.unlink(missing_ok=True)
            raise NoteStatusError(f"the image snapshot could not be written "
                                  f"({type(exc).__name__}); nothing was approved") from exc
    try:
        _data, written = _read_image(target)
    except NoteStatusError as exc:
        if created:
            target.unlink(missing_ok=True)
        raise NoteStatusError(f"the image snapshot cannot be read back ({exc}); "
                              "nothing was approved") from exc  # fmt: skip
    if not _same(info, written):
        if created:
            target.unlink(missing_ok=True)
            raise NoteStatusError("the image snapshot does not match the image; nothing was "
                                  "approved")  # fmt: skip
        raise NoteStatusError(f"the existing snapshot {rel.as_posix()} does not match its hash; "
                              "it is not overwritten (check it by hand)")  # fmt: skip
    if created:
        target.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)  # 読むだけにする
    source_path = Path(source)
    try:
        provenance = source_path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        provenance = None  # root の外: 名前だけ
    return {**info,
            "source": {"filename": source_path.name, "path": provenance,
                       "role": "provenance only; not canonical after approval"},
            "snapshot": {"schema": SNAPSHOT_SCHEMA, "path": rel.as_posix(),
                         "bytes": written["bytes"], "sha256": written["sha256"],
                         "mime_type": written["mime_type"], "reused": not created},
            "canonical": CANONICAL_SNAPSHOT}  # fmt: skip


def verify_snapshot(image: dict, root: Path | str | None) -> dict:
    """承認の画像の写しが、今も承認したときの中身か。写しの無い古い承認は ``legacy``。"""

    snap = image.get("snapshot")
    if not snap:
        return {"state": "legacy", "canonical": CANONICAL_LEGACY,
                "note": "approved before image snapshots; valid as is (not backfilled)"}
    if root is None:
        return {"state": "unchecked", "note": "no snapshot store was given"}
    base = (Path(root) / SNAPSHOT_DIR).resolve()
    path = (Path(root) / str(snap.get("path") or "")).resolve()
    if not path.is_relative_to(base) or not _SHA256.match(str(snap.get("sha256") or "")):
        return {"state": "invalid", "path": snap.get("path"),
                "note": "the snapshot reference is outside the snapshot store"}
    if not path.is_file():
        return {"state": "missing", "path": snap.get("path")}
    try:
        _data, current = _read_image(path)
    except NoteStatusError as exc:
        return {"state": "unreadable", "path": snap.get("path"), "note": str(exc)}
    same = _same(current, snap) and _same(current, image)
    return {"state": "ok" if same else "changed", "path": snap.get("path"),
            "sha256": current["sha256"], "bytes": current["bytes"]}


def image_required(draft: NoteDraft) -> bool:
    """サムネイルの指示がある下書きは、公開に使う画像そのものを承認に含める。"""

    return bool(draft.thumbnail_brief)


def approved_images(draft: NoteDraft) -> list[dict]:
    """承認に拘束された画像 (古い承認には無い。補わない)。"""

    return list((draft.approval or {}).get("images") or [])


def check_image(draft: NoteDraft, path: Path | str, *, root: Path | str | None = None) -> dict:
    """手元の画像が、承認したときの画像と同じか (大きさと sha256)。名前が同じでも中身が
    変われば同じではない。

    -3: 承認の正本は写し。元のファイルが後で変わっても承認は効いたまま (``approval_valid``)。
    変わった画像を note に使うなら、取り消して承認し直す。
    """

    approved = approved_images(draft)
    current = inspect_image(path)
    if not approved:
        return {"state": "no_image_bound", "current": current,
                "note": "the current approval binds no image"}  # fmt: skip
    same = any(a["sha256"] == current["sha256"] and a["bytes"] == current["bytes"]
               for a in approved)  # fmt: skip
    snapshots = [verify_snapshot(a, root) for a in approved]
    out = {"state": "matches_approval" if same else "changed_since_approval",
           "current": current, "approved": approved, "snapshots": snapshots,
           "note": ("this is the approved image" if same else
                    "this file differs from the approved image: to use it in note, reopen and "
                    "approve again")}  # fmt: skip
    if all(s["state"] == "ok" for s in snapshots):
        out["approval_valid"] = True
        if not same:
            out["note"] += ("; the approval itself is still valid (its canonical image is the "
                            "approval snapshot)")  # fmt: skip
    return out


def _archive_approval(draft: NoteDraft, reason: str, now: datetime) -> None:
    """効かなくなった承認を消さずに残す (過去の承認の意味は書き換えない)。"""

    if draft.approval:
        draft.approval_history.append({**draft.approval, "superseded_at":
                                       now.isoformat(timespec="seconds"),
                                       "superseded_reason": reason})  # fmt: skip
    draft.approval = None


def paid_packet(draft: NoteDraft, root: Path | str | None = None) -> dict | None:
    """有料の記事の確認用のまとめ (承認しない)。無料の記事は None。"""

    if not paid.is_paid(draft):
        return None
    out: dict = {"approval_schema": paid.APPROVAL_SCHEMA_PAID, "problems": paid.problems(draft)}
    if out["problems"]:
        return out
    terms = paid.terms(draft)
    free_text, paid_text = (paid.section_text(draft, "free"), paid.section_text(draft, "paid"))
    out.update(terms=terms, commercial_hash=paid.commercial_hash(draft),
               free_chars=len(free_text), paid_chars=len(paid_text))
    if root is not None:
        out["missing_asset_lines"] = paid.missing_asset_lines(draft, root)
        release = paid.h5_release(root, draft.product)
        out["h5_release"] = ({"approved_by": release.get("by"), "approved_at": release.get("at")}
                             if release else None)
    return out


def review_packet(draft: NoteDraft, *, root: Path | str | None = None) -> dict:
    links = links_of(draft)
    paid_info = paid_packet(draft, root)
    return {"paid": paid_info,
        "draft_id": draft.id, "status": draft.status, "title": draft.working_title,
        "access_mode": draft.access_mode, "content_hash": draft.content_hash,
        "links": links, "links_hash": _links_hash(links),
        "image_required": image_required(draft),
        "images": [{**{k: i.get(k) for k in ("filename", "bytes", "sha256", "mime_type")},
                    "snapshot": (i.get("snapshot") or {}).get("path"),
                    "canonical": i.get("canonical") or CANONICAL_LEGACY}
                   for i in approved_images(draft)],
        "errors": list(draft.errors), "warnings": list(draft.warnings),
        "edited_by_human": draft.edited_by_human, "edited_by": draft.edited_by,
        "headings": [s["heading"] for s in draft.sections],
        "tags": list(draft.tags), "thumbnail_brief": draft.thumbnail_brief,
        "human_approves": ["final title", "final body (content_hash)", "external links",
                           "images / thumbnail", "tags", "access mode",
                           *(["paid boundary, price and the sold product (commercial_hash)",
                              "the product is H5-approved"] if paid_info else []),
                           "the act of publishing (in note)"],
        "can_submit": draft.status == "draft" and not draft.errors,
        # 有料の記事は、売る物が H5 で承認されていないと承認できない (root を渡したときに確かめる)
        "can_approve": draft.status == "review_ready" and not draft.errors and (
            paid_info is None or (not paid_info["problems"] and bool(paid_info.get("h5_release")))),
    }  # fmt: skip


def render_packet(draft: NoteDraft, *, root: Path | str | None = None) -> str:
    packet = review_packet(draft, root=root)
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
                  f"sha256 {i['sha256']} · canonical: "
                  + (f"snapshot {i['snapshot']}" if i["snapshot"] else
                     "legacy (no snapshot; not backfilled)")
                  for i in packet["images"]]  # fmt: skip
    elif packet["image_required"]:
        lines.append("- image: **required** (the thumbnail brief is set) — not bound yet; the "
                     "human approves with the final image file (`approve ... --image <file>`)")
    else:
        lines.append("- images: none")
    lines += ["- the system binds the approved image by content hash (a snapshot copy); the "
              "human checks that the image uploaded to note is the same one"] if packet[
        "image_required"] else []  # fmt: skip
    lines += _paid_packet_lines(packet["paid"], draft)
    lines += [f"- error: {e}" for e in draft.errors] or ["- errors: none"]
    lines += [f"- warning: {w}" for w in draft.warnings]
    lines += ["", "## 根拠 (事実の文を確かめるため。公開の前に消す)", ""]
    for claim in draft.claims:
        refs = ", ".join(f"{e.source} ({e.locator})" for e in claim.evidence) or "—"
        lines.append(f"- [{claim.kind}] {claim.text} — {refs}")
    lines += ["", "人が承認するもの: " + "・".join(packet["human_approves"])]
    return "\n".join(lines) + "\n"


def _paid_packet_lines(info: dict | None, draft: NoteDraft) -> list[str]:
    if not info:
        return []
    if info["problems"]:
        return [f"- paid article (note-approval/4): **incomplete** — {p}" for p in info["problems"]]
    terms = info["terms"]
    bound, product, price = terms["boundary"], terms["product"], terms["price"]
    h5 = info.get("h5_release")
    missing = {k: v for k, v in (info.get("missing_asset_lines") or {}).items() if v}
    return [
        "- paid article: approval schema **note-approval/4**",
        f"- paid boundary: after section {bound['paid_from_section']} "
        f"「{bound['free_headings'][-1]}」 → paid from 「{bound['paid_headings'][0]}」 "
        f"(free {len(bound['free_headings'])} sections / {info['free_chars']} chars, paid "
        f"{len(bound['paid_headings'])} sections / {info['paid_chars']} chars)",
        f"- price: {price['amount']} {price['currency']} "
        "(operating value; not the product version)",
        f"- sold product: `{product['product_id']}` {product['version']} · content hash "
        f"`{product['content_hash']}` · manifest `{product['manifest_hash']}` · package sha256 "
        f"`{product['package_sha256']}`",
        f"- commercial_hash: {info['commercial_hash']}",
        "- product assets in the paid part: "
        + ("all lines present" if not missing and "missing_asset_lines" in info
           else f"missing {missing}" if missing else "not checked"),
        "- product H5 (release approval): " + (f"approved by {h5['approved_by']} at "
                                               f"{h5['approved_at']}" if h5 else
                                               "**not yet** — the paid approval needs it"),
        f"- approve: `approve {draft.id} --content-hash {draft.content_hash} --commercial-hash "
        f"{info['commercial_hash']} --by <name> --links-approved --image <thumbnail>` "
        "(only after H4 and H5)",
        "- after publishing: the paid part cannot be read on the public page; a human records "
        "how it was checked (`record-paid-verification`)",
        "- human review (paid): [ ] the free part is worth reading on its own (not thinned) · "
        "[ ] the paid line sits where intended · [ ] the price · [ ] the sold product version is "
        "the H5-approved one · [ ] the paid part matches the product's assets · [ ] the terms of "
        "use · [ ] no promised results · [ ] after pasting, the note display (tables, code "
        "blocks, lists) is readable",
    ]  # fmt: skip


# -- 承認と公開の記録 ---------------------------------------------------------------------------
def submit(draft: NoteDraft) -> NoteDraft:
    return transition(draft, "review_ready")


def approve(draft: NoteDraft, *, content_hash: str, approved_by: str, now: datetime,
            links_approved: bool = False, image: Path | str | None = None,
            note: str | None = None,
            after_publication_at: datetime | None = None,
            snapshot_root: Path | str | None = None,
            commercial_hash: str | None = None,
            product_root: Path | str | None = None) -> NoteDraft:  # fmt: skip
    """人の承認を記録する (無料の記事は ``note-approval/3``、有料の記事は ``note-approval/4``)。

    承認に入るもの (それぞれ別に追える): 本文 (``content_hash``。本文と題名だけ、今までと同じ)・
    外部リンク (一覧と hash。``links_approved`` が要る)・タグ・公開の形・サムネイルの指示・
    **画像** (``image`` のファイルの名前・大きさ・sha256・形式)。サムネイルの指示がある下書きは、
    画像のファイルを渡さないと承認できない (真偽値だけの「画像も承認」は作らない)。

    -3: 画像は承認の前に ``snapshot_root`` の写しの置き場へ写し、写しを読み直して確かめる。
    写しが作れない・確かめられなければ承認しない。承認の正本の画像は写し。

    -4 (有料の記事だけ): 販売の条件 (有料の境界・価格・売る物) の ``commercial_hash`` も人が
    確かめて渡す (``commercial_hash``)。有料の部分が売る物の package の資産をすべて含み、売る物が
    H5 で承認された版であること (``product_root`` の ``products/<id>/records.json``) を要る。
    """

    if draft.errors:
        raise NoteStatusError(f"draft has errors: {draft.errors}")
    links = links_of(draft)
    if links and not links_approved:
        raise NoteStatusError(f"{len(links)} external link(s) need explicit approval")
    if not approved_by.strip():
        raise NoteStatusError("approved_by is required")
    if image_required(draft) and image is None:
        raise NoteStatusError("this draft uses a thumbnail: approve it with the final image "
                              "file (--image <file>)")  # fmt: skip
    # 写しを作る前に、承認できる下書きかを確かめる (状態と本文の hash)
    if "approved" not in TRANSITIONS[draft.status]:
        raise NoteStatusError(f"{draft.status} -> approved is not allowed (an approved draft "
                              "changes its image only by reopen, submit and approve)")  # fmt: skip
    if content_hash != draft.content_hash:
        raise NoteStatusError("approval is for a different content hash")
    paid_extra = _paid_approval(draft, commercial_hash=commercial_hash, product_root=product_root)
    images = []
    if image is not None:
        if snapshot_root is None:
            raise NoteStatusError("an image approval needs the snapshot store (snapshot_root)")
        images = [snapshot_image(image, root=snapshot_root, draft_id=draft.id)]
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
    return transition(draft, "approved", approval={**extra, **paid_extra,
        "approval_schema": paid.APPROVAL_SCHEMA_PAID if paid_extra else APPROVAL_SCHEMA,
        "approved_by": approved_by.strip(), "approved_at": now.isoformat(timespec="seconds"),
        "content_hash": content_hash, "access_mode": draft.access_mode,
        "links": links, "links_hash": _links_hash(links), "links_approved": bool(links),
        "tags": list(draft.tags), "thumbnail_brief": draft.thumbnail_brief,
        "edited_by": draft.edited_by,
        "image_required": image_required(draft),
        "images_approved": bool(images), "images": images})  # fmt: skip


def _paid_approval(draft: NoteDraft, *, commercial_hash: str | None,
                   product_root: Path | str | None) -> dict:  # fmt: skip
    """有料の記事の承認に入るもの (-4)。無料の記事は空 (今までと同じ -3)。"""

    if not paid.is_paid(draft):
        if commercial_hash is not None:
            raise NoteStatusError("a free article has no commercial terms (no --commercial-hash)")
        return {}
    terms = paid.terms(draft)  # 境界・価格・売る物が足りなければ拒む
    current = paid.commercial_hash(draft)
    if commercial_hash is None:
        raise NoteStatusError("a paid article is approved with its commercial terms: pass "
                              f"--commercial-hash {current}")  # fmt: skip
    if commercial_hash != current:
        raise NoteStatusError("the commercial terms are not the reviewed ones (commercial hash "
                              "mismatch: boundary, price or product changed)")  # fmt: skip
    if product_root is None:
        raise NoteStatusError("a paid approval needs the product records (product_root)")
    missing = {k: v for k, v in paid.missing_asset_lines(draft, product_root).items() if v}
    if missing:
        raise NoteStatusError(f"the paid part does not contain the product's assets: {missing}")
    release = paid.h5_release(product_root, draft.product)
    if release is None:
        raise NoteStatusError(f"the product {draft.product['product_id']} "
                              f"{draft.product['version']} is not H5-approved (release_approval "
                              "with the same manifest and package); approve the release first")
    return {"commercial_hash": current, "paid_terms": terms,
            "product_release": {"approved_by": release.get("by"), "approved_at": release.get("at"),
                                "manifest_hash": release.get("manifest_hash"),
                                "package_sha256": release.get("package_sha256")}}  # fmt: skip


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


NOTE_IMAGE_MATCH = ("human responsibility: the system binds the approved image by content hash; "
                    "it cannot check the image uploaded to note")


def record_publication(draft: NoteDraft, *, url: str, observed_at: datetime,
                       published_hash: str, now: datetime, policy: dict,
                       image: Path | str | None = None,
                       snapshot_root: Path | str | None = None,
                       note: str | None = None,
                       product_root: Path | str | None = None,
                       observed_price: dict | None = None,
                       free_section_check: str | None = None,
                       paid_verified_by: str | None = None,
                       paid_verification: str | None = None) -> NoteDraft:  # fmt: skip
    """人が note で公開した後に、その証拠を記録する。承認した本文と違えば記録しない。

    -3: 承認の画像の正本は写し (``snapshot_root`` の下)。写しが今も承認の中身であることを確かめて
    記録する (元のファイルが後で変わっても失敗しない)。``image`` は任意の確認: 人が note に使った
    ファイルを渡すと、承認の画像と同じかを比べ、違えば記録しない (取り消して承認し直す)。
    写しの無い古い承認 (legacy) は今までどおり ``image`` が要る。承認した画像の指紋は公開の記録に
    引き継ぐ。note に上がった画像との一致は、仕組みでは確かめられないので人の責任。
    ``note``: 公開ページの確認で分かったこと (例: note がタグの表記を変えた) を残す短いメモ。
    承認の記録は変えない。

    -4 (有料の記事): 販売の条件が承認のときと同じ (``commercial_hash``)・売る物が今も H5 で承認
    された版であることを確かめる。``observed_price`` (note の画面の価格) を渡すと承認の価格と
    比べ、違えば記録しない。有料の部分は買わないと読めないので、人が確かめた
    (``paid_verified_by`` と ``paid_verification``) ときだけ ``human_verified``、それ以外は
    ``human_verification_required`` と記録する (確かめていないのに一致とは書かない)。
    """

    if draft.status != "approved" or not draft.approval:
        raise NoteStatusError("only an approved draft can be recorded as published")
    if published_hash != draft.approval["content_hash"] or published_hash != draft.content_hash:
        raise NoteStatusError("the published body is not the approved version (hash mismatch); "
                              "edit, re-submit and re-approve first")  # fmt: skip
    if draft.approval.get("access_mode", "free") != draft.access_mode:
        raise NoteStatusError("the access mode changed after approval")
    paid_record = _paid_publication(draft, product_root=product_root, now=now,
                                    observed_price=observed_price,
                                    free_section_check=free_section_check,
                                    paid_verified_by=paid_verified_by,
                                    paid_verification=paid_verification)  # fmt: skip
    bound = approved_images(draft)
    states = [verify_snapshot(i, snapshot_root) for i in bound]
    for i, state in zip(bound, states, strict=True):
        if state["state"] not in ("ok", "legacy"):
            raise NoteStatusError(f"the approved image snapshot of {i.get('filename')} is "
                                  f"{state['state']} ({state.get('path')}); nothing recorded")
    legacy = any(s["state"] == "legacy" for s in states)
    confirmation = None
    if bound and image is not None:
        check = check_image(draft, image, root=snapshot_root)
        current = check["current"]
        if check["state"] != "matches_approval":
            raise NoteStatusError(f"the image is not the approved one ({current['filename']}, "
                                  f"sha256 {current['sha256'][:12]}); reopen and approve the "
                                  "new image first")  # fmt: skip
        confirmation = {"checked": True, "filename": current["filename"],
                        "sha256": current["sha256"], "matches_approval": True}  # fmt: skip
    elif bound and legacy:
        raise NoteStatusError("the approval binds an image without a snapshot (legacy): pass "
                              "the image file used for the publication (--image <file>)")
    elif bound:
        confirmation = {"checked": False,
                        "note": "no file given; the canonical image is the approval snapshot"}
    return transition(draft, "published", publication={
        "url": check_publication_url(url, policy),
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "recorded_at": now.isoformat(timespec="seconds"),
        "content_hash": published_hash, "access_mode": draft.access_mode,
        "published_by": "human (note editor)",
        "approved_images": [{**{k: i.get(k) for k in ("filename", "bytes", "sha256", "mime_type")},
                             **({"snapshot": i["snapshot"]["path"],
                                 "canonical": CANONICAL_SNAPSHOT} if i.get("snapshot")
                                else {"canonical": CANONICAL_LEGACY})}
                            for i in bound],
        **({"image_confirmation": confirmation} if confirmation else {}),
        **({"publication_note": note.strip()[:500]} if note and note.strip() else {}),
        **({"paid": paid_record} if paid_record else {}),
        "note_image_match": NOTE_IMAGE_MATCH if bound else
        "no image was bound at approval"})  # fmt: skip


HUMAN_VERIFICATION_REQUIRED = "human_verification_required"


def _verification(by: str | None, method: str | None, now: datetime) -> dict:
    by, method = (by or "").strip(), (method or "").strip()
    if not by or "@" in by or not method:
        raise NoteStatusError("a paid-section verification needs a short name (--paid-verified-by) "
                              "and how it was checked (--paid-verification)")  # fmt: skip
    return {"state": "human_verified", "by": by[:64], "method": method[:300],
            "at": now.isoformat(timespec="seconds")}


def _paid_publication(draft: NoteDraft, *, product_root, now: datetime, observed_price,
                      free_section_check, paid_verified_by, paid_verification) -> dict | None:
    """有料の記事の公開の記録 (-4)。無料の記事は None。"""

    approval = draft.approval or {}
    if not paid.is_paid(draft) and approval.get("approval_schema") != paid.APPROVAL_SCHEMA_PAID:
        if observed_price or paid_verified_by or paid_verification:
            raise NoteStatusError("price and paid-section evidence are for paid articles only")
        return None
    if approval.get("approval_schema") != paid.APPROVAL_SCHEMA_PAID:
        raise NoteStatusError("a paid article needs a note-approval/4 approval (commercial terms)")
    if paid.commercial_hash(draft) != approval.get("commercial_hash"):
        raise NoteStatusError("the commercial terms changed after approval (boundary, price or "
                              "product); reopen and approve again")  # fmt: skip
    if product_root is None or paid.h5_release(product_root, draft.product) is None:
        raise NoteStatusError("the sold product is no longer the H5-approved release")
    approved_price = approval["paid_terms"]["price"]
    if observed_price is not None:
        seen = paid.check_price(observed_price.get("amount"), observed_price.get("currency"))
        if seen != approved_price:
            raise NoteStatusError(f"the price in note ({seen['amount']} {seen['currency']}) is not "
                                  f"the approved price ({approved_price['amount']} "
                                  f"{approved_price['currency']}); nothing recorded")
        price_record = {"state": "observed", **seen,
                        "source": str(observed_price.get("source") or "note page")[:200]}
    else:
        price_record = {"state": "not_observed"}
    if paid_verified_by or paid_verification:
        paid_section = _verification(paid_verified_by, paid_verification, now)
    else:
        paid_section = {"state": HUMAN_VERIFICATION_REQUIRED,
                        "note": "the paid part cannot be read on the public page without buying; "
                                "a human confirms it (record-paid-verification)"}
    return {"commercial_hash": approval["commercial_hash"], "approved_price": approved_price,
            "observed_price": price_record,
            "boundary": approval["paid_terms"]["boundary"],
            "product": approval["paid_terms"]["product"],
            "public_free_section": ({"state": "checked", "result": free_section_check.strip()[:300]}
                                    if free_section_check and free_section_check.strip()
                                    else {"state": "not_checked"}),
            "paid_section": paid_section}  # fmt: skip


def record_paid_verification(draft: NoteDraft, *, by: str, method: str, now: datetime) -> NoteDraft:
    """公開の後に、人が有料の部分を確かめたことを足す (追記だけ。前の記録は変えない)。"""

    record = (draft.publication or {}).get("paid")
    if draft.status != "published" or not record:
        raise NoteStatusError("only a published paid article takes a paid-section verification")
    draft.publication.setdefault("paid_verifications", []).append(_verification(by, method, now))
    return draft


def paid_section_state(publication: dict | None) -> str | None:
    """有料の部分の確認の状態 (後から足した人の確認を含む)。無料の記事は None。"""

    record = (publication or {}).get("paid")
    if not record:
        return None
    if record["paid_section"]["state"] == "human_verified" or publication.get(
            "paid_verifications"):  # fmt: skip
        return "human_verified"
    return HUMAN_VERIFICATION_REQUIRED


def reject(draft: NoteDraft, *, reason: str, now: datetime) -> NoteDraft:
    if not reason.strip():
        raise NoteStatusError("a reason is required")
    transition(draft, "rejected")
    draft.warnings = sorted(set(draft.warnings) | {
        f"rejected at {now.isoformat(timespec='seconds')}: {reason.strip()[:200]}"})
    return draft


__all__ = ["APPROVAL_SCHEMA", "CANONICAL_LEGACY", "CANONICAL_SNAPSHOT", "NOTE_IMAGE_MATCH",
           "HUMAN_VERIFICATION_REQUIRED", "PAID_CONTENT_TYPE", "import_paid_draft",
           "paid_draft_id", "paid_packet", "paid_section_state", "plain_text_parts",
           "record_paid_verification", "sections_from_markdown", "set_paid_terms",
           "LIST_MARK", "SNAPSHOT_DIR", "SNAPSHOT_SCHEMA", "add_evidence", "apply_edit", "approve",
           "body_lines", "canonical_lines", "compare_published", "display_blocks", "paste_html",
           "approved_images", "check_image", "check_publication_url", "draft_from_dict",
           "image_required", "inspect_image", "reopen", "snapshot_image", "verify_snapshot",
           "links_of", "load_policy", "parse_markdown", "plain_text", "record_publication",
           "recheck", "reject", "render_packet", "review_packet", "set_access_mode", "set_meta",
           "submit"]
