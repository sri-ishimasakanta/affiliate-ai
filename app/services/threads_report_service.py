"""ThreadsReportService -- 人に見せる Threads の日次・週次のまとめの素材 (T6.4、**読むだけ**)。

DB と Growth の記録を読むだけで、Threads にも OpenAI にも問い合わせない。成績の分析や結論は
出さない (T6.5 の仕事)。数えるのは: 公開の本数 (通常記事 / Growth Post)・承認の状況・公開の
問題・保存済みの最新の指標 (合計)・Growth の目標と、あなたの対応が要ること。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import (
    PUB_IN_FLIGHT_STATES,
    PUB_PUBLISHED,
    TP_APPROVED,
    TP_OPEN_STATES,
    OperationsAlert,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.social.threads.growth import GROWTH_FOLLOWER_TARGET, FollowerObservation, target_reached

METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
GROWTH_DIRECTORY = Path("data/threads-growth")


class ThreadsReportService:
    def __init__(
        self,
        session: Session,
        *,
        timezone: ZoneInfo,
        growth_directory: Path | str = GROWTH_DIRECTORY,
    ) -> None:
        self._session = session
        self._tz = timezone
        self._growth_dir = Path(growth_directory)

    def summary(self, *, start: date, end: date, now: datetime | None = None) -> dict:
        """``start``〜``end`` (JST、両端を含む) のまとめ。"""

        now = ensure_aware(now or datetime.now(UTC))
        lo = datetime.combine(start, time(0), tzinfo=self._tz).astimezone(UTC)
        hi = datetime.combine(end + timedelta(days=1), time(0), tzinfo=self._tz).astimezone(UTC)
        pubs = [
            p
            for p in self._session.scalars(
                select(ThreadsPublication).where(ThreadsPublication.status == PUB_PUBLISHED)
            ).all()
            if p.published_at is not None and lo <= ensure_aware(p.published_at) < hi
        ]
        article = [p for p in pubs if p.source_article_id is not None]
        growth = [p for p in pubs if p.source_article_id is None]
        metrics = self._metrics([p.id for p in pubs])
        proposals = self._session.scalars(select(ThreadsPostProposal)).all()
        published_ids = {
            p.proposal_id
            for p in self._session.scalars(
                select(ThreadsPublication).where(ThreadsPublication.status == PUB_PUBLISHED)
            ).all()
        }
        awaiting = [p for p in proposals if p.status in TP_OPEN_STATES]
        approved_waiting = [
            p for p in proposals if p.status == TP_APPROVED and p.id not in published_ids
        ]
        problems = self._session.scalars(
            select(ThreadsPublication).where(
                or_(
                    ThreadsPublication.status.in_(tuple(PUB_IN_FLIGHT_STATES)),
                    ThreadsPublication.reconciliation_required.is_(True),
                    ThreadsPublication.status == "failed",
                )
            )
        ).all()
        failed = [p for p in problems if p.status == "failed" and not p.reconciliation_required]
        needs_check = [p for p in problems if p not in failed]
        alerts = self._session.scalars(
            select(OperationsAlert).where(
                OperationsAlert.source.like("threads%"),
                OperationsAlert.status.notin_(("resolved", "closed")),
            )
        ).all()
        observation = FollowerObservation.from_dict(self._read("followers.json"))
        status = self._read("status.json") or {}
        attention: list[str] = []
        if awaiting:
            attention.append(
                f"承認待ちの投稿案が {len(awaiting)} 件あります（メールのリンクから確認）"
            )
        if needs_check:
            attention.append(
                f"公開結果の確認が必要な投稿が {len(needs_check)} 件あります（自動再送は停止中）"
            )
        if failed:
            attention.append(
                f"公開に失敗した投稿が {len(failed)} 件あります（投稿はされていません）"
            )
        if alerts:
            attention.append(f"Threads のアラートが {len(alerts)} 件開いています")
        if target_reached(observation, GROWTH_FOLLOWER_TARGET):
            attention.append(
                f"フォロワーが目標の {GROWTH_FOLLOWER_TARGET} 人に届きました。"
                "次の目標を決めてください（Growth Post は止まっています）"
            )
        return {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "generated_at": now.isoformat(),
            "article_posts": len(article),
            "growth_posts": len(growth),
            "total_posts": len(pubs),
            "posts": [
                {
                    "publication_id": p.id,
                    "proposal_id": p.proposal_id,
                    "kind": "account_growth" if p.source_article_id is None else "article",
                    "published_at": ensure_aware(p.published_at).isoformat(),
                    "permalink": p.permalink,
                    "metrics": metrics.get(p.id, {}),
                }
                for p in sorted(pubs, key=lambda p: ensure_aware(p.published_at))
            ],
            "metrics_total": {
                m: sum(v.get(m) or 0 for v in metrics.values()) if metrics else None
                for m in METRICS
            },
            "observed_posts": len(metrics),
            "awaiting_approval": len(awaiting),
            "approved_unpublished": len(approved_waiting),
            "failed_publications": [p.id for p in failed],
            "needs_check_publications": [p.id for p in needs_check],
            "open_alerts": [{"id": a.id, "title": a.title, "severity": a.severity} for a in alerts],
            "growth": {
                "follower_target": GROWTH_FOLLOWER_TARGET,
                "follower_observation": observation.as_dict() if observation else None,
                "follower_target_reached": target_reached(observation, GROWTH_FOLLOWER_TARGET),
                "last_maintenance_reason": status.get("reason"),
                "follower_read": status.get("follower_read"),
            },
            "needs_attention": attention,
        }

    def _metrics(self, publication_ids: list[int]) -> dict[int, dict]:
        if not publication_ids:
            return {}
        latest: dict[int, ThreadsInsightSnapshot] = {}
        for snap in self._session.scalars(
            select(ThreadsInsightSnapshot)
            .where(
                ThreadsInsightSnapshot.threads_publication_id.in_(publication_ids),
                ThreadsInsightSnapshot.outcome == "observed",
            )
            .order_by(ThreadsInsightSnapshot.observed_at, ThreadsInsightSnapshot.id)
        ):
            latest[snap.threads_publication_id] = snap
        return {pid: {m: getattr(s, m) for m in METRICS} for pid, s in latest.items()}

    def _read(self, name: str) -> dict | None:
        path = self._growth_dir / name
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except ValueError:
            return None


__all__ = ["METRICS", "ThreadsReportService"]
