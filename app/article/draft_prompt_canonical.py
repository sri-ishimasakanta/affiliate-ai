"""DraftPromptPackage の canonical 化と ``prompt_input_hash`` / ``rendered_prompt_hash``
の計算 (pure)。DB / SQLAlchemy / FastAPI 非依存。

* PromptPackage の値は最初から canonical (datetime は UTC 秒精度 ``+00:00`` 文字列)。
* ``prompt_input_hash`` は package の semantic 部分 (トップレベル ``"audit"`` を除外) の
  SHA-256。非意味的値 (built_at 等) は ``audit`` サブツリーに置く約束。
* ``rendered_prompt_hash`` は rendered 文字列そのものの SHA-256。
* :func:`app.article.draft_input_canonical` の canonical helper を再利用する。
"""

from __future__ import annotations

import hashlib

from app.article.draft_input_canonical import canonical_json

_SEMANTIC_EXCLUDED_TOP_KEYS: frozenset[str] = frozenset({"audit"})


def semantic_package_for_hash(package: dict) -> dict:
    """PromptPackage から ``prompt_input_hash`` 対象の意味的部分だけを取り出す。"""

    return {
        k: v for k, v in package.items() if k not in _SEMANTIC_EXCLUDED_TOP_KEYS
    }


def compute_prompt_input_hash(package: dict) -> str:
    """PromptPackage の semantic 部分の SHA-256 hex (64 文字)。"""

    canonical = canonical_json(semantic_package_for_hash(package))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_rendered_prompt_hash(rendered_prompt: str) -> str:
    """rendered_prompt 文字列そのものの SHA-256 hex (64 文字)。"""

    return hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest()
