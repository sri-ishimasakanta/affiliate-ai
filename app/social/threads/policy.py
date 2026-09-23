"""Threads の文体ポリシー (T2)。

閾値と禁止語を **コードに埋めない**。``app/config/threads_style_policy.json`` に置き、
``policy_version`` で「どの基準で作った提案か」を後から再現できるようにする
(C6/C7 の policy と同じ方針)。

ここにあるのは運用上の文体の約束であって、良し悪しの点数ではない。不透明な合成
スコアは作らず、名前の付いた検査を個別に通す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "threads_style_policy.json"


@dataclass(frozen=True)
class ThreadsStylePolicy:
    policy_version: str
    raw: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    def gate(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    def listing(self, name: str) -> tuple[str, ...]:
        value = self.raw.get(name)
        return tuple(str(v) for v in value) if isinstance(value, list) else ()

    @property
    def max_characters(self) -> int:
        return int(self.gate("limits", "max_characters", 500))

    @property
    def preferred_max_characters(self) -> int:
        return int(self.gate("limits", "preferred_max_characters", 380))

    @property
    def max_sentences(self) -> int:
        return int(self.gate("limits", "max_sentences", 8))

    @property
    def preferred_max_sentence_characters(self) -> int:
        return int(self.gate("limits", "preferred_max_sentence_characters", 60))

    @property
    def angles(self) -> tuple[str, ...]:
        return self.listing("angles")

    @property
    def banned_phrases(self) -> tuple[str, ...]:
        return self.listing("banned_phrases")

    @property
    def discouraged_phrases(self) -> tuple[str, ...]:
        return self.listing("discouraged_phrases")

    @property
    def prohibitions(self) -> tuple[str, ...]:
        return self.listing("prohibitions")

    @property
    def allowed_link_modes(self) -> tuple[str, ...]:
        value = self.gate("link", "allowed_modes", [])
        return tuple(str(v) for v in value) if isinstance(value, list) else ()

    @property
    def allowed_link_host(self) -> str:
        return str(self.gate("link", "allowed_host", "bizfluxlab.com"))

    @property
    def forbidden_path_prefixes(self) -> tuple[str, ...]:
        value = self.gate("link", "forbidden_path_prefixes", [])
        return tuple(str(v) for v in value) if isinstance(value, list) else ()

    @property
    def max_emoji(self) -> int:
        return int(self.gate("emoji", "max_count", 2))

    @property
    def max_questions(self) -> int:
        return int(self.gate("tone", "max_questions", 1))

    @property
    def polite_markers(self) -> tuple[str, ...]:
        """文末が機械的に見える語尾。「〜です」と「〜ます」の両方を見る。"""

        value = self.gate("polite_form", "markers", ["です", "ます"])
        return tuple(str(v) for v in value) if isinstance(value, list) else ("です", "ます")

    @property
    def max_consecutive_polite_endings(self) -> int:
        return int(self.gate("polite_form", "max_consecutive_sentence_endings", 3))

    @property
    def factual_rules(self) -> tuple[str, ...]:
        value = self.gate("factual_grounding", "rules", [])
        return tuple(str(v) for v in value) if isinstance(value, list) else ()


def load_policy(path: Path | str | None = None) -> ThreadsStylePolicy:
    target = Path(path) if path is not None else _POLICY_PATH
    document = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("threads style policy must be a JSON object")
    version = document.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("threads style policy must declare a policy_version")
    return ThreadsStylePolicy(policy_version=version, raw=document)


@lru_cache
def get_policy() -> ThreadsStylePolicy:
    return load_policy()
