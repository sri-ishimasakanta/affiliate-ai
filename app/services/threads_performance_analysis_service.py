"""ThreadsPerformanceAnalysisService -- 自分の投稿の成績の分析と、生成への補助の参考 (T6.5)。

**読むだけ。** DB には書かない (``read_only_session`` で flush を止める)。Threads には
問い合わせない (公開の時刻と観測は ``ThreadsPerformanceService.records`` の保存済みの記録から)。

- 公開 → 提案 (種類・会話のきっかけ) → コンテナ作成の記録 (実際に送ったトピック) をつなぐ。
- 分析は :mod:`app.social.threads.performance_analysis` (pure)。
- 参考 (``feedback``) は、読めない・証拠が無いときは **中立** に落ちる (生成を止めない)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import ThreadsPostProposal
from app.operations.policy import get_policy as get_c8_policy
from app.services.threads_feature_store import _kind, _sent_topic
from app.services.threads_learning_service import read_only_session
from app.services.threads_performance_service import ThreadsPerformanceService
from app.social.threads.conversation import hook_from_provenance
from app.social.threads.performance_analysis import (
    LANE_GROWTH,
    LANE_REGULAR,
    LANE_UNKNOWN,
    EvidenceThresholds,
    PerformanceFeedback,
    PostInput,
    build_analysis,
    build_feedback,
    neutral_feedback,
)
from app.social.threads.policy import get_measurement_policy, get_operations_policy
from app.social.threads.topic import CONTENT_KIND_ACCOUNT_GROWTH, CONTENT_KIND_ARTICLE


def _lane(kind: str) -> str:
    if kind == CONTENT_KIND_ACCOUNT_GROWTH:
        return LANE_GROWTH
    if kind == CONTENT_KIND_ARTICLE:
        return LANE_REGULAR
    return LANE_UNKNOWN


class ThreadsPerformanceAnalysisService:
    def __init__(self, session: Session, *, settings=None, timezone: ZoneInfo | None = None,
                 observation_policy: dict | None = None) -> None:  # fmt: skip
        self._session = session
        if settings is None:
            from app.config.settings import get_settings

            settings = get_settings()
        self._settings = settings
        self._tz = timezone or get_c8_policy().timezone
        if observation_policy is None:
            from app.social.threads.observer.planning import load_policy

            observation_policy = load_policy()
        self._thresholds = EvidenceThresholds.from_policy(observation_policy)

    @property
    def thresholds(self) -> EvidenceThresholds:
        return self._thresholds

    def inputs(self) -> list[PostInput]:
        records = ThreadsPerformanceService(
            self._session, settings=self._settings, timezone=self._tz
        ).records()
        out = []
        for record in records:
            proposal = (self._session.get(ThreadsPostProposal, record.proposal_id)
                        if record.proposal_id else None)  # fmt: skip
            kind = _kind(proposal)
            recorded, topic = _sent_topic(self._session, record.publication_id)
            hook = (None if kind == CONTENT_KIND_ACCOUNT_GROWTH
                    else hook_from_provenance(getattr(proposal, "learning_guidance_json", None)))
            out.append(PostInput(record=record, lane=_lane(kind), content_kind=kind,
                                 threads_topic=topic, topic_recorded=recorded,
                                 conversation_hook=hook))  # fmt: skip
        return out

    def report(self, *, as_of: datetime | None = None) -> dict:
        as_of = ensure_aware(as_of or datetime.now(UTC))
        with read_only_session(self._session):
            inputs = self.inputs()
            self._session.rollback()
        return build_analysis(
            inputs, as_of=as_of, tz=self._tz, measurement_policy=get_measurement_policy(),
            operations_policy=get_operations_policy(), thresholds=self._thresholds,
        )  # fmt: skip

    def feedback(self, *, as_of: datetime | None = None, supported_values=None
                 ) -> PerformanceFeedback:  # fmt: skip
        """生成への補助の参考。失敗しても生成は止めない (中立の参考を返す)。"""

        as_of = ensure_aware(as_of or datetime.now(UTC))
        try:
            report = self.report(as_of=as_of)
        except Exception as exc:  # noqa: BLE001 - 参考が作れないなら中立 (生成を止めない)
            return neutral_feedback(as_of.isoformat(),
                                    f"performance analysis unavailable ({type(exc).__name__})")
        return build_feedback(report, supported_values=supported_values)


def feedback_provider(session_factory, *, settings=None, timezone: ZoneInfo | None = None,
                      supported_values=None):  # fmt: skip
    """``ThreadsProposalService(performance_feedback_provider=...)`` に渡す関数を作る。"""

    def provide(as_of: datetime) -> PerformanceFeedback:
        with session_factory() as session:
            return ThreadsPerformanceAnalysisService(
                session, settings=settings, timezone=timezone
            ).feedback(as_of=as_of, supported_values=supported_values)

    return provide


__all__ = ["ThreadsPerformanceAnalysisService", "feedback_provider"]
