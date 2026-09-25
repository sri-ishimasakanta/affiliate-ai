"""ThreadsPerformanceService -- 保存済みの観測から、経過時間をそろえた診断を作る (読むだけ)。

- DB には書かない (``read_only_session`` で flush を止める)。
- Threads には問い合わせない。実際の公開時刻は ``ThreadsPublicationService.gap_basis``
  (保存済みの記録だけを読む) から取る。そのために渡す Threads の代役は、呼ばれたら
  例外にする (うっかり通信しないことを構造で保証する)。
- 観測を取り直したり、保存済みの指標を書き換えたりしない。

アカウントの投稿一覧を読む API はこのリポジトリの Threads client に無いので、手動・
管理外の投稿の検出は ``unavailable`` と報告する (新しい API 呼び出しは足さない)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import (
    PUB_PUBLISHED,
    Article,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.operations.policy import get_policy as get_c8_policy
from app.services.threads_learning_service import read_only_session
from app.services.threads_publication_service import ThreadsPublicationService
from app.social.threads.performance import (
    METRICS,
    Observation,
    PublicationRecord,
    build_report,
)
from app.social.threads.policy import get_measurement_policy, get_operations_policy

UNTRACKED_UNAVAILABLE = {
    "status": "unavailable",
    "reason": "the Threads client has no read-only method to list the account's posts; "
    "untracked-post detection is not implemented in this task",
    "remote_not_tracked": [],
}


class _NoNetwork:
    """Threads の代役。どのメソッドを呼んでも失敗させる (この診断は通信しない)。"""

    def __getattr__(self, name):
        raise RuntimeError(f"the performance diagnostic must not call Threads ({name})")


class ThreadsPerformanceService:
    def __init__(self, session: Session, *, settings, timezone: ZoneInfo | None = None) -> None:
        self._session = session
        self._settings = settings
        self._tz = timezone or get_c8_policy().timezone

    def records(self) -> list[PublicationRecord]:
        publications = ThreadsPublicationService(
            self._session, settings=self._settings, threads_service=_NoNetwork()
        )
        rows = self._session.scalars(
            select(ThreadsPublication)
            .where(ThreadsPublication.status == PUB_PUBLISHED)
            .order_by(ThreadsPublication.id)
        ).all()
        records = []
        for row in rows:
            basis = publications.gap_basis(row)
            if basis is None:
                continue
            proposal = self._session.get(ThreadsPostProposal, row.proposal_id)
            article = (
                self._session.get(Article, row.source_article_id)
                if row.source_article_id is not None
                else None
            )
            keyword = article.keyword if article is not None else None
            snapshots = self._session.scalars(
                select(ThreadsInsightSnapshot)
                .where(ThreadsInsightSnapshot.threads_publication_id == row.id)
                .order_by(ThreadsInsightSnapshot.observed_at, ThreadsInsightSnapshot.id)
            ).all()
            last = snapshots[-1] if snapshots else None
            records.append(
                PublicationRecord(
                    publication_id=row.id,
                    published_at=ensure_aware(basis.at),
                    basis_source=basis.source,
                    proposal_id=row.proposal_id,
                    media_id=row.threads_media_id,
                    trigger=row.trigger,
                    angle=row.angle,
                    link_mode=getattr(proposal, "link_mode", None),
                    character_count=getattr(proposal, "character_count", None),
                    article_id=row.source_article_id,
                    article_slug=getattr(article, "slug", None),
                    topic=keyword.keyword if keyword is not None else None,
                    text=row.exact_published_text or "",
                    observations=tuple(
                        Observation(
                            observed_at=ensure_aware(s.observed_at),
                            outcome=s.outcome,
                            metrics={m: getattr(s, m) for m in METRICS},
                            snapshot_id=s.id,
                        )
                        for s in snapshots
                    ),
                    last_error_category=last.error_category if last is not None else None,
                )
            )
        return records

    def report(self, *, as_of: datetime | None = None) -> dict:
        as_of = ensure_aware(as_of or datetime.now(UTC))
        with read_only_session(self._session):
            records = self.records()
        return build_report(
            records,
            as_of=as_of,
            measurement_policy=get_measurement_policy(),
            operations_policy=get_operations_policy(),
            tz=self._tz,
            untracked={
                **UNTRACKED_UNAVAILABLE,
                # 手元の公開で、最後の観測が「見つからない (404)」だったもの。
                "internal_not_resolvable": [
                    r.publication_id
                    for r in records
                    if r.last_error_category == "threads_not_found"
                ],
                "tracked_media_ids": len([r for r in records if r.media_id]),
            },
        )


__all__ = ["ThreadsPerformanceService"]
