"""canonical HTML -> tracked (affiliate-substituted) HTML の byte-preserving
substitution engine (pure, D-D3)。

DB / network 非依存。``render_wordpress_html(Article.body)`` が出力した canonical
HTML 文字列を **一切 DOM 再シリアライズしない**。承認された external link 出現の
``<a ...>`` open tag だけを、正規表現で特定した exact byte span で局所的にパッチする
— それ以外の全バイト (テキスト・空白・改行・属性順序・引用符スタイル・エンティティ・
見出し・表・段落・リスト・他のリンク・コメント) は完全に無変更のまま素通しする。

この renderer (:mod:`app.wordpress.renderer`) は external link を常に厳密に

    <a href="{href}" target="_blank" rel="noopener noreferrer">

の形で出力し、internal link は ``target``/``rel`` を一切持たない
``<a href="{href}">`` 形で出力する (renderer.py の ``link()`` 参照)。よって
``target="_blank"`` 属性の有無だけで external/internal を機械的に判別でき、この
判別は D-D2 の ``external_links`` (document order で external のみ append される
list) と常に 1:1 で対応する — 別の occurrence 抽出ロジックを発明しない。

このモジュールは D-D2 の occurrence identity 契約 (``OCCURRENCE_SCHEMA_VERSION`` /
``compute_occurrence_identity_hash``) と D-D1 の manifest/artifact hash 契約
(``app.wordpress.publication_artifact``) をそのまま再利用する。
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.wordpress.link_occurrence import compute_occurrence_identity_hash
from app.wordpress.publication_artifact import expected_replacement_href

# 承認された tracked rel の唯一の deterministic ordering (D-D3 §7)。
TRACKED_REL = "sponsored nofollow noopener noreferrer"

# renderer.py の external link 出力の固定 marker。
_ANCHOR_TAG_RE = re.compile(r"<a\b[^>]*>")
_TARGET_BLANK_RE = re.compile(r'\btarget\s*=\s*(["\'])_blank\1')
_HREF_ATTR_RE = re.compile(r'\bhref\s*=\s*(["\'])(?P<value>.*?)\1')
_REL_ATTR_RE = re.compile(r'\brel\s*=\s*(["\'])(?P<value>.*?)\1')


class PublicationSubstitutionError(ValueError):
    """canonical/tracked HTML の構造が想定と異なる、または検証に失敗した。
    メッセージに full token / destination_url は含めない。"""


@dataclass(frozen=True)
class _AnchorMatch:
    start: int
    end: int
    tag_text: str
    href_raw: str  # unescape 済み (D-D2 の original_href と同じ規約)
    rel: str | None  # rel 属性が無ければ None


@dataclass(frozen=True)
class SelectedSubstitution:
    """1 occurrence に対して Human 承認済みの置換先を凍結した入力
    (D-D3 の preparation service がこの型で選択結果を渡す)。"""

    occurrence_ordinal: int
    occurrence_identity_hash: str
    mapping_id: int
    affiliate_link_target_id: int
    token: str
    target_projection_version: int


@dataclass(frozen=True)
class TrackedHtmlResult:
    tracked_html: str
    manifest: list[dict[str, Any]]  # occurrence_ordinal ASC, D-D1 の 10 field 契約


# ==================== anchor tag extraction (shared fwd/reverse) ===========
def _extract_external_anchor_matches(html_text: str) -> list[_AnchorMatch]:
    """``target="_blank"`` を持つ ``<a ...>`` open tag だけを document order で
    抽出する (= renderer の external link 出力そのもの)。href が無ければ fail
    closed で raise する。"""

    matches: list[_AnchorMatch] = []
    for m in _ANCHOR_TAG_RE.finditer(html_text):
        tag_text = m.group(0)
        if not _TARGET_BLANK_RE.search(tag_text):
            continue  # internal link (target="_blank" を持たない) -- 対象外
        href_m = _HREF_ATTR_RE.search(tag_text)
        if href_m is None:
            raise PublicationSubstitutionError(
                "external anchor tag has no href attribute"
            )
        rel_m = _REL_ATTR_RE.search(tag_text)
        matches.append(
            _AnchorMatch(
                start=m.start(),
                end=m.end(),
                tag_text=tag_text,
                href_raw=html.unescape(href_m.group("value")),
                rel=(rel_m.group("value") if rel_m is not None else None),
            )
        )
    return matches


def _require_consistent_with_external_links(
    matches: Sequence[_AnchorMatch], external_links: Sequence[str]
) -> None:
    if len(matches) != len(external_links):
        raise PublicationSubstitutionError(
            "external anchor tag count in HTML does not match external_links count"
        )
    for ordinal, (match, href) in enumerate(zip(matches, external_links, strict=True)):
        if match.href_raw != href:
            raise PublicationSubstitutionError(
                f"occurrence_ordinal {ordinal}: anchor href does not match "
                "external_links at the same position"
            )


# ==================== tag-level patch primitives ============================
def _patch_anchor_tag_href_rel(
    tag_text: str, *, new_href: str, new_rel: str
) -> str:
    """``tag_text`` の href/rel 属性値だけを置換する。他の属性・空白・属性順序・
    引用符スタイルは完全に無変更。rel 属性が元々無ければ ``target="_blank"`` の
    直後に deterministic に挿入する。"""

    escaped_href = html.escape(new_href, quote=True)

    def _href_repl(m: re.Match[str]) -> str:
        quote = m.group(1)
        return f"href={quote}{escaped_href}{quote}"

    patched, href_count = _HREF_ATTR_RE.subn(_href_repl, tag_text, count=1)
    if href_count != 1:
        raise PublicationSubstitutionError("failed to patch href attribute")

    if _REL_ATTR_RE.search(patched):
        def _rel_repl(m: re.Match[str]) -> str:
            quote = m.group(1)
            return f"rel={quote}{new_rel}{quote}"

        patched, rel_count = _REL_ATTR_RE.subn(_rel_repl, patched, count=1)
        if rel_count != 1:
            raise PublicationSubstitutionError("failed to patch rel attribute")
    else:
        def _insert_rel_after_target(m: re.Match[str]) -> str:
            return f'{m.group(0)} rel="{new_rel}"'

        patched, target_count = _TARGET_BLANK_RE.subn(
            _insert_rel_after_target, patched, count=1
        )
        if target_count != 1:
            raise PublicationSubstitutionError("failed to insert rel attribute")

    return patched


def _patch_anchor_tag_reverse(
    tag_text: str, *, original_href: str, rel_before: str
) -> str:
    """``_patch_anchor_tag_href_rel`` の厳密な逆操作。``rel_before`` が空文字列
    なら「元々 rel 属性が無かった」ことを意味し、現在の rel 属性 (+ 直前に挿入
    された空白) を除去する。"""

    escaped_href = html.escape(original_href, quote=True)

    def _href_repl(m: re.Match[str]) -> str:
        quote = m.group(1)
        return f"href={quote}{escaped_href}{quote}"

    patched, href_count = _HREF_ATTR_RE.subn(_href_repl, tag_text, count=1)
    if href_count != 1:
        raise PublicationSubstitutionError("failed to reverse href attribute")

    if rel_before == "":
        patched, rel_count = re.subn(
            r'\s*\brel\s*=\s*(["\']).*?\1', "", patched, count=1
        )
        if rel_count != 1:
            raise PublicationSubstitutionError(
                "failed to remove rel attribute while reversing"
            )
    else:
        def _rel_repl(m: re.Match[str]) -> str:
            quote = m.group(1)
            return f"rel={quote}{rel_before}{quote}"

        patched, rel_count = _REL_ATTR_RE.subn(_rel_repl, patched, count=1)
        if rel_count != 1:
            raise PublicationSubstitutionError("failed to reverse rel attribute")

    return patched


# ==================== forward: build_tracked_html ===========================
def build_tracked_html(
    *,
    canonical_html: str,
    external_links: Sequence[str],
    canonical_body_hash: str,
    renderer_version: str,
    selections: Sequence[SelectedSubstitution],
) -> TrackedHtmlResult:
    """canonical_html の中から承認された occurrence_ordinal の ``<a>`` open tag
    だけを置換した tracked_html + manifest を作る。選択されていない occurrence の
    バイト、および ``<a>`` 以外の全バイトは 1 バイトも変更しない。

    各 selection の ``occurrence_identity_hash`` は
    (canonical_body_hash, renderer_version, occurrence_ordinal, 実際にその
    ordinal に存在する original_href) から独立に再計算し、一致しなければ fail
    closed で reject する (呼び出し側が誤った/古い occurrence に対して選択して
    いないことの証明)。
    """

    matches = _extract_external_anchor_matches(canonical_html)
    _require_consistent_with_external_links(matches, external_links)

    selections_by_ordinal: dict[int, SelectedSubstitution] = {}
    seen_mapping_ids: set[int] = set()
    seen_identity_hashes: set[str] = set()
    for sel in selections:
        if sel.occurrence_ordinal in selections_by_ordinal:
            raise PublicationSubstitutionError(
                f"duplicate selection for occurrence_ordinal {sel.occurrence_ordinal}"
            )
        if not (0 <= sel.occurrence_ordinal < len(matches)):
            raise PublicationSubstitutionError(
                f"occurrence_ordinal {sel.occurrence_ordinal} is out of range"
            )
        if sel.mapping_id in seen_mapping_ids:
            raise PublicationSubstitutionError(
                f"mapping_id {sel.mapping_id} is selected for more than one occurrence"
            )
        if sel.occurrence_identity_hash in seen_identity_hashes:
            raise PublicationSubstitutionError(
                "duplicate occurrence_identity_hash in selections"
            )
        seen_mapping_ids.add(sel.mapping_id)
        seen_identity_hashes.add(sel.occurrence_identity_hash)
        selections_by_ordinal[sel.occurrence_ordinal] = sel

    pieces: list[str] = []
    manifest_entries: list[dict[str, Any]] = []
    cursor = 0
    for ordinal, match in enumerate(matches):
        pieces.append(canonical_html[cursor:match.start])
        sel = selections_by_ordinal.get(ordinal)
        if sel is None:
            pieces.append(match.tag_text)  # 完全無変更
            cursor = match.end
            continue

        expected_identity_hash = compute_occurrence_identity_hash(
            canonical_body_hash=canonical_body_hash,
            renderer_version=renderer_version,
            occurrence_ordinal=ordinal,
            original_href=match.href_raw,
        )
        if sel.occurrence_identity_hash != expected_identity_hash:
            raise PublicationSubstitutionError(
                f"occurrence_ordinal {ordinal}: selection occurrence_identity_hash "
                "does not match the recomputed identity for this occurrence"
            )

        replacement_href = expected_replacement_href(sel.token)
        new_tag = _patch_anchor_tag_href_rel(
            match.tag_text, new_href=replacement_href, new_rel=TRACKED_REL
        )
        pieces.append(new_tag)
        manifest_entries.append(
            {
                "occurrence_ordinal": ordinal,
                "occurrence_identity_hash": sel.occurrence_identity_hash,
                "mapping_id": sel.mapping_id,
                "affiliate_link_target_id": sel.affiliate_link_target_id,
                "token": sel.token,
                "target_projection_version": sel.target_projection_version,
                "original_href": match.href_raw,
                "replacement_href": replacement_href,
                "rel_before": match.rel if match.rel is not None else "",
                "rel_after": TRACKED_REL,
            }
        )
        cursor = match.end

    pieces.append(canonical_html[cursor:])
    manifest_entries.sort(key=lambda e: e["occurrence_ordinal"])
    return TrackedHtmlResult(tracked_html="".join(pieces), manifest=manifest_entries)


# ==================== reverse: reverse_tracked_html ==========================
def reverse_tracked_html(
    *, tracked_html: str, manifest: Sequence[Mapping[str, Any]]
) -> str:
    """tracked_html + manifest **だけ** から canonical_html を再構築する
    (strict reverse validation の核、D-D3 §13)。manifest に記録された
    replacement_href/rel_after と実際の tracked_html の内容が食い違っていれば
    fail closed で raise する。"""

    matches = _extract_external_anchor_matches(tracked_html)

    manifest_by_ordinal: dict[int, Mapping[str, Any]] = {}
    for entry in manifest:
        ordinal = entry["occurrence_ordinal"]
        if ordinal in manifest_by_ordinal:
            raise PublicationSubstitutionError(
                f"duplicate manifest entry for occurrence_ordinal {ordinal}"
            )
        manifest_by_ordinal[ordinal] = entry

    for ordinal in manifest_by_ordinal:
        if not (0 <= ordinal < len(matches)):
            raise PublicationSubstitutionError(
                f"manifest occurrence_ordinal {ordinal} is out of range for "
                "tracked_html"
            )

    pieces: list[str] = []
    cursor = 0
    for ordinal, match in enumerate(matches):
        pieces.append(tracked_html[cursor:match.start])
        entry = manifest_by_ordinal.get(ordinal)
        if entry is None:
            pieces.append(match.tag_text)
            cursor = match.end
            continue

        expected_href = expected_replacement_href(entry["token"])
        if match.href_raw != expected_href:
            raise PublicationSubstitutionError(
                f"occurrence_ordinal {ordinal}: tracked href does not match "
                "manifest replacement_href"
            )
        if (match.rel or "") != entry["rel_after"]:
            raise PublicationSubstitutionError(
                f"occurrence_ordinal {ordinal}: tracked rel does not match "
                "manifest rel_after"
            )

        original_tag = _patch_anchor_tag_reverse(
            match.tag_text,
            original_href=entry["original_href"],
            rel_before=entry["rel_before"],
        )
        pieces.append(original_tag)
        cursor = match.end

    pieces.append(tracked_html[cursor:])
    return "".join(pieces)


# ==================== strict validation ======================================
def validate_tracked_html(
    *,
    canonical_html: str,
    external_links: Sequence[str],
    canonical_body_hash: str,
    renderer_version: str,
    tracked_html: str,
    manifest: Sequence[Mapping[str, Any]],
) -> None:
    """canonical_html + manifest から独立に tracked_html を再構築し (forward)、
    かつ tracked_html + manifest から canonical_html を再構築できること
    (reverse) の両方を要求する。どちらか一方でも一致しなければ fail closed で
    raise する — 承認されていない差分 (テキスト/属性/injected・removed HTML等)
    はどちらの再構築とも必ず食い違うため検出できる。
    """

    selections = [
        SelectedSubstitution(
            occurrence_ordinal=e["occurrence_ordinal"],
            occurrence_identity_hash=e["occurrence_identity_hash"],
            mapping_id=e["mapping_id"],
            affiliate_link_target_id=e["affiliate_link_target_id"],
            token=e["token"],
            target_projection_version=e["target_projection_version"],
        )
        for e in manifest
    ]

    rebuilt = build_tracked_html(
        canonical_html=canonical_html,
        external_links=external_links,
        canonical_body_hash=canonical_body_hash,
        renderer_version=renderer_version,
        selections=selections,
    )
    if rebuilt.tracked_html != tracked_html:
        raise PublicationSubstitutionError(
            "tracked_html does not match the deterministic reconstruction from "
            "canonical_html + manifest (forward validation failed)"
        )
    if rebuilt.manifest != list(manifest):
        raise PublicationSubstitutionError(
            "manifest does not match the deterministic reconstruction from "
            "canonical_html + selections (forward validation failed)"
        )

    reconstructed_canonical = reverse_tracked_html(
        tracked_html=tracked_html, manifest=manifest
    )
    if reconstructed_canonical != canonical_html:
        raise PublicationSubstitutionError(
            "tracked_html cannot be reversed back to canonical_html using only "
            "the recorded manifest substitutions (reverse validation failed)"
        )
