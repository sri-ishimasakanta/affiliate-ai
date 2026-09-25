"""ThreadsLearningService -- 公開済み投稿と観測から学習結果を作る (T5、**読むだけ**)。

やること:

1. 公開済みの投稿・提案・記事・観測 (append-only) を DB から読む。
2. :mod:`app.social.threads.learning` に渡して、証拠と所見を作る。
3. 基準時刻 (既定は 24 時間前) の時点の結果を **同じ観測から作り直して** 比べる。

やらないこと:

- DB に書かない。書こうとしたら例外にする (``before_flush`` で止める)。
- Threads / Meta に問い合わせない。ThreadsService を作りもしない。
- 提案の生成・並び順・公開・承認・通知につながない (T5.5 以降)。

履歴のための表を持たないのは、観測が append-only で、どの時点の結果も同じ入力から
決定的に作り直せるからである。過去の観測を書き換えることはない。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.exceptions import ApplicationError
from app.models import (
    PUB_PUBLISHED,
    Article,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.operations.policy import get_policy as get_operations_policy
from app.social.threads.learning import (
    METRICS,
    PublicationFact,
    SnapshotFact,
    analyze,
    compare_reports,
)
from app.social.threads.policy import ThreadsMeasurementPolicy, get_measurement_policy


class ThreadsLearningError(ApplicationError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"threads learning error: {reason}")
        self.reason = reason


class ThreadsLearningService:
    def __init__(
        self,
        session: Session,
        *,
        policy: ThreadsMeasurementPolicy | None = None,
        timezone: ZoneInfo | None = None,
    ) -> None:
        self._session = session
        self._policy = policy or get_measurement_policy()
        # 時刻・曜日は C8 と同じ運用タイムゾーンで読む (Threads 専用の設定を作らない)。
        self._tz = timezone or get_operations_policy().timezone

    def report(self, *, now: datetime | None = None, since: datetime | None = None) -> dict:
        """``now`` の時点の学習結果と、``since`` の時点からの変化。"""

        now = ensure_aware(now or datetime.now(UTC))
        since = (
            ensure_aware(since)
            if since
            else now - timedelta(hours=self._policy.change_baseline_hours)
        )
        with _read_only(self._session):
            facts = self.facts()
        current = analyze(facts, as_of=now, policy=self._policy, tz=self._tz)
        baseline = analyze(facts, as_of=since, policy=self._policy, tz=self._tz)
        current["changes"] = compare_reports(current, baseline)
        current["side_effects"] = {
            "database_writes": 0,
            "threads_calls": 0,
            "threads_writes": 0,
            "emails": 0,
            "wordpress_writes": 0,
        }
        return current

    def facts(self) -> list[PublicationFact]:
        """公開済みの投稿 (T4 の計測と同じ条件) と、その観測。"""

        rows = self._session.scalars(
            select(ThreadsPublication)
            .where(
                ThreadsPublication.status == PUB_PUBLISHED,
                ThreadsPublication.threads_media_id.is_not(None),
            )
            .order_by(ThreadsPublication.id)
        ).all()
        snapshots = self._snapshots([row.id for row in rows])
        facts = []
        for row in rows:
            proposal = self._session.get(ThreadsPostProposal, row.proposal_id)
            article = (
                self._session.get(Article, row.source_article_id)
                if row.source_article_id is not None
                else None
            )
            keyword = article.keyword if article is not None else None
            facts.append(
                PublicationFact(
                    publication_id=row.id,
                    published_at=ensure_aware(row.published_at) if row.published_at else None,
                    angle=row.angle,
                    link_mode=getattr(proposal, "link_mode", None),
                    character_count=getattr(proposal, "character_count", None),
                    trigger=row.trigger,
                    article_id=row.source_article_id,
                    article_title=article.title if article is not None else None,
                    topic=keyword.keyword if keyword is not None else None,
                    proposal_id=row.proposal_id,
                    snapshots=tuple(snapshots.get(row.id, ())),
                )
            )
        return facts

    def _snapshots(self, publication_ids: list[int]) -> dict[int, list[SnapshotFact]]:
        if not publication_ids:
            return {}
        rows = self._session.scalars(
            select(ThreadsInsightSnapshot)
            .where(ThreadsInsightSnapshot.threads_publication_id.in_(publication_ids))
            .order_by(ThreadsInsightSnapshot.observed_at, ThreadsInsightSnapshot.id)
        ).all()
        grouped: dict[int, list[SnapshotFact]] = {}
        for row in rows:
            grouped.setdefault(row.threads_publication_id, []).append(
                SnapshotFact(
                    snapshot_id=row.id,
                    observed_at=ensure_aware(row.observed_at),
                    outcome=row.outcome,
                    age_hours=row.age_hours,
                    # 欠測は NULL のまま渡す (0 に丸めない)。
                    metrics={metric: getattr(row, metric) for metric in METRICS},
                )
            )
        return grouped


@contextmanager
def _read_only(session: Session) -> Iterator[None]:
    """この中で flush が起きたら止める。学習は DB を 1 行も変えない。"""

    def refuse(_session, _context, _instances) -> None:
        raise ThreadsLearningError("the learning report is read-only; a write was attempted")

    event.listen(session, "before_flush", refuse)
    try:
        yield
    finally:
        event.remove(session, "before_flush", refuse)


#: 他の読むだけの処理 (T6 の監査など) でも同じ保護を使う。
read_only_session = _read_only

__all__ = ["ThreadsLearningError", "ThreadsLearningService", "read_only_session"]
