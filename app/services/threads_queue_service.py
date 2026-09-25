"""ThreadsQueueService -- queue 評価の事実を DB から集める (T4.1、read-only)。

**読むだけ。** 提案・公開・承認のどの行も書き換えない。Meta にも触れない
(設定の状態は ``describe()`` で見るだけで、ネットワークは使わない)。

提案 1 件ごとの中身の判定 (stale / 文字数の不整合 / 既に公開済み / 公開途中) は
T3 の :meth:`ThreadsPublicationService.assess` をそのまま使う。同じ判定を
ここで作り直さない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import (
    PUB_IN_FLIGHT_STATES,
    PUB_PUBLISHED,
    SNAPSHOT_FAILED,
    TP_APPROVED,
    TP_OPEN_STATES,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.operations.policy import get_policy as get_operations_policy
from app.services.threads_publication_service import ThreadsPublicationService
from app.social.threads.measurement import classify_maturity
from app.social.threads.policy import (
    ThreadsMeasurementPolicy,
    ThreadsOperationsPolicy,
    get_measurement_policy,
)
from app.social.threads.policy import (
    get_operations_policy as get_threads_operations_policy,
)
from app.social.threads.queue import CandidateFacts, QueueEvaluation, QueueFacts, evaluate_queue
from app.social.threads.schedule import local_day_bounds
from app.social.threads.service import ThreadsConnectionStatus, ThreadsService


class ThreadsQueueService:
    def __init__(
        self,
        session: Session,
        *,
        settings,
        threads_service: ThreadsService | None = None,
        policy: ThreadsOperationsPolicy | None = None,
        measurement_policy: ThreadsMeasurementPolicy | None = None,
        timezone: ZoneInfo | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._threads = threads_service or ThreadsService(settings)
        self._policy = policy or get_threads_operations_policy()
        self._measurement = measurement_policy or get_measurement_policy()
        self._tz = timezone or get_operations_policy().timezone
        self._publications = ThreadsPublicationService(
            session, settings=settings, threads_service=self._threads
        )

    @property
    def timezone(self) -> ZoneInfo:
        return self._tz

    @property
    def policy(self) -> ThreadsOperationsPolicy:
        return self._policy

    def config_status(self) -> ThreadsConnectionStatus:
        return self._threads.describe()

    # -- facts -----------------------------------------------------------------
    def facts(self, *, now: datetime | None = None) -> QueueFacts:
        now = ensure_aware(now or datetime.now(UTC))
        status = self._threads.describe()
        # 間隔は **実際の公開時刻** から測る。T3 の書き込み経路と同じ起点を使う。
        basis = self.latest_gap_basis()
        last = self._session.get(ThreadsPublication, basis.publication_id) if basis else None
        return QueueFacts(
            now=now,
            threads_state=status.state,
            candidates=tuple(self._candidate_facts()),
            last_published_at=basis.at if basis else None,
            last_published_angle=last.angle if last else None,
            last_published_article_id=last.source_article_id if last else None,
            published_today=self.published_today(now),
            uncertain_publication_ids=tuple(self.uncertain_publication_ids()),
            mature_post_count=self.mature_post_count(now),
            minimum_mature_posts=self._measurement.minimum_mature_posts,
        )

    def evaluate(
        self, *, now: datetime | None = None, publication_enabled: bool = False
    ) -> QueueEvaluation:
        """queue を評価する。``publication_enabled`` は自動公開の経路だけが True にする。"""

        return evaluate_queue(
            self.facts(now=now),
            self._policy,
            self._tz,
            publication_enabled=publication_enabled,
        )

    def approval_fingerprint(self) -> tuple:
        """queue の中身が変わったかを安く判定するための指紋 (id と状態だけ)。"""

        rows = self._session.execute(
            select(ThreadsPostProposal.id, ThreadsPostProposal.status).order_by(
                ThreadsPostProposal.id
            )
        ).all()
        publications = self._session.execute(
            select(ThreadsPublication.id, ThreadsPublication.status).order_by(ThreadsPublication.id)
        ).all()
        return (tuple(tuple(r) for r in rows), tuple(tuple(p) for p in publications))

    def counts(self) -> dict:
        proposals = self._session.scalars(select(ThreadsPostProposal)).all()
        published = set(self._session.scalars(select(ThreadsPublication.proposal_id)).all())
        return {
            "approved": sum(1 for p in proposals if p.status == TP_APPROVED),
            # 承認済みでも、公開済みのものは queue に残っていない。
            "approved_unpublished": sum(
                1 for p in proposals if p.status == TP_APPROVED and p.id not in published
            ),
            "awaiting_approval": sum(1 for p in proposals if p.status in TP_OPEN_STATES),
            "total": len(proposals),
        }

    # -- publications ------------------------------------------------------------
    def latest_gap_basis(self):
        """最後の公開の **実際の** 時刻 (T3 と同じ計算)。"""

        return self._publications.latest_gap_basis()

    def latest_publication(self) -> ThreadsPublication | None:
        """実際の公開時刻が最も新しい公開。"""

        basis = self.latest_gap_basis()
        return self._session.get(ThreadsPublication, basis.publication_id) if basis else None

    def uncertain_publication_ids(self) -> list[int]:
        """照合が済んでいない公開。**1 件でもあれば queue 全体を止める。**"""

        return list(
            self._session.scalars(
                select(ThreadsPublication.id)
                .where(
                    or_(
                        ThreadsPublication.status.in_(tuple(PUB_IN_FLIGHT_STATES)),
                        ThreadsPublication.reconciliation_required.is_(True),
                    )
                )
                .order_by(ThreadsPublication.id)
            ).all()
        )

    def published_today(self, now: datetime) -> int:
        """運用タイムゾーンでの「今日」に公開した本数。"""

        start, end = local_day_bounds(now, self._tz)
        rows = self._session.scalars(
            select(ThreadsPublication).where(ThreadsPublication.status == PUB_PUBLISHED)
        ).all()
        bases = [self._publications.gap_basis(row) for row in rows]
        return sum(1 for basis in bases if basis is not None and start <= basis.at < end)

    def mature_post_count(self, now: datetime) -> int:
        """比較に使える投稿の数。**成熟していて、観測できている** ものだけ数える。"""

        count = 0
        for row in self._session.scalars(
            select(ThreadsPublication).where(
                ThreadsPublication.status == PUB_PUBLISHED,
                ThreadsPublication.published_at.is_not(None),
            )
        ).all():
            age = (now - ensure_aware(row.published_at)).total_seconds() / 3600.0
            if not classify_maturity(age, self._measurement).comparable:
                continue
            observed = self._session.scalars(
                select(ThreadsInsightSnapshot.id)
                .where(
                    ThreadsInsightSnapshot.threads_publication_id == row.id,
                    ThreadsInsightSnapshot.outcome != SNAPSHOT_FAILED,
                )
                .limit(1)
            ).first()
            if observed is not None:
                count += 1
        return count

    # -- candidates --------------------------------------------------------------
    def _candidate_facts(self) -> list[CandidateFacts]:
        rows = self._session.scalars(
            select(ThreadsPostProposal)
            .where(ThreadsPostProposal.status.in_((TP_APPROVED, *TP_OPEN_STATES)))
            .order_by(ThreadsPostProposal.id)
        ).all()
        out: list[CandidateFacts] = []
        for proposal in rows:
            assessment = self._publications.assess(proposal)
            out.append(
                CandidateFacts(
                    proposal_id=proposal.id,
                    status=proposal.status,
                    angle=proposal.angle,
                    source_article_id=proposal.source_article_id,
                    link_mode=proposal.link_mode,
                    approved_at=self._approved_at(proposal),
                    stale_reasons=assessment.stale_reasons,
                    integrity_reasons=assessment.integrity_reasons,
                    already_published=assessment.already_published,
                    in_flight=assessment.in_flight,
                    held=proposal.held_at is not None,
                    not_before=_aware_or_none(proposal.not_before),
                    expires_at=_aware_or_none(proposal.expires_at),
                    preferred_at=_aware_or_none(proposal.preferred_at),
                )
            )
        return out

    @staticmethod
    def _approved_at(proposal: ThreadsPostProposal) -> datetime | None:
        """承認をシステムが受理した時刻 (T4.2 で永続化した権威ある値)。

        T4.1 は携帯承認の決定時刻、無ければ ``updated_at`` から推測していた。
        ``updated_at`` は無関係な変更でも動くので、もう使わない。権威ある値が無い
        過去の行は ``None`` のまま (承認順では最後に並ぶ)。
        """

        if proposal.status != TP_APPROVED:
            return None
        return _aware_or_none(proposal.approved_at)


def _aware_or_none(moment: datetime | None) -> datetime | None:
    return ensure_aware(moment) if moment is not None else None


__all__ = ["ThreadsQueueService"]
