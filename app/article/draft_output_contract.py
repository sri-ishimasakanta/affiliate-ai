"""LLM 出力の構造化 contract の parse (pure)。

期待する出力 (§45):
    {"meta_description": "...", "body_markdown": "...", "generation_notes": [...]}

- ``title`` は出力させない (Article.title authoritative)。
- parse 不能 / required 欠落 / body 空 は :class:`DraftContractError` (呼び出し側で run failed)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class DraftContractError(ValueError):
    """出力が structured contract を満たさない (parse/required の失敗)。"""


@dataclass
class ParsedDraft:
    meta_description: str
    body_markdown: str
    generation_notes: list[str] = field(default_factory=list)


def _strip_code_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = _FENCE_RE.sub("", s)
        # 末尾フェンス
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def parse_draft_output(raw: str) -> ParsedDraft:
    if not isinstance(raw, str) or not raw.strip():
        raise DraftContractError("output is empty")

    text = _strip_code_fence(raw)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DraftContractError(f"output is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise DraftContractError("output JSON root must be an object")

    meta = obj.get("meta_description")
    body = obj.get("body_markdown")
    notes = obj.get("generation_notes", [])

    if not isinstance(meta, str) or not meta.strip():
        raise DraftContractError("meta_description must be a non-empty string")
    if not isinstance(body, str) or not body.strip():
        raise DraftContractError("body_markdown must be a non-empty string")
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise DraftContractError("generation_notes must be a list of strings")

    return ParsedDraft(
        meta_description=meta.strip(),
        body_markdown=body,
        generation_notes=[n for n in notes],
    )
