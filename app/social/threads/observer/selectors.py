"""Threads の Web 画面の読み方 (版つき)。**まだ本物の画面で確かめていない下書き。**

Threads の画面は変わる。ここにある形と違えば、取り出しは止まる (fail closed)。値を推測で
読み替えない。手動の試験 (パイロット) で本物の画面と照らし、合わなければ版を上げて直す。
"""

from __future__ import annotations

from urllib.parse import quote

COLLECTOR_VERSION = "t6.5b-collector-1"
#: 本物の画面で確かめたら ``-verified`` を付けた版に上げる。
SELECTOR_VERSION = "threads-web-draft-2026-09-28"
SELECTOR_VERIFIED = False

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

#: 指標のアイコン (svg の aria-label) → 指標の名前。
#: **引用 (quotes) は画面に別に出ないので読まない。**
METRIC_LABELS = {
    "いいね!": "likes", "いいね！": "likes", "いいね": "likes", "Like": "likes",
    "返信": "replies", "Reply": "replies", "Comment": "replies",
    "再投稿": "reposts", "リポスト": "reposts", "Repost": "reposts",
    "シェア": "shares", "Share": "shares",
}  # fmt: skip

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
    "ALLOWED_HOSTS", "COLLECTOR_VERSION", "LIMITS", "METRIC_LABELS", "SELECTOR_VERIFIED",
    "SELECTOR_VERSION", "account_url", "custom_feed_url", "for_you_url", "search_url",
    "topic_url", "trends_url",
]  # fmt: skip
