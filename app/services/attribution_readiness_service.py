"""帰属の準備度の報告 (C10-A)。**読むだけ・外に問い合わせない・追跡の URL を変えない。**

- プログラムごとの準備度 (``app/revenue/attribution_readiness.py``)。
- クリック: 信頼できる計測開始より後・リンク先で記事まで結べる数 / 結べない数。
- データの品質の印: 同じ秒に複数のクリックが並ぶ (計測の試験らしい) 束 —
  **除かない** (印を付けるだけ。除くかは人の判断)。
- 成果: 行の数・クリックの参照を持つ行の数 (いまの表にはその列が無いので常に 0)。
"""

from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import (
    AffiliateCommissionFact,
    AffiliateLinkTarget,
    AffiliateOutboundClick,
    AffiliateProgram,
)
from app.revenue import attribution_readiness as ar

#: 同じ秒に、この数以上のクリックが並んだら「束」とみなす (人の読者の動きとしては不自然)。
BURST_MIN_CLICKS = 2


class AttributionReadinessService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def report(self) -> dict:
        from app.revenue.policy import load_policy

        trusted = load_policy().trusted_measurement_start_at
        targets = list(self._session.scalars(select(AffiliateLinkTarget)))
        by_token = {t.token: t for t in targets}
        clicks = list(self._session.scalars(select(AffiliateOutboundClick)
                                            .order_by(AffiliateOutboundClick.clicked_at)))
        counts = Counter()
        per_second: dict[str, list[str]] = defaultdict(list)
        for click in clicks:
            at = ensure_aware(click.clicked_at)
            if trusted is not None and at < trusted:
                counts["before_trusted_start"] += 1
                continue
            target = by_token.get(click.token)
            counts["trusted_linked" if target is not None else "trusted_unattributed"] += 1
            if target is not None:
                per_second[at.replace(microsecond=0).isoformat()].append(click.token)
        bursts = {k: len(v) for k, v in per_second.items() if len(v) >= BURST_MIN_CLICKS}
        commissions = list(self._session.scalars(select(AffiliateCommissionFact)))
        rows_by_program = Counter(c.affiliate_program_id for c in commissions)
        programs = []
        for program in self._session.scalars(select(AffiliateProgram)
                                             .order_by(AffiliateProgram.id)):
            mine = [t for t in targets if t.affiliate_program_id == program.id]
            active = [t for t in mine if t.status == "active"]
            programs.append(ar.program_readiness(
                program_id=program.id, name=program.name, provider=program.provider,
                has_tracking_url=bool(program.tracking_url), active_targets=len(active),
                targets_with_article=sum(1 for t in active if t.article_id is not None),
                commission_rows=rows_by_program.get(program.id, 0),
                commissions_with_click_reference=0,  # 表に参照の列が無い
                subid_in_tracking_url=False))  # 追跡の URL に参照を足す処理は無い
        self._session.rollback()
        return {
            "trusted_measurement_start_at": trusted.isoformat() if trusted else None,
            "clicks": {"total": len(clicks), **dict(counts),
                       "possible_instrumentation_bursts": len(bursts),
                       "clicks_in_bursts": sum(bursts.values()),
                       "burst_seconds": sorted(bursts)[:10]},
            "commissions": {"rows": len(commissions),
                            "rows_with_click_reference": 0,
                            "attribution": "provider level at best; no article join key"},
            "programs": [p.as_dict() for p in programs],
            "summary": {
                "click_levels": dict(Counter(p.click_attribution_level for p in programs)),
                "commission_levels": dict(Counter(p.commission_attribution_level
                                                  for p in programs)),
                "deterministic_commission_join_programs": sum(
                    1 for p in programs if p.deterministic_join_possible)},
            "truth_table": list(ar.TRUTH_TABLE),
            "c11_requirements": list(ar.C11_REQUIREMENTS),
            "notes": ["no revenue is allocated to articles; no proportional split; no "
                      "last-click assumption", "tracking URLs are not modified"],
        }


__all__ = ["AttributionReadinessService"]
