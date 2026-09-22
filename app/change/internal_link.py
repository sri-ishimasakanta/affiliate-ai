"""内部リンク追加の提案生成 (C9、pure)。

**本文を書き換えない。** 既存の段落・見出し・引用・リンクは 1 バイトも触らず、
短い段落を 1 つ **挿入するだけ** にする。これが「周囲の日本語を保つ」もっとも
確実な方法であり、差分も人が読み切れる大きさに収まる。

挿入位置は決定的に選ぶ:

    導入部の最後の段落 (= 最初の ``##`` 見出しの直前) の後ろ。

「関連記事はこちら」に相当する位置で、記事の構造を崩さない。見出しの内部にも、
既存のリンクの内部にも、リスト項目の途中にも入らない。

安全ガード (どれか 1 つでも該当すれば提案しない):

- 同じリンク先への内部リンクが既にある
- 挿入できる導入部が無い (先頭が見出しなど)
- リンク先が公開 URL を持っていない
- 本文が空

出典・参考文献のリンクやアフィリエイトリンクは **一切触らない** -- 純粋な挿入
なので、そもそも変更対象にならない。
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.seo.url_normalization import normalize_url_key

#: 挿入する段落のテンプレート。決定的に 1 つだけ使う。
_PARAGRAPH_TEMPLATE = "あわせて読みたい: [{title}]({url})"

_HEADING_RE = re.compile(r"^#{1,6}\s")
#: 記事の題としての H1 (節の区切りではないので導入部の探索では読み飛ばす)。
_TITLE_HEADING_RE = re.compile(r"^#\s")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
#: 段落として扱わない行頭 (リスト・引用・コードなど)。
_NON_PARAGRAPH_RE = re.compile(r"^\s*(?:[-*+]\s|\d+\.\s|>|```|\||#)")


@dataclass
class ProposalRejection:
    """提案できない理由 (黙って握りつぶさない)。"""

    reason_code: str
    detail: str


@dataclass
class InternalLinkProposal:
    """1 件の内部リンク追加提案 (immutable な内容として扱う)。"""

    source_article_id: int
    target_article_id: int
    target_url: str
    anchor_text: str
    inserted_paragraph: str
    #: 挿入位置の直前/直後の本文 (人が位置を確認するための文脈)。
    context_before: str
    context_after: str
    proposed_body: str
    source_body_hash: str
    proposed_body_hash: str
    unified_diff: str
    insertion_line: int
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source_article_id": self.source_article_id,
            "target_article_id": self.target_article_id,
            "target_url": self.target_url,
            "anchor_text": self.anchor_text,
            "inserted_paragraph": self.inserted_paragraph,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "source_body_hash": self.source_body_hash,
            "proposed_body_hash": self.proposed_body_hash,
            "unified_diff": self.unified_diff,
            "insertion_line": self.insertion_line,
            "warnings": self.warnings,
        }


def compute_proposal_hash(
    *,
    change_type: str,
    source_article_id: int,
    target_article_id: int | None,
    source_body_hash: str,
    proposed_body_hash: str,
    anchor_text: str,
    inserted_paragraph: str,
    insertion_line: int,
) -> str:
    """提案の identity。内容が 1 文字でも変われば別の hash になる。

    承認はこの値に結び付くので、「あとで生成される別の文章」が同じ承認で通ることは
    あり得ない。
    """

    # フィールド区切りに unit separator を挟む。区切りが無いと、隣接する値の
    # 切れ目が変わっただけの別提案が同じ hash になりうる。
    payload = chr(31).join(
        [
            change_type,
            str(source_article_id),
            str(target_article_id),
            source_body_hash,
            proposed_body_hash,
            anchor_text,
            inserted_paragraph,
            str(insertion_line),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def existing_link_targets(body: str) -> set[str | None]:
    """本文が既にリンクしている URL の正規化キー。"""

    return {normalize_url_key(url) for url in _MD_LINK_RE.findall(body or "")}


def has_link_to(body: str, url: str) -> bool:
    return normalize_url_key(url) in existing_link_targets(body)


def _find_insertion_line(lines: list[str]) -> int | None:
    """導入部の最後 (= 最初の節見出しの直前) の行番号を返す。

    本文が ``# タイトル`` で始まる場合、その H1 は記事の題であって節ではないので
    読み飛ばし、H1 と最初の節見出しのあいだを導入部として扱う。

    見出しの内部にも、リスト/引用/コードブロックの途中にも入らない。
    """

    start = 0
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if _TITLE_HEADING_RE.match(line):
            start = index + 1
        break

    end = next(
        (index for index in range(start, len(lines)) if _HEADING_RE.match(lines[index])),
        # 見出しが無い記事は末尾に足す。
        len(lines),
    )

    # 導入部のうち、通常の段落である最後の行を探す。
    for index in range(end - 1, start - 1, -1):
        line = lines[index]
        if not line.strip():
            continue
        if _NON_PARAGRAPH_RE.match(line):
            return None
        return index + 1
    return None


def build_internal_link_proposal(
    *,
    source_article_id: int,
    source_body: str,
    target_article_id: int,
    target_title: str,
    target_url: str,
    body_hasher,
) -> tuple[InternalLinkProposal | None, ProposalRejection | None]:
    """決定的に 1 つの提案を組み立てる (WordPress には一切触れない)。"""

    body = source_body or ""
    if not body.strip():
        return None, ProposalRejection("EMPTY_SOURCE_BODY", "source article body is empty")
    if not target_url or not urlsplit(target_url).netloc:
        return None, ProposalRejection(
            "TARGET_URL_UNAVAILABLE", "target article has no usable published URL"
        )
    if has_link_to(body, target_url):
        return None, ProposalRejection(
            "LINK_ALREADY_PRESENT", f"the source article already links to {target_url}"
        )

    lines = body.split("\n")
    insertion_line = _find_insertion_line(lines)
    if insertion_line is None:
        return None, ProposalRejection(
            "NO_SAFE_INSERTION_POINT",
            "no plain introductory paragraph was found before the first heading",
        )

    anchor_text = (target_title or target_url).strip()
    paragraph = _PARAGRAPH_TEMPLATE.format(title=anchor_text, url=target_url)

    proposed_lines = lines[:insertion_line] + ["", paragraph] + lines[insertion_line:]
    proposed_body = "\n".join(proposed_lines)

    source_hash = body_hasher(body)
    proposed_hash = body_hasher(proposed_body)

    diff = "\n".join(
        difflib.unified_diff(
            lines,
            proposed_lines,
            fromfile=f"article-{source_article_id} (current)",
            tofile=f"article-{source_article_id} (proposed)",
            lineterm="",
            n=2,
        )
    )

    warnings: list[str] = []
    # 純粋な挿入なので、既存リンクは順序も含めてそのまま残り、新しい 1 本だけが
    # 増えるはず。新リンクを 1 つ取り除いた残りが元と一致することで検証する
    # (挿入位置は既存リンクより前にも後にもなりうるので、前方一致では見ない)。
    before_links = _MD_LINK_RE.findall(body)
    after_links = _MD_LINK_RE.findall(proposed_body)
    remaining = list(after_links)
    if target_url in remaining:
        remaining.remove(target_url)
    if remaining != before_links:
        warnings.append("existing links changed; review the diff carefully")
    if len(after_links) != len(before_links) + 1:
        warnings.append("the proposal does not add exactly one link")
    if not non_link_text_unchanged(body, proposed_body):
        warnings.append("existing prose was modified; this should be an insertion only")

    return (
        InternalLinkProposal(
            source_article_id=source_article_id,
            target_article_id=target_article_id,
            target_url=target_url,
            anchor_text=anchor_text,
            inserted_paragraph=paragraph,
            context_before=_context(lines, insertion_line - 1),
            context_after=_context(lines, insertion_line),
            proposed_body=proposed_body,
            source_body_hash=source_hash,
            proposed_body_hash=proposed_hash,
            unified_diff=diff,
            insertion_line=insertion_line,
            warnings=warnings,
        ),
        None,
    )


def _context(lines: list[str], index: int, *, width: int = 160) -> str:
    if index < 0 or index >= len(lines):
        return ""
    return lines[index][:width]


def added_link_count(before: str, after: str) -> int:
    return len(_MD_LINK_RE.findall(after)) - len(_MD_LINK_RE.findall(before))


def non_link_text_unchanged(before: str, after: str) -> bool:
    """挿入した段落を除いて、本文が一致するか (既存の散文を守るための検査)。"""

    before_lines = before.split("\n")
    after_lines = after.split("\n")
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    for tag, _i1, _i2, _j1, _j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            return False
    return True
