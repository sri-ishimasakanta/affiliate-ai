"""記事の **データ成熟度** の決定的な判定 (C6)。

この module の存在理由はただ 1 つ:

    「指標がゼロであること」と「成績が悪いこと」は別物である。

2026-09-22 に公開した 24 本の記事の表示回数がゼロなのは、性能の問題ではなく
**レポートがまだ追いついていない** だけである。成熟度を先に判定し、成熟して
いない記事には性能系の推奨を一切出さない。

検索 (Search Console) と エンゲージメント (GA4) は別の出所なので、成熟度も
**別々に** 持つ。片方が成熟しても、もう片方の判断材料にはならない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# -- 検索データの成熟度 --------------------------------------------------------
#: 公開直後。評価そのものを行わない。
SEARCH_NEWLY_PUBLISHED = "newly_published"
#: 十分古いが、Google がまだインデックスを報告していない。
SEARCH_AWAITING_INDEX_DISCOVERY = "awaiting_index_discovery"
#: インデックス済み (あるいは判定不能) だが、検索データが公開日に追いついていない。
SEARCH_INDEXED_AWAITING_DATA = "indexed_awaiting_search_data"
#: データは届いているが、表示回数が評価に足りない。
SEARCH_INSUFFICIENT_IMPRESSIONS = "insufficient_impressions"
#: 表示回数が十分にあり、CTR / 掲載順位の議論ができる。
SEARCH_SUFFICIENT_SAMPLE = "sufficient_search_sample"

# -- エンゲージメントデータの成熟度 --------------------------------------------
#: GA4 が未設定、または日次行がまだ無い。
ENGAGEMENT_AWAITING_DATA = "awaiting_ga4_data"
#: 行はあるが、オーガニックセッションが評価に足りない。
ENGAGEMENT_INSUFFICIENT_SAMPLE = "insufficient_engagement_sample"
#: オーガニックセッションが十分にある。
ENGAGEMENT_SUFFICIENT_SAMPLE = "sufficient_engagement_sample"

#: Google が「インデックス済み」と報告している状態 (C5.1 の語彙)。
_INDEXED = "GSC_INDEXED"
#: 「取得できていない」= 未インデックスの証拠ではない (C5.1 の語彙)。
_UNKNOWN = "GSC_UNKNOWN"


@dataclass(frozen=True)
class ArticleMaturity:
    """1 記事の成熟度。判定の根拠になった事実も併せて持つ。"""

    article_id: int
    age_days: int | None
    search_state: str
    engagement_state: str
    search_reason: str
    engagement_reason: str
    search_data_covers_article: bool
    impressions: int
    organic_sessions: int

    @property
    def search_sample_sufficient(self) -> bool:
        return self.search_state == SEARCH_SUFFICIENT_SAMPLE

    @property
    def engagement_sample_sufficient(self) -> bool:
        return self.engagement_state == ENGAGEMENT_SUFFICIENT_SAMPLE

    @property
    def old_enough_to_evaluate(self) -> bool:
        return self.search_state != SEARCH_NEWLY_PUBLISHED


def days_between(earlier: datetime | date | None, later: date) -> int | None:
    if earlier is None:
        return None
    if isinstance(earlier, datetime):
        earlier = earlier.date()
    return (later - earlier).days


def assess_maturity(
    *,
    article_id: int,
    published_at: datetime | date | None,
    today: date,
    google_index_state: str | None,
    gsc_data_through: date | None,
    ga4_data_through: date | None,
    impressions: int,
    organic_sessions: int,
    ga4_configured: bool,
    policy,
) -> ArticleMaturity:
    """記事 1 本の成熟度を決める (最初に当たった「まだ判断できない」理由が勝つ)。"""

    age_days = days_between(published_at, today)
    minimum_age = policy.minimum_article_age_days

    # -- 検索側 -------------------------------------------------------------
    published_on = published_at.date() if isinstance(published_at, datetime) else published_at
    # 検索データがそもそも公開日以降を含んでいるか。含んでいなければ、表示回数が
    # ゼロでも「成績が悪い」ことの証拠にはならない。
    covers = bool(
        gsc_data_through is not None
        and published_on is not None
        and gsc_data_through >= published_on
    )

    if age_days is None:
        search_state = SEARCH_NEWLY_PUBLISHED
        search_reason = "publication date unknown"
    elif age_days < minimum_age:
        search_state = SEARCH_NEWLY_PUBLISHED
        search_reason = f"published {age_days} day(s) ago (< {minimum_age})"
    elif not covers:
        search_state = SEARCH_INDEXED_AWAITING_DATA
        search_reason = (
            f"search console data only reaches {gsc_data_through}, "
            f"which does not cover the publication date {published_on}"
        )
    elif google_index_state is not None and google_index_state not in (_INDEXED, _UNKNOWN):
        search_state = SEARCH_AWAITING_INDEX_DISCOVERY
        search_reason = f"google reports {google_index_state}"
    elif impressions <= 0:
        search_state = SEARCH_INDEXED_AWAITING_DATA
        search_reason = "no impressions recorded yet in the evaluation window"
    elif impressions < int(policy.gate("ctr", "minimum_impressions", 200)) and impressions < int(
        policy.gate("ranking", "minimum_impressions", 100)
    ):
        search_state = SEARCH_INSUFFICIENT_IMPRESSIONS
        search_reason = f"{impressions} impression(s) is below every evaluation gate"
    else:
        search_state = SEARCH_SUFFICIENT_SAMPLE
        search_reason = f"{impressions} impression(s) in the evaluation window"

    # -- エンゲージメント側 --------------------------------------------------
    minimum_sessions = int(policy.gate("engagement", "minimum_organic_sessions", 100))
    if not ga4_configured:
        engagement_state = ENGAGEMENT_AWAITING_DATA
        engagement_reason = "ga4 is not configured"
    elif ga4_data_through is None:
        engagement_state = ENGAGEMENT_AWAITING_DATA
        engagement_reason = "ga4 has no daily rows yet"
    elif organic_sessions < minimum_sessions:
        engagement_state = ENGAGEMENT_INSUFFICIENT_SAMPLE
        engagement_reason = (
            f"{organic_sessions} organic session(s) is below the gate of {minimum_sessions}"
        )
    else:
        engagement_state = ENGAGEMENT_SUFFICIENT_SAMPLE
        engagement_reason = f"{organic_sessions} organic session(s) in the evaluation window"

    return ArticleMaturity(
        article_id=article_id,
        age_days=age_days,
        search_state=search_state,
        engagement_state=engagement_state,
        search_reason=search_reason,
        engagement_reason=engagement_reason,
        search_data_covers_article=covers,
        impressions=impressions,
        organic_sessions=organic_sessions,
    )
