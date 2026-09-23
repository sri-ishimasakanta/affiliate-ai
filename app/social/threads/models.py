"""Threads API の定数と形 (T1、pure)。

ここに書いてある **エンドポイント・パラメータ名・メトリクス名は、2026-09-24 時点の
公式ドキュメント (developers.facebook.com/documentation/threads) で確認したもの**
である。推測で足したものは無い。公式に存在しない指標を作らない。

確認した事実:

- 投稿は 2 段階。まずコンテナを作り、次に公開する。
    ``POST /{threads-user-id}/threads``          -- media_type=TEXT, text
    ``POST /{threads-user-id}/threads_publish``  -- creation_id
- テキスト投稿は **500 文字まで**。
- コンテナ作成から公開まで **平均 30 秒待つことが推奨**されている。
- プロフィールあたり **24 時間で 250 投稿** まで。
- media insights の指標: views / likes / replies / reposts / quotes / shares
- user insights の指標: views / likes / replies / reposts / quotes / clicks /
  followers_count / follower_demographics
  (``follower_demographics`` は since/until と併用できない)
- access token は短命 1 時間、長期 60 日。長期は 24 時間以上経過していれば更新でき、
  60 日更新しなければ失効して復活できない。

未確認 (公式ドキュメントに明記が見つからなかったもの) は
:data:`UNVERIFIED_FACTS` に明示する。埋め合わせに推測を書かない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# -- host / version ------------------------------------------------------------
#: 公式の投稿ドキュメントが例示しているホスト。
DEFAULT_API_BASE_URL = "https://graph.threads.net"
DEFAULT_API_VERSION = "v1.0"
#: OAuth のトークン交換だけは別ホストが案内されている (移行途中と思われる)。
OAUTH_AUTHORIZE_HOST = "https://threads.com/oauth/authorize"
OAUTH_TOKEN_HOST = "https://graph.threads.com/oauth/access_token"

# -- scopes --------------------------------------------------------------------
SCOPE_BASIC = "threads_basic"
SCOPE_CONTENT_PUBLISH = "threads_content_publish"
SCOPE_READ_REPLIES = "threads_read_replies"
SCOPE_MANAGE_REPLIES = "threads_manage_replies"
SCOPE_MANAGE_INSIGHTS = "threads_manage_insights"
SCOPES = (
    SCOPE_BASIC,
    SCOPE_CONTENT_PUBLISH,
    SCOPE_READ_REPLIES,
    SCOPE_MANAGE_REPLIES,
    SCOPE_MANAGE_INSIGHTS,
)
#: T2/T3 が実際に必要とする最小の組み合わせ。
REQUIRED_SCOPES = (SCOPE_BASIC, SCOPE_CONTENT_PUBLISH, SCOPE_MANAGE_INSIGHTS)

# -- publishing ----------------------------------------------------------------
MEDIA_TYPE_TEXT = "TEXT"
#: 公式: "Text posts are limited to 500 characters."
TEXT_MAX_LENGTH = 500
#: 公式: "recommended to wait on average 30 seconds before publishing".
RECOMMENDED_PUBLISH_DELAY_SECONDS = 30
#: 公式: "Profiles are limited to 250 published posts within a 24-hour period."
DAILY_PUBLISH_LIMIT = 250

# -- fields / metrics ----------------------------------------------------------
#: 1 件の投稿から読める項目 (公式一覧のうち、T3 で使う見込みのものだけ)。
MEDIA_FIELDS = ("id", "permalink", "timestamp", "text", "media_type", "shortcode", "username")
#: プロフィール確認に使う項目。
PROFILE_FIELDS = ("id", "username")

#: 投稿単位の指標 (公式)。
MEDIA_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
#: アカウント単位の指標 (公式)。
USER_METRICS = (
    "views",
    "likes",
    "replies",
    "reposts",
    "quotes",
    "clicks",
    "followers_count",
    "follower_demographics",
)
#: since/until と併用できない指標 (公式の注記)。
METRICS_WITHOUT_TIME_RANGE = ("follower_demographics",)

# -- tokens --------------------------------------------------------------------
SHORT_LIVED_TOKEN_HOURS = 1
LONG_LIVED_TOKEN_DAYS = 60
LONG_LIVED_REFRESH_MIN_AGE_HOURS = 24
GRANT_EXCHANGE = "th_exchange_token"
GRANT_REFRESH = "th_refresh_token"

#: 公式ドキュメントで確認できなかったこと。埋めずに、確認済みと区別して残す。
UNVERIFIED_FACTS = (
    "access token を Authorization ヘッダで送れるかどうかは、読んだ範囲の公式"
    "ドキュメントに明記が無い。確認できた例はすべて access_token をリクエスト"
    "パラメータとして渡している。ここではその確認済みの形だけを使う。",
    "投稿数 (250/24h) 以外の細かいレート制限は、読んだ範囲では明記されていない。",
    "投稿ホストは graph.threads.net、OAuth のトークン交換は graph.threads.com と"
    "案内されている。移行途中の可能性があるため base URL は設定可能にしてある。",
)


@dataclass(frozen=True)
class ThreadsProfile:
    """接続確認で読むアカウント情報 (最小)。"""

    user_id: str
    username: str | None = None

    def as_dict(self) -> dict:
        return {"user_id": self.user_id, "username": self.username}


@dataclass(frozen=True)
class ThreadsContainer:
    """コンテナ作成の結果。まだ公開されていない。"""

    creation_id: str


@dataclass(frozen=True)
class ThreadsPublication:
    """公開された投稿。"""

    media_id: str
    permalink: str | None = None
    published_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "media_id": self.media_id,
            "permalink": self.permalink,
            "published_at": self.published_at,
        }


@dataclass(frozen=True)
class ThreadsInsights:
    """指標の読み取り結果。**公式に存在する名前しか入れない。**

    取得できなかった指標は 0 で埋めず、``values`` に現れないままにする
    (「観測できなかった」と「0 だった」を混同しない)。
    """

    subject: str
    values: dict[str, Any] = field(default_factory=dict)
    missing: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"subject": self.subject, "values": dict(self.values), "missing": list(self.missing)}


def text_within_limit(text: str) -> bool:
    return 0 < len(text or "") <= TEXT_MAX_LENGTH


def validate_metrics(requested, allowed) -> tuple[str, ...]:
    """公式に存在する指標だけを残す (知らない名前を API へ投げない)。"""

    allowed_set = set(allowed)
    return tuple(m for m in requested if m in allowed_set)
