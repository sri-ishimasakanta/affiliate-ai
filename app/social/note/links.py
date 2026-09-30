"""note の 1 本から、関係する WordPress の記事を探す (N2 の配布。pure)。

- 候補は公開済みの記事の URL だけ。アフィリエイトの転送 (``/go/``) やトラッキングの URL は
  扱わない (C11 まで変えない)。
- 近さは文字の 3-gram の重なり (記事の題名と主な語が、note の本文にどれだけ含まれるか)。
  点数は並べるためだけ。リンクを入れるのは人が本文を直すとき (承認の対象)。
- 付け方の約束 (``none`` / ``utm``): ``utm`` は note からの流入を見分けるための印。公開の
  リンクに付けるかは人が決める (方針の ``link_convention``。既定は ``none``)。
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.social.note.safety import shingles

CONVENTIONS = ("none", "utm")
MIN_SCORE = 0.15


def _is_tracking(url: str) -> bool:
    path = urlparse(url).path
    return "/go/" in path or path.startswith("/go")


def related_articles(text: str, articles: Iterable[dict], *, limit: int = 3) -> list[dict]:
    """``articles``: ``{"id", "title", "url", "keyword"}``。近い順 (同点は id)。"""

    body = shingles(text)
    out = []
    for a in articles:
        url = a.get("url")
        if not url or _is_tracking(url):
            continue
        probe = shingles(f"{a.get('title') or ''}{a.get('keyword') or ''}")
        if not probe:
            continue
        score = round(len(probe & body) / len(probe), 3)
        if score >= MIN_SCORE:
            out.append({"article_id": a["id"], "title": a.get("title"), "url": url,
                        "score": score})  # fmt: skip
    out.sort(key=lambda r: (-r["score"], r["article_id"]))
    return out[:limit]


def tagged_url(url: str, *, convention: str, campaign: str) -> str:
    """約束に従った URL。``none`` はそのまま。既にある印は上書きしない。"""

    if convention not in CONVENTIONS:
        raise ValueError(f"link convention must be one of {CONVENTIONS}")
    if _is_tracking(url):
        raise ValueError("affiliate / tracking URLs are never tagged (C11)")
    if convention == "none":
        return url
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in (("utm_source", "note"), ("utm_medium", "referral"),
                       ("utm_campaign", campaign)):  # fmt: skip
        query.setdefault(key, value)
    return urlunparse(parts._replace(query=urlencode(query)))


__all__ = ["CONVENTIONS", "related_articles", "tagged_url"]
