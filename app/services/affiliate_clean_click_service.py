"""AffiliateCleanClickService -- 信頼できるクリックだけを切り出す (C7)。

**なぜ必要か**: ``/go/<token>`` の runtime は、その URL への GET を無条件に 1 クリック
として記録する。C4.8〜C5.2 で行ったリダイレクトの健全性チェックも、読者のクリックと
同じ行に化けている (2026-09-22 12:20〜13:46 の 17 件がこれに当たる)。この行を読者の
行動として読むと、記事 10/11 が「クリック率が高い」という誤った結論になる。

対処方針:

- **履歴は絶対に削除・書き換えしない**。``affiliate_outbound_clicks`` の行はそのまま
  監査証跡として残す。
- ポリシーの ``trusted_measurement_start_at`` より前のクリックを ``excluded`` として
  数え、``clean`` からは外す。境界は設定ファイルにあり、理由も併記されている。
- 集計結果は必ず ``raw`` / ``excluded`` / ``clean`` の 3 つを同時に出す。どれか 1 つ
  だけを見せて誤解させない。

帰属 (どの記事・どのプログラムか) は C5.3 の :class:`AffiliateClickMetricsService` と
同じく ``token -> AffiliateLinkTarget`` の join で決まる。target の無い token は
捨てずに ``unattributed`` として数える。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AffiliateLinkTarget, AffiliateOutboundClick


@dataclass(frozen=True)
class ArticleClickCounts:
    """1 記事のクリック内訳。3 つの数を必ず一緒に持つ。"""

    article_id: int
    affiliate_program_ids: tuple[int, ...]
    raw_clicks: int
    excluded_clicks: int
    clean_clicks: int
    first_clean_click_at: datetime | None
    last_clean_click_at: datetime | None


@dataclass
class CleanClickBaseline:
    """全体の内訳と記事ごとの内訳。"""

    trusted_measurement_start_at: datetime | None
    window_start: date | None
    window_end: date | None
    raw_clicks: int = 0
    excluded_clicks: int = 0
    clean_clicks: int = 0
    unattributed_raw_clicks: int = 0
    unattributed_tokens: list[str] = field(default_factory=list)
    inactive_target_raw_clicks: int = 0
    clean_data_through: date | None = None
    raw_data_through: date | None = None
    by_article: dict[int, ArticleClickCounts] = field(default_factory=dict)

    def counts_for(self, article_id: int) -> ArticleClickCounts:
        return self.by_article.get(
            article_id,
            ArticleClickCounts(
                article_id=article_id,
                affiliate_program_ids=(),
                raw_clicks=0,
                excluded_clicks=0,
                clean_clicks=0,
                first_clean_click_at=None,
                last_clean_click_at=None,
            ),
        )

    def as_dict(self) -> dict:
        return {
            "trusted_measurement_start_at": (
                self.trusted_measurement_start_at.isoformat()
                if self.trusted_measurement_start_at
                else None
            ),
            "raw_clicks": self.raw_clicks,
            "excluded_clicks": self.excluded_clicks,
            "clean_clicks": self.clean_clicks,
            "unattributed_raw_clicks": self.unattributed_raw_clicks,
            "inactive_target_raw_clicks": self.inactive_target_raw_clicks,
            "clean_data_through": (
                self.clean_data_through.isoformat() if self.clean_data_through else None
            ),
            "raw_data_through": (
                self.raw_data_through.isoformat() if self.raw_data_through else None
            ),
        }


class AffiliateCleanClickService:
    def __init__(self, session: Session, *, policy) -> None:
        self._session = session
        self._policy = policy

    def build(
        self, *, window_start: date | None = None, window_end: date | None = None
    ) -> CleanClickBaseline:
        cutoff = self._policy.trusted_measurement_start_at
        targets = {t.token: t for t in self._session.scalars(select(AffiliateLinkTarget)).all()}
        baseline = CleanClickBaseline(
            trusted_measurement_start_at=cutoff,
            window_start=window_start,
            window_end=window_end,
        )

        grouped: dict[int, dict] = defaultdict(
            lambda: {
                "programs": set(),
                "raw": 0,
                "excluded": 0,
                "clean": 0,
                "clean_times": [],
            }
        )
        unattributed: set[str] = set()

        for click in self._session.scalars(
            select(AffiliateOutboundClick).order_by(AffiliateOutboundClick.clicked_at)
        ).all():
            clicked_at = _as_utc(click.clicked_at)
            clicked_on = clicked_at.date()
            if window_start is not None and clicked_on < window_start:
                continue
            if window_end is not None and clicked_on > window_end:
                continue

            trusted = cutoff is None or clicked_at >= cutoff
            baseline.raw_clicks += 1
            baseline.raw_data_through = _max_date(baseline.raw_data_through, clicked_on)
            if trusted:
                baseline.clean_clicks += 1
                baseline.clean_data_through = _max_date(baseline.clean_data_through, clicked_on)
            else:
                baseline.excluded_clicks += 1

            target = targets.get(click.token)
            if target is None:
                baseline.unattributed_raw_clicks += 1
                unattributed.add(click.token)
                continue
            if target.status != "active":
                baseline.inactive_target_raw_clicks += 1

            bucket = grouped[target.article_id]
            bucket["programs"].add(target.affiliate_program_id)
            bucket["raw"] += 1
            if trusted:
                bucket["clean"] += 1
                bucket["clean_times"].append(clicked_at)
            else:
                bucket["excluded"] += 1

        baseline.unattributed_tokens = sorted(unattributed)
        for article_id, bucket in sorted(grouped.items()):
            times = bucket["clean_times"]
            baseline.by_article[article_id] = ArticleClickCounts(
                article_id=article_id,
                affiliate_program_ids=tuple(sorted(bucket["programs"])),
                raw_clicks=bucket["raw"],
                excluded_clicks=bucket["excluded"],
                clean_clicks=bucket["clean"],
                first_clean_click_at=min(times) if times else None,
                last_clean_click_at=max(times) if times else None,
            )
        return baseline


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _max_date(current: date | None, candidate: date) -> date:
    return candidate if current is None or candidate > current else current
