"""Threads からサイトへの流入を後で見分けるための UTM (T1 で定義、T3 で使用)。

**まだ何も付けない。** ここで決めておくのは、T3 が計測を組むときに使う決定的な
規則だけである。規則を後から変えると、過去の流入と比較できなくなるので先に固定する。

- ``utm_source`` は ``threads`` 固定 (プラットフォーム名)。
- ``utm_medium`` は ``social`` 固定 (有料流入と混ぜない)。
- ``utm_campaign`` / ``utm_content`` は **決定的に** 作る。同じ投稿からは必ず同じ
  値が出る。連番や時刻を混ぜない。

GA4 側では ``utm_source=threads`` で絞り込める。Threads API 側の ``clicks`` 指標
(アカウント単位) とは別物なので、同じ数字として扱わない。
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

UTM_SOURCE = "threads"
UTM_MEDIUM = "social"


def build_campaign(*, article_id: int, angle: str) -> str:
    """``article-21-insight`` のような決定的なキャンペーン名。"""

    return f"article-{int(article_id)}-{_slug(angle)}"


def build_content(*, proposal_hash: str) -> str:
    """どの提案から出た投稿かを示す決定的な値 (承認された内容 1 つに対応)。"""

    return (proposal_hash or "")[:16]


def decorate(url: str, *, article_id: int, angle: str, proposal_hash: str) -> str:
    """記事 URL に UTM を付ける。既存のクエリは壊さない。

    同じ (記事, 切り口, 提案) からは必ず同じ URL が出る。
    """

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(
        {
            "utm_source": UTM_SOURCE,
            "utm_medium": UTM_MEDIUM,
            "utm_campaign": build_campaign(article_id=article_id, angle=angle),
            "utm_content": build_content(proposal_hash=proposal_hash),
        }
    )
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(sorted(query.items())), parts.fragment)
    )


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (value or "").lower()).strip("-")
