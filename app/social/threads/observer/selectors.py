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

#: collector-3 (T6.5B.1): 続きの投稿の印を DOM で除く・候補の勘定・並びが落ち着くまで待つ。
COLLECTOR_VERSION = "t6.5b-collector-3"
#: 本物の画面で確かめた版 (T6.5B パイロット)。形を直したら版を上げる。
#: v2 (T6.5B.1): 本文の中の続きの投稿の印 (「1/2」の div) を除く・引用した投稿の中の要素を
#: 外側の投稿として読まない・候補の勘定。For You の dry-run で画面と照らして確認
#: (2026-09-28、5 件 × 5 回)。
SELECTOR_VERSION = "threads-web-verified-2026-09-28-v2"
SELECTOR_VERIFIED = True
#: パイロットで画面と照らして確かめたもの。**ここに無いものは未確認。**
VERIFIED_SURFACES = (
    "for_you_card", "body", "author_handle", "permalink", "post_timestamp", "topic",
    "likes", "replies", "media_image", "thread_marker_exclusion", "reposted_by_header_card",
    "candidate_accounting",
)  # fmt: skip
#: ``quoted_post_scoping`` は画面で問題を見つけ (外側の返信の数に引用の数が入った)、試験で
#: 直したが、直した後の画面ではまだ確かめていない。``meta_ai_label_dom`` は DOM の形を見て
#: いない (分析の層の決まった規則で除く: ``observer.normalize``)。
UNVERIFIED_SURFACES = (
    "reposts", "shares", "quotes", "media_video", "has_link", "quoted_post_scoping",
    "meta_ai_label_dom", "custom_feed_page", "account_page", "search_recent_tab",
    "search_profile_tab", "global_trending_ranking",
)  # fmt: skip

#: 画面 (出どころ) ごとの確かめた状態。**全体の版とは別に記録する。** ここに無い出どころは
#: 未確認 (``surface_verification`` が ``verified=False`` を返す)。
SURFACE_VERIFICATION = {
    "for_you": {
        "version": "threads-web-verified-2026-09-28-v2",
        "verified_fields": ("card", "author_handle", "permalink", "post_timestamp", "body",
                            "analysis_body", "topic", "likes", "replies", "media_image",
                            "candidate_accounting"),
        "evidence": "For You dry-runs 2026-09-28 (T6.5B.1), screenshots cross-checked",
    },
    #: 検索 (「上位検索結果」の画面、既定の ``serp_type=default``)。投稿のまとまりの形は
    #: For You と同じ (時刻のリンクの位置・指標のボタンの形・印が一致)。タブ・検索窓は
    #: まとまりの外。検索語は本文の中の普通の文字 (強調の印は無い)。
    "search": {
        "version": "threads-search-verified-2026-09-28-v1",
        "verified_fields": ("card", "author_handle", "permalink", "post_timestamp", "body",
                            "analysis_body", "topic", "likes", "replies", "candidate_accounting",
                            "search_tabs_outside_cards", "query_text_not_highlighted"),
        "evidence": "Search dry-run 2026-09-28 (query 生成AI, 上位検索結果), screenshots and "
                    "a structure probe cross-checked",
    },
    #: トピックの一覧 = 検索の最初の画面 (``/search``、語なし) の「おすすめのトピック」
    #: (``serp_type=search_nullstate_topic_for_you``)。**Threads は「トレンド」と表示して
    #: いない** (このアカウント向け。世の中の順位ではない)。数も分類も表示されない。
    "trending_list": {
        "version": "threads-topic-list-verified-2026-09-28-v1",
        "verified_fields": ("topic_name", "topic_query", "serp_type", "rank_on_screen",
                            "candidate_accounting", "sidebar_communities_excluded",
                            "account_suggestions_excluded"),
        "evidence": "trending dry-run 2026-09-28 (/search landing), screenshots and a "
                    "structure probe cross-checked",
    },
    #: 一覧のトピックのリンク先 = そのトピックの語の検索結果 (「上位検索結果」)。投稿の
    #: まとまりの形は For You・検索と同じ。ページのトピックは投稿に写さない。
    "trending_topic": {
        "version": "threads-topic-posts-verified-2026-09-28-v1",
        "verified_fields": ("card", "author_handle", "permalink", "post_timestamp", "body",
                            "analysis_body", "post_topic", "likes", "replies", "media_image",
                            "candidate_accounting", "page_topic_not_copied_to_posts"),
        "evidence": "trending dry-run 2026-09-28 (topic ハンドメイド, 5 posts), screenshots "
                    "cross-checked",
    },
}  # fmt: skip


def surface_verification(source_type: str) -> dict:
    """出どころの確かめた状態 (``version`` / ``verified`` / ``verified_fields``)。"""

    entry = SURFACE_VERIFICATION.get(source_type)
    if entry is None:
        return {"version": None, "verified": False, "verified_fields": ()}
    return {"version": entry["version"], "verified": True,
            "verified_fields": entry["verified_fields"]}  # fmt: skip

BASE_URL = "https://www.threads.com"
ALLOWED_HOSTS = ("www.threads.com", "threads.com", "www.threads.net", "threads.net")

# -- 投稿のまとまり・リンク ------------------------------------------------------------
POST_CONTAINER = {"tag": "div", "data_pressable_container": "true"}
PERMALINK_PATTERN = (
    r"^(?:https://www\.threads\.(?:com|net))?/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)"
)
TOPIC_LINK = {"tag": "a", "href__contains": "serp_type=tags"}
#: 検索の最初の画面 (``/search``、語なし) のトピックの一覧 (T6.5B、2026-09-28 に画面で確認)。
#: 各トピックは ``/search?q=<語>&serp_type=search_nullstate_topic_for_you`` へのリンク。
#: **Threads はこれを「トレンド」とは表示していない** (見出しも「トレンド」「話題」の文字も
#: 無い)。``serp_type`` のとおり、このアカウント向けの「おすすめのトピック」。左のメニューの
#: コミュニティ (``serp_type=tags``) は一覧ではない (数えない)。
TOPIC_SUGGESTION_LINK = {"tag": "a", "href__contains": "serp_type=search_nullstate_topic"}
#: ``serp_type`` → 一覧の種類 (保存するときの ``source_type``。24 文字以内)。
TOPIC_LIST_KINDS = {"search_nullstate_topic_for_you": "topic_for_you"}
TOPIC_LIST_KIND_UNKNOWN = "topic_suggestion"
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
    "SURFACE_VERIFICATION", "surface_verification",
    "SELECTOR_VERIFIED", "UNVERIFIED_METRIC_LABELS", "UNVERIFIED_SURFACES", "VERIFIED_METRICS",
    "VERIFIED_SURFACES",
    "SELECTOR_VERSION", "account_url", "custom_feed_url", "for_you_url", "search_url",
    "topic_url", "trends_url",
]  # fmt: skip
