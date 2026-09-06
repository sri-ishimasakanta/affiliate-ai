from enum import StrEnum

# ステータスは DB 上では単純な文字列カラムとして保存する。
# ネイティブの ENUM 型は使わず、PostgreSQL へ移行しても
# ALTER TYPE などのマイグレーションコストが発生しないようにする。


class ArticleStatus(StrEnum):
    """記事のライフサイクル状態。"""

    IDEA = "idea"
    PLANNED = "planned"
    DRAFTING = "drafting"
    REVIEW = "review"
    APPROVED = "approved"
    PUBLISHED = "published"
    REWRITE = "rewrite"
    ARCHIVED = "archived"


class KeywordStatus(StrEnum):
    """キーワードの選定プロセス上の状態。"""

    DISCOVERED = "discovered"
    ANALYZED = "analyzed"
    SELECTED = "selected"
    ASSIGNED = "assigned"
    REJECTED = "rejected"


class AffiliateProgramStatus(StrEnum):
    """アフィリエイトプログラムの稼働状態。"""

    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"
    UNKNOWN = "unknown"


class AffiliateLinkTargetStatus(StrEnum):
    """AffiliateLinkTarget の状態 (control-plane)。

    active    : 将来 runtime projection の対象になりうる
    disabled  : 意図的に利用不可
    superseded: より新しい immutable target に置き換えられた
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    SUPERSEDED = "superseded"


class KeywordSignalComponent(StrEnum):
    """Keyword Signal が根拠となる Opportunity Score の component。

    値は Opportunity Score V1 の component 名
    (:data:`app.keyword.scoring.COMPONENT_NAMES`) と完全に一致させる。
    """

    SEARCH_DEMAND = "search_demand"
    COMMERCIAL_INTENT = "commercial_intent"
    AFFILIATE_OPPORTUNITY = "affiliate_opportunity"
    COMPETITION_EASE = "competition_ease"
    TREND = "trend"
    ORIGINALITY = "originality"
    SITE_RELEVANCE = "site_relevance"
