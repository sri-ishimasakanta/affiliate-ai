"""本文の書き換え・メタディスクリプションの変更の提案の規則 (C10-3 / C10-E、pure)。

**人が書いた** 新しい本文 / メタディスクリプションだけを扱う (生成しない)。

本文の書き換え (``text_edit``) が守ること:

- 既にあるリンクの行き先 (href) は **全部そのまま** (追跡のリンク ``/go/`` を含む。消さない・
  変えない)。新しいリンクは足さない (内部リンクの挿入は既存の add_internal_link の流れ)。
- 冒頭 (700 字) の PR 表記 (広告・PR 等) があれば残す。
- 空の本文・変わらない本文は提案にしない。
- 差分 (unified diff) と変わった行の数を記録に残す。

メタディスクリプション (``meta_description``): 空にしない。長さの目安 (20〜220 字、推奨 60〜160 字)
は既存の下書きの検査と同じ。変わらないものは提案にしない。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass

_LINK = re.compile(r"\]\(([^)\s]+)")  # markdown のリンクの行き先
_HREF = re.compile(r"href=[\"']([^\"']+)[\"']")
_PR_MARKERS = ("PR", "広告", "アフィリエイト", "プロモーション", "スポンサー")
_PR_HEAD = 700
META_MIN = 20
META_MAX = 220
META_RECOMMENDED = (60, 160)


def links_of(body: str) -> list[str]:
    return sorted(_LINK.findall(body or "") + _HREF.findall(body or ""))


def _has_disclosure(body: str) -> bool:
    head = (body or "")[:_PR_HEAD]
    return any(m in head for m in _PR_MARKERS)


@dataclass(frozen=True)
class TextEditCheck:
    ok: bool
    problems: tuple[str, ...]
    added_lines: int
    removed_lines: int
    diff: str

    def as_dict(self) -> dict:
        return {**asdict(self), "problems": list(self.problems)}


def check_text_edit(current: str, proposed: str) -> TextEditCheck:
    problems = []
    if not (proposed or "").strip():
        problems.append("the proposed body is empty")
    if (proposed or "") == (current or ""):
        problems.append("the proposed body is identical to the current body")
    before, after = links_of(current), links_of(proposed)
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    if removed or len(after) < len(before):
        problems.append("existing links must stay unchanged (removed or changed: "
                        + ", ".join(removed[:5] or ["a duplicate occurrence"]) + ")")
    if added:
        problems.append("a text edit must not add links (use the internal link flow): "
                        + ", ".join(added[:5]))
    if _has_disclosure(current) and not _has_disclosure(proposed):
        problems.append("the PR disclosure near the top must stay")
    diff_lines = list(difflib.unified_diff((current or "").splitlines(),
                                           (proposed or "").splitlines(), "current", "proposed",
                                           lineterm=""))
    added_n = sum(1 for d in diff_lines if d.startswith("+") and not d.startswith("+++"))
    removed_n = sum(1 for d in diff_lines if d.startswith("-") and not d.startswith("---"))
    return TextEditCheck(not problems, tuple(problems), added_n, removed_n,
                         "\n".join(diff_lines))


@dataclass(frozen=True)
class MetaCheck:
    ok: bool
    problems: tuple[str, ...]
    warnings: tuple[str, ...]
    length: int

    def as_dict(self) -> dict:
        return {**asdict(self), "problems": list(self.problems), "warnings": list(self.warnings)}


def check_meta_description(current: str | None, proposed: str) -> MetaCheck:
    text = (proposed or "").strip()
    problems, warnings = [], []
    if not text:
        problems.append("the proposed meta description is empty")
    if text == (current or "").strip():
        problems.append("the proposed meta description is identical to the current one")
    if text and not META_MIN <= len(text) <= META_MAX:
        problems.append(f"length {len(text)} is outside {META_MIN}-{META_MAX}")
    lo, hi = META_RECOMMENDED
    if text and not lo <= len(text) <= hi:
        warnings.append(f"length {len(text)} is outside the recommended {lo}-{hi}")
    if "<" in text or ">" in text:
        problems.append("HTML is not allowed in a meta description")
    return MetaCheck(not problems, tuple(problems), tuple(warnings), len(text))


__all__ = ["META_MAX", "META_MIN", "MetaCheck", "TextEditCheck", "check_meta_description",
           "check_text_edit", "links_of"]
