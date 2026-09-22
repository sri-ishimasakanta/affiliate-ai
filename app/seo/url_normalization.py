"""公開 URL の比較用正規化 (C5.1)。

DB の ``Article.published_url``、sitemap の ``<loc>``、HTML の canonical、Search
Console の ``page`` は、同じページを指していても表記が揺れる:

- scheme / host の大小文字
- ``www.`` の有無
- 末尾スラッシュの有無
- 日本語 slug の percent-encoding (記事 #1 が該当) と大文字/小文字の 16 進表記

この module は **比較のためだけ** の正規化キーを作る。元の URL 文字列は決して
書き換えない -- 呼び出し側は常に元の URL を保持し、突き合わせにだけキーを使う。

**意図的に吸収しないもの** (別 URL は別 URL のまま扱う):

- path の中身の違い (1 文字でも違えば別キー)
- query string / fragment (付いていればキーに残す)
- host が別ドメインの場合

``www.`` の有無だけは、canonical host を明示的に渡されたときに限って吸収する
(このサイトの canonical は non-www HTTPS で、www は 301 で寄せられている)。
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit, urlunsplit


def canonical_host(host: str) -> str:
    """比較用のホスト表記 (小文字化し、先頭の ``www.`` を落とす)。"""

    host = (host or "").strip().lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url_key(url: str | None) -> str | None:
    """同一ページ判定に使う正規化キー。判定できなければ ``None``。

    scheme は落とす (http/https は 301 で寄せられており、同じページを指す)。
    host は小文字化して ``www.`` を落とす。path は percent-decode して比較し、
    末尾スラッシュの有無を吸収する。query / fragment は保持する。
    """

    if not url or not url.strip():
        return None
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return None
    host = canonical_host(parts.netloc)
    # percent-encoding の揺れ (%E6 と %e6、encode の有無) を decode で吸収する。
    path = unquote(parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    if not path:
        path = "/"
    key = urlunsplit(("", host, path, parts.query, ""))
    return key.lstrip("/") if key.startswith("//") else key


def same_url(left: str | None, right: str | None) -> bool:
    """2 つの URL が同じページを指すか (どちらかが解釈不能なら ``False``)。"""

    a = normalize_url_key(left)
    b = normalize_url_key(right)
    return a is not None and a == b
