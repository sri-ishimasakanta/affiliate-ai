"""外の投稿の本文の「分析用の本文」(T6.5B.1、決定的・版つき)。**保存した本文は変えない。**

- ``raw_body`` = 集めたときに保存した本文 (証拠。書き換えない)。
- ``analysis_body`` = 画面の部品を決まった規則で除いた本文 (特徴の計算に使う)。

規則 (``threads-body-normalizer-1``。どれも位置と形が決まったものだけ。広い置き換えはしない):

1. ``thread_marker``: 本文の **末尾** の「NBSP + 数 / 数」(例 ``\\xa01/2``)。続きの投稿の印の
   ``div`` が本文に混ざった形 (collector-2 まで)。collector-3 からは DOM の段階で入らない。
   書いた人の「1/2の確率」「確率は1/2」は、末尾でない・NBSP が前に無いので残る。
2. ``meta_ai_label``: 本文の **先頭** の ``meta.ai`` という札 (空白か改行が続くとき)。
   画面では青い「meta.ai」の札 (2026-09-28 の保存つきのパイロットで確認)。DOM の形は
   まだ見ていないので、DOM の段階では除いていない。**先頭が本当に ``meta.ai`` で始まる
   投稿の文字も除かれる** (残る危険。印 ``meta_ai_label_removed`` で分かる)。

除いた後にまだ部品の形が残っていれば ``text_contaminated`` (意味の分析から外す)。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

NORMALIZER_VERSION = "threads-body-normalizer-1"

FLAG_THREAD_MARKER_REMOVED = "thread_marker_removed"
FLAG_META_AI_LABEL_REMOVED = "meta_ai_label_removed"

_THREAD_MARKER_TAIL = re.compile(r" \d+\s*/\s*\d+\Z")
_META_AI_HEAD = re.compile(r"\Ameta\.ai(?:[ \t ]+|\n+)")

#: 部品が残っているかの確認 (除いた後の本文に対して)。
_RESIDUE = (_THREAD_MARKER_TAIL, _META_AI_HEAD)

TEXT_CLEAN = "text_clean"
TEXT_UI_CHROME_REMOVED = "ui_chrome_removed"
TEXT_CONTAMINATED = "text_contaminated"


@dataclass(frozen=True)
class NormalizedBody:
    raw_body: str
    analysis_body: str
    raw_body_hash: str
    normalized_body_hash: str
    normalization_version: str
    normalization_flags: tuple[str, ...]
    text_quality: str

    def as_dict(self) -> dict:
        return {
            "raw_body_hash": self.raw_body_hash,
            "normalized_body_hash": self.normalized_body_hash,
            "normalization_version": self.normalization_version,
            "normalization_flags": list(self.normalization_flags),
            "text_quality": self.text_quality,
        }


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_body(raw: str) -> NormalizedBody:
    text = raw or ""
    flags: list[str] = []
    stripped = _THREAD_MARKER_TAIL.sub("", text)
    if stripped != text:
        flags.append(FLAG_THREAD_MARKER_REMOVED)
        text = stripped.rstrip()
    stripped = _META_AI_HEAD.sub("", text, count=1)
    if stripped != text:
        flags.append(FLAG_META_AI_LABEL_REMOVED)
        text = stripped.lstrip()
    if any(p.search(text) for p in _RESIDUE) or not text.strip():
        quality = TEXT_CONTAMINATED
    else:
        quality = TEXT_UI_CHROME_REMOVED if flags else TEXT_CLEAN
    return NormalizedBody(
        raw_body=raw or "",
        analysis_body=text,
        raw_body_hash=_hash(raw or ""),
        normalized_body_hash=_hash(text),
        normalization_version=NORMALIZER_VERSION,
        normalization_flags=tuple(flags),
        text_quality=quality,
    )


__all__ = ["FLAG_META_AI_LABEL_REMOVED", "FLAG_THREAD_MARKER_REMOVED", "NORMALIZER_VERSION",
           "TEXT_CLEAN", "TEXT_CONTAMINATED", "TEXT_UI_CHROME_REMOVED", "NormalizedBody",
           "normalize_body"]  # fmt: skip
