"""生成バックエンドの adapter (§40/§42)。

V1 で実行可能なのは :class:`ManualAdapter` のみ。API / local CLI adapter は
interface / stub だけ用意し、実際の外部呼び出しはしない (追加実費 0 円方針, §43)。

adapter は「rendered_prompt を受け取り raw_output を返す」だけ。DB / transaction は
Service が持つ。外部 network call を DB transaction 内で行わない (§56)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

_SECRET_MARKERS = (
    "authorization:",
    "x-api-key",
    "bearer ",
    "basic ",
    "sk-ant-",
    "sk-",
    "api_key=",
    "apikey=",
    "app_password",
    "application password",
    "wordpress_app_password",
    "x-wp-nonce",
)


@dataclass
class AdapterResult:
    raw_output: str
    token_usage: dict | None = None
    provider_meta: dict = field(default_factory=dict)


class DraftGenerationAdapter(Protocol):
    """rendered prompt → raw_output。実装は secret を返り値に含めない。"""

    mode: str

    def is_synchronous(self) -> bool:
        """True なら execute 内で結果まで取得。False (manual) なら別途 submit-result。"""
        ...

    def generate(self, rendered_prompt: str, parameters: dict | None) -> AdapterResult:
        ...


class ManualAdapter:
    """外部呼び出しをしない。execute は run を running にするだけで、実生成は Human が
    行い ``submit-result`` で戻す。
    """

    mode = "manual"

    def is_synchronous(self) -> bool:
        return False

    def generate(self, rendered_prompt: str, parameters: dict | None) -> AdapterResult:
        raise NotImplementedError(
            "ManualAdapter does not generate; the Human runs the prompt externally "
            "and posts the result via submit-result"
        )


def sanitize_provider_error(message: object) -> str:
    """provider 由来のエラー文字列から secret 様の行を除去し截断する (§57)。"""

    text = str(message)
    safe_lines: list[str] = []
    for line in text.splitlines():
        low = line.lower()
        if any(marker in low for marker in _SECRET_MARKERS):
            safe_lines.append("[redacted line]")
        else:
            safe_lines.append(line)
    out = "\n".join(safe_lines)
    return out[:2000]
