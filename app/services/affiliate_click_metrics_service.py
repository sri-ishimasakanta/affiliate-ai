"""AffiliateClickMetricsService -- first-party の outbound click を記事/プログラム別に
集計する read-only service (C5.3)。

クリックの記録そのものは既存基盤が行う:

- WordPress 側の ``/go/<token>`` runtime が click を記録し、署名付き export
  endpoint で公開する (D-B2)。
- :mod:`app.affiliate.click_export_client` が export を読み、
  :class:`AffiliateOutboundClick` として append する (``scripts/
  import_affiliate_outbound_clicks.py``)。

この service は **集計だけ** を行い、click を作らない・変更しない。記事への帰属は
``AffiliateOutboundClick.token`` -> :class:`AffiliateLinkTarget` の join で決まる。
target が見つからない token は **捨てずに** ``unattributed`` として数え、データ品質
チェックに載せる (黙って落とさない)。

保持しているのは token・時刻・provider 側の click id だけで、IP・User-Agent・
cookie・フィンガープリントの類は一切持たない。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AffiliateLinkTarget, AffiliateOutboundClick


@dataclass(frozen=True)
class ClickBucket:
    """1 記事 x 1 プログラムのクリック集計。"""

    article_id: int
    affiliate_program_id: int
    clicks: int
    first_click_at: datetime | None
    last_click_at: datetime | None
    by_date: dict[date, int] = field(default_factory=dict)


@dataclass
class ClickMetrics:
    """集計結果。帰属できなかった click も必ず表に出す。"""

    window_start: date | None
    window_end: date | None
    total_clicks: int = 0
    buckets: list[ClickBucket] = field(default_factory=list)
    unattributed_clicks: int = 0
    unattributed_tokens: list[str] = field(default_factory=list)
    inactive_target_clicks: int = 0

    def for_article(self, article_id: int) -> list[ClickBucket]:
        return [b for b in self.buckets if b.article_id == article_id]


class AffiliateClickMetricsService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def aggregate(
        self, *, start_date: date | None = None, end_date: date | None = None
    ) -> ClickMetrics:
        targets = {t.token: t for t in self._session.scalars(select(AffiliateLinkTarget)).all()}
        stmt = select(AffiliateOutboundClick).order_by(AffiliateOutboundClick.clicked_at)
        metrics = ClickMetrics(window_start=start_date, window_end=end_date)

        grouped: dict[tuple[int, int], list[AffiliateOutboundClick]] = defaultdict(list)
        unattributed: set[str] = set()
        for click in self._session.scalars(stmt).all():
            clicked_on = click.clicked_at.date()
            if start_date is not None and clicked_on < start_date:
                continue
            if end_date is not None and clicked_on > end_date:
                continue
            metrics.total_clicks += 1
            target = targets.get(click.token)
            if target is None:
                metrics.unattributed_clicks += 1
                unattributed.add(click.token)
                continue
            if target.status != "active":
                metrics.inactive_target_clicks += 1
            grouped[(target.article_id, target.affiliate_program_id)].append(click)

        for (article_id, program_id), clicks in sorted(grouped.items()):
            by_date: dict[date, int] = defaultdict(int)
            for click in clicks:
                by_date[click.clicked_at.date()] += 1
            metrics.buckets.append(
                ClickBucket(
                    article_id=article_id,
                    affiliate_program_id=program_id,
                    clicks=len(clicks),
                    first_click_at=min(c.clicked_at for c in clicks),
                    last_click_at=max(c.clicked_at for c in clicks),
                    by_date=dict(sorted(by_date.items())),
                )
            )
        metrics.unattributed_tokens = sorted(unattributed)
        return metrics
