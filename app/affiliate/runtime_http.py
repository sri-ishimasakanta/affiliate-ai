"""affiliate runtime (WordPress MU-plugin) 向け HTTP client の共有プリミティブ。

projection push client と click export client の両方が使う、狭くて安定した 2 つだけ:

* :func:`require_https_origin` — ``Settings.wordpress_base_url`` を「WordPress origin
  のみ」(``https://host[:port]``) に制限する実行境界チェック。
* :func:`known_server_code` — サーバの ``{"error":{"code": ...}}`` から、呼び出し側が
  渡した allowlist に含まれる code だけを安全に取り出す (raw body は返さない)。

汎用 URL パーサや汎用 JSON 正規化は作らない。private helper を跨いで import させない
ためだけの最小抽出。
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx


def require_https_origin(base_url: str, *, error_cls: type[Exception]) -> str:
    """base URL を ``https://host[:port]`` へ縮約して返す。不適なら ``error_cls`` を raise。

    scheme は https 固定 / userinfo 不可 / host 必須 / query・fragment・path を持たない。
    署名対象の固定 endpoint path は呼び出し側が付ける (caller は path を差し替えられない)。
    """

    parts = urlsplit((base_url or "").strip())
    if parts.scheme.lower() != "https":
        raise error_cls("affiliate runtime base URL must be https")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise error_cls("affiliate runtime base URL must not contain userinfo")
    if not parts.hostname:
        raise error_cls("affiliate runtime base URL has no host")
    if parts.query or parts.fragment:
        raise error_cls(
            "affiliate runtime base URL must not carry a query or fragment"
        )
    if parts.path not in ("", "/"):
        raise error_cls("affiliate runtime base URL must be an origin only (no path)")
    return f"https://{parts.netloc.lower()}"


def known_server_code(
    response: httpx.Response, allowed: frozenset[str]
) -> str | None:
    """``{"error":{"code": ...}}`` の code が ``allowed`` に含まれるときだけ返す。

    JSON でない / dict でない / code が未知 なら ``None`` (raw body は決して返さない)。
    """

    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    code = err.get("code") if isinstance(err, dict) else None
    return code if isinstance(code, str) and code in allowed else None
