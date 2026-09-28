"""Threads の Web 画面の読み方 (版つき)。

Threads の画面は変わる。ここにある形と違えば、取り出しは止まる (fail closed)。値を推測で
読み替えない。画面が変わったら、本物の画面と照らして版を上げて直す。

**確かめた範囲** (T6.5B パイロット、2026-09-28、For You、ログイン済み、5 件を画面と照合):
投稿のまとまり・本文・投稿者・permalink (投稿のキー)・投稿時刻・トピック・いいね・返信・
画像の有無。それ以外 (再投稿・共有・引用の数、動画、外部リンク、検索・トレンド・カスタム
フィード・アカウントの画面) は **まだ確かめていない** (``VERIFIED_SURFACES`` 参照)。
"""

from __future__ import annotations

from urllib.parse import quote

COLLECTOR_VERSION = "t6.5b-collector-2"
#: 本物の画面で確かめた版 (T6.5B パイロット)。形を直したら版を上げる。
SELECTOR_VERSION = "threads-web-verified-2026-09-28-v1"
SELECTOR_VERIFIED = True
#: パイロットで画面と照らして確かめたもの。**ここに無いものは未確認。**
VERIFIED_SURFACES = (
    "for_you_card", "body", "author_handle", "permalink", "post_timestamp", "topic",
    "likes", "replies", "media_image",
)  # fmt: skip
UNVERIFIED_SURFACES = (
    "reposts", "shares", "quotes", "media_video", "has_link", "search_page", "trending_page",
    "topic_page", "custom_feed_page", "account_page",
)  # fmt: skip

BASE_URL = "https://www.threads.com"
ALLOWED_HOSTS = ("www.threads.com", "threads.com", "www.threads.net", "threads.net")

# -- 投稿のまとまり・リンク ------------------------------------------------------------
POST_CONTAINER = {"tag": "div", "data_pressable_container": "true"}
PERMALINK_PATTERN = (
    r"^(?:https://www\.threads\.(?:com|net))?/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)"
)
TOPIC_LINK = {"tag": "a", "href__contains": "serp_type=tags"}
EXTERNAL_LINK_PREFIXES = ("https://l.threads.com/", "https://l.threads.net/")
LOGIN_MARKERS = ("/login",)

#: 指標のアイコン (svg の ``title`` 属性 / ``<title>`` / ``aria-label``) → 指標の名前。
#: 数はアイコンと同じ ``role=button`` の中の文字。**0 のときは数が表示されない** (空) ので、
#: 空は ``None`` のまま (0 と決めつけない)。
#:
#: パイロット (2026-09-28、For You) で画面と照らして確かめたのは **いいね・返信だけ**。
#: 再投稿の数 (引用を含むかどうかが画面からは分からない)・共有 (数が出ない)・引用 (別に
#: 出ない) は **読まない** (``None``)。確かめたら ``METRIC_LABELS`` に足す。
METRIC_LABELS = {
    "「いいね！」": "likes", "いいね！": "likes", "いいね!": "likes", "いいね": "likes",
    "Like": "likes",
    "返信": "replies", "Reply": "replies",
}  # fmt: skip
VERIFIED_METRICS = ("likes", "replies")
#: 画面にアイコンはあるが、意味を確かめていない / 数が出ない (読まない)。
UNVERIFIED_METRIC_LABELS = ("再投稿", "シェアする")
#: 投稿のまとまりが描かれるまで待つ上限 (見つからなくても止めない。判定は parser)。
RENDER_WAIT_MS = 15_000

# -- 収集の上限 (パイロットは控えめに) ----------------------------------------------------
LIMITS = {
    "for_you": 20,
    "search": 10,
    "trending_topic": 5,
    "trending_topics_max": 5,
    "custom_feed": 10,
    "known_account": 10,
    "run_total": 60,
    #: スクロールの回数の上限 (無限にスクロールしない)。
    "max_scrolls": 3,
}


def for_you_url() -> str:
    return f"{BASE_URL}/"


def search_url(query: str) -> str:
    return f"{BASE_URL}/search?q={quote(query)}&serp_type=default"


def topic_url(topic: str) -> str:
    return f"{BASE_URL}/search?q={quote(topic)}&serp_type=tags"


def trends_url() -> str:
    return f"{BASE_URL}/search"


def custom_feed_url(feed_id: str) -> str:
    return f"{BASE_URL}/custom_feed/{quote(feed_id)}"


def account_url(handle: str) -> str:
    return f"{BASE_URL}/@{quote(handle.lstrip('@'))}"


__all__ = [
    "ALLOWED_HOSTS", "COLLECTOR_VERSION", "LIMITS", "METRIC_LABELS", "RENDER_WAIT_MS",
    "SELECTOR_VERIFIED", "UNVERIFIED_METRIC_LABELS", "UNVERIFIED_SURFACES", "VERIFIED_METRICS",
    "VERIFIED_SURFACES",
    "SELECTOR_VERSION", "account_url", "custom_feed_url", "for_you_url", "search_url",
    "topic_url", "trends_url",
]  # fmt: skip
