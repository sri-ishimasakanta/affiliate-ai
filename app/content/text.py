"""語の正規化 (C10-2、pure)。keyword・検索クエリ・発見の候補を同じ規則で比べる。"""

from __future__ import annotations

import hashlib
import json
import unicodedata


def phrase_text(text: str) -> str:
    """表示用の正規化: NFKC・前後の空白を除く・空白を 1 つに。"""

    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def phrase_key(text: str) -> str:
    """同じ語かを決める鍵: NFKC・casefold・空白を除く (「AI 議事録」=「ai議事録」)。"""

    return "".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def stable_hash(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)  # fmt: skip
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = ["phrase_key", "phrase_text", "stable_hash"]
