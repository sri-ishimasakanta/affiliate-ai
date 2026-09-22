"""記事ごとの **収益計測の成熟度** (C7)。

C6 の検索/エンゲージメント成熟度と同じ発想を、収益側に独立して適用する:

    データが無いことと、成果が出ていないことは別物である。

GA4 の日次行がまだ無い記事について「アフィリエイト CTR 0%」と言ってはならない。
正しくは「流入データ待ち」である。同様に、自分たちの計測用リクエストが作った
クリックは読者行動ではないので、信頼できる期間の外にあるものは分母にも分子にも
入れない。

C6 と同じく、**収益性の判定と計測の成熟度を混ぜない**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# -- 収益計測の成熟度 ----------------------------------------------------------
#: そもそも収益化を意図していない記事 (supporting など)。
NOT_MONETIZABLE = "not_monetizable"
#: 収益化を意図しているが、トラッキングの設定が揃っていない。
MONETIZATION_SETUP_INCOMPLETE = "monetization_setup_incomplete"
#: 収益化済みだが、流入データがまだ届いていない。
MONETIZED_AWAITING_TRAFFIC = "monetized_awaiting_traffic"
#: 流入データはあるが、評価に足りない。
INSUFFICIENT_TRAFFIC_SAMPLE = "insufficient_traffic_sample"
#: 流入は十分だが、信頼できるクリックが評価に足りない。
INSUFFICIENT_CLICK_SAMPLE = "insufficient_click_sample"
#: 行動指標を議論できる。
MEASURABLE = "measurable"

#: 成果 (コンバージョン/報酬) を記事へ結び付けられない。
ATTRIBUTION_UNAVAILABLE = "unavailable"
#: プログラム単位でのみ分かる。
ATTRIBUTION_PROGRAM_ONLY = "program_only"
#: 記事単位で分かる (現時点では到達しない)。
ATTRIBUTION_ARTICLE = "article"

_AFFILIATE_MODE = "affiliate"


@dataclass(frozen=True)
class RevenueMaturity:
    """1 記事の収益計測の成熟度。判定根拠も持つ。"""

    article_id: int
    state: str
    reason: str
    attribution: str
    attribution_reason: str
    age_days: int | None
    monetized: bool
    sessions: int | None
    organic_sessions: int | None
    clean_clicks: int
    excluded_clicks: int
    raw_clicks: int
    traffic_data_available: bool

    @property
    def behavioral_evaluation_allowed(self) -> bool:
        """行動 (クリック率など) の議論をしてよいか。"""

        return self.state in (MEASURABLE, INSUFFICIENT_CLICK_SAMPLE)


def assess_revenue_maturity(
    *,
    article_id: int,
    monetization_mode: str | None,
    published_at: datetime | date | None,
    today: date,
    active_target_count: int,
    active_mapping_count: int,
    traffic_data_available: bool,
    sessions: int | None,
    organic_sessions: int | None,
    clean_clicks: int,
    excluded_clicks: int,
    raw_clicks: int,
    commission_attribution: str,
    policy,
) -> RevenueMaturity:
    """最初に当たった「まだ判断できない」理由が勝つ。"""

    age_days = _days_between(published_at, today)
    monetized = active_target_count > 0 and active_mapping_count > 0
    minimum_organic = int(policy.gate("traffic", "minimum_organic_sessions_for_click_review", 100))
    minimum_clicks = int(policy.gate("clicks", "minimum_clean_clicks_for_pattern_review", 20))

    if monetization_mode != _AFFILIATE_MODE and not monetized:
        state = NOT_MONETIZABLE
        reason = f"monetization mode is {monetization_mode!r}; no affiliate placement is intended"
    elif not monetized:
        state = MONETIZATION_SETUP_INCOMPLETE
        reason = (
            f"monetization mode is {monetization_mode!r} but there is no active "
            f"target/mapping (targets={active_target_count}, mappings={active_mapping_count})"
        )
    elif age_days is not None and age_days < policy.minimum_article_age_days:
        state = MONETIZED_AWAITING_TRAFFIC
        reason = (
            f"published {age_days} day(s) ago (< {policy.minimum_article_age_days}); "
            "too early to read behaviour"
        )
    elif not traffic_data_available:
        state = MONETIZED_AWAITING_TRAFFIC
        reason = "ga4 has no daily rows for this article yet"
    elif (organic_sessions or 0) < minimum_organic:
        state = INSUFFICIENT_TRAFFIC_SAMPLE
        reason = (
            f"{organic_sessions or 0} organic session(s) is below the gate of {minimum_organic}"
        )
    elif clean_clicks < minimum_clicks:
        state = INSUFFICIENT_CLICK_SAMPLE
        reason = (
            f"{clean_clicks} trusted click(s) is below the gate of {minimum_clicks} "
            f"({excluded_clicks} click(s) excluded as instrumentation)"
        )
    else:
        state = MEASURABLE
        reason = (
            f"{organic_sessions} organic session(s) and {clean_clicks} trusted click(s) "
            "in the evaluation window"
        )

    if commission_attribution == ATTRIBUTION_ARTICLE:
        attribution_reason = "provider data contains an article-level join key"
    elif commission_attribution == ATTRIBUTION_PROGRAM_ONLY:
        attribution_reason = (
            "provider reports commissions per program only; there is no click/order/subid "
            "join back to an article"
        )
    else:
        attribution_reason = "no commission data is available for this program"

    return RevenueMaturity(
        article_id=article_id,
        state=state,
        reason=reason,
        attribution=commission_attribution,
        attribution_reason=attribution_reason,
        age_days=age_days,
        monetized=monetized,
        sessions=sessions,
        organic_sessions=organic_sessions,
        clean_clicks=clean_clicks,
        excluded_clicks=excluded_clicks,
        raw_clicks=raw_clicks,
        traffic_data_available=traffic_data_available,
    )


def _days_between(earlier: datetime | date | None, later: date) -> int | None:
    if earlier is None:
        return None
    if isinstance(earlier, datetime):
        earlier = earlier.date()
    return (later - earlier).days
