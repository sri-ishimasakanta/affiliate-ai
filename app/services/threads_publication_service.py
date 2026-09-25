"""ThreadsPublicationService -- 承認済み提案を 1 度だけ公開する (T3)。

**承認は公開ではない。** 承認された提案は「公開してよい」状態になるだけで、外へ
出るのは人が明示的に実行したときだけである。スケジューラからは呼ばれない。

二重投稿を防ぐことがこの service の最大の責務である:

- 提案 1 件につき公開行は 1 つ (``UNIQUE(proposal_id)``)。
- ``threads_media_id`` を持つ行は二度と公開しない。
- 応答を取りこぼしたら ``uncertain`` にして止める。**盲目的に再送しない。**
  コンテナ状態 (``GET /{container-id}?fields=status,error_message``) を照会し、
  ``PUBLISHED`` なら「公開は成立していた」として確定させる。
- 途中で落ちた行は in-flight のまま残り、照合するまで次へ進めない。

送るのは **承認された文字列そのもの**。ここで文章も URL も作り直さない。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import ensure_aware, to_storage_utc
from app.exceptions import ApplicationError
from app.models import (
    PUB_CONTAINER_CREATED,
    PUB_CREATING,
    PUB_FAILED,
    PUB_IN_FLIGHT_STATES,
    PUB_PLANNED,
    PUB_PUBLISHED,
    PUB_PUBLISHING,
    PUB_RETRYABLE_STATES,
    PUB_TRIGGER_AUTOMATIC,
    PUB_TRIGGER_MANUAL,
    PUB_TRIGGERS,
    PUB_UNCERTAIN,
    TP_APPROVED,
    Article,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
)
from app.social.threads.errors import ThreadsError
from app.social.threads.models import (
    CONTAINER_ERROR,
    CONTAINER_EXPIRED,
    CONTAINER_PUBLISHED,
    MEDIA_METRICS,
    RECOMMENDED_PUBLISH_DELAY_SECONDS,
    TEXT_MAX_LENGTH,
)
from app.social.threads.service import ThreadsService

#: 間隔の上書きを行える唯一の主体。自動の公開には使わせない。
GAP_OVERRIDE_SOURCE_HUMAN_CLI = "human-cli"

STEP_CREATE = "create_container"
STEP_PUBLISH = "publish_container"
STEP_READBACK = "readback"
STEP_RECONCILE = "reconcile"


class ThreadsPublicationError(ApplicationError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"threads publication error: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class ManualGapOverride:
    """人が明示した、120 分の間隔 **だけ** の上書き (T4.3)。

    飛ばすのは「前回の公開からの間隔」だけ。承認・stale・中身の整合性・二重投稿
    防止・他の公開の不確定状態 は、すべてそのまま効く。理由は必須で、公開の行に
    残る。自動の公開は作れない (``source`` は human-cli だけ)。
    """

    reason: str
    source: str = GAP_OVERRIDE_SOURCE_HUMAN_CLI

    def __post_init__(self) -> None:
        if self.source != GAP_OVERRIDE_SOURCE_HUMAN_CLI:
            raise ValueError("only an explicit human CLI run may override the posting gap")
        if not (self.reason or "").strip():
            raise ValueError("a gap override needs a non-empty reason")
        object.__setattr__(self, "reason", self.reason.strip()[:500])


@dataclass(frozen=True)
class ProposalAssessment:
    """1 件の提案の **中身** について、公開を止める事実 (T4.1 で切り出し)。

    設定や時刻には触れない。T3 の ``plan()`` と T4.1 の queue 評価が同じ判定を
    使うための共通部分で、ここを二重に実装しない。
    """

    stale_reasons: tuple[str, ...]
    integrity_reasons: tuple[str, ...]
    existing: ThreadsPublication | None

    @property
    def stale(self) -> bool:
        return bool(self.stale_reasons)

    @property
    def already_published(self) -> bool:
        existing = self.existing
        return existing is not None and (
            existing.status == PUB_PUBLISHED or bool(existing.threads_media_id)
        )

    @property
    def in_flight(self) -> bool:
        existing = self.existing
        return (
            existing is not None
            and not self.already_published
            and existing.status in PUB_IN_FLIGHT_STATES
        )


@dataclass
class PublishPlan:
    """公開前に人が読む計画。**外部呼び出しは 1 度も行わない。**"""

    proposal_id: int
    source_article_id: int
    source_article_title: str
    angle: str
    publish_text: str
    character_count: int
    link_mode: str
    destination_url: str | None
    proposal_hash: str
    proposal_status: str
    stale: bool
    stale_reasons: list[str] = field(default_factory=list)
    existing_publication: dict | None = None
    threads_config: dict = field(default_factory=dict)
    blocked_reasons: list[str] = field(default_factory=list)
    would_call: list[str] = field(default_factory=list)
    #: T4.3: 公開を始める主体と、間隔の判定。
    trigger: str = PUB_TRIGGER_MANUAL
    gap: dict = field(default_factory=dict)
    gap_overridden_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.blocked_reasons

    def as_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "source_article_id": self.source_article_id,
            "source_article_title": self.source_article_title,
            "angle": self.angle,
            "publish_text": self.publish_text,
            "character_count": self.character_count,
            "link_mode": self.link_mode,
            "destination_url": self.destination_url,
            "proposal_hash": self.proposal_hash,
            "proposal_status": self.proposal_status,
            "stale": self.stale,
            "stale_reasons": list(self.stale_reasons),
            "existing_publication": self.existing_publication,
            "threads_config": self.threads_config,
            "blocked_reasons": list(self.blocked_reasons),
            "would_call": list(self.would_call),
            "trigger": self.trigger,
            "gap": dict(self.gap),
            "gap_overridden_reason": self.gap_overridden_reason,
            "eligible": self.ok,
        }


@dataclass
class PublishOutcome:
    proposal_id: int
    executed: bool
    outcome: str
    publication_id: int | None = None
    creation_id: str | None = None
    media_id: str | None = None
    permalink: str | None = None
    published_at: str | None = None
    reconciliation_required: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    error: dict | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "executed": self.executed,
            "outcome": self.outcome,
            "publication_id": self.publication_id,
            "creation_id": self.creation_id,
            "media_id": self.media_id,
            "permalink": self.permalink,
            "published_at": self.published_at,
            "reconciliation_required": self.reconciliation_required,
            "blocked_reasons": list(self.blocked_reasons),
            "error": self.error,
            "notes": list(self.notes),
        }


class ThreadsPublicationService:
    def __init__(
        self,
        session: Session,
        *,
        settings,
        threads_service: ThreadsService | None = None,
        sleep=time.sleep,
        gap_minutes: int | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._threads = threads_service or ThreadsService(settings, sleep=sleep)
        self._sleep = sleep
        if gap_minutes is None:
            from app.social.threads.policy import get_operations_policy

            gap_minutes = get_operations_policy().soft_min_gap_minutes
        #: 前回の公開からの最小間隔 (T4.3 で書き込み経路に入れた)。
        self._gap = timedelta(minutes=int(gap_minutes))

    # -- plan ------------------------------------------------------------------
    def plan(
        self,
        *,
        proposal_id: int,
        now: datetime | None = None,
        trigger: str = PUB_TRIGGER_MANUAL,
        gap_override: ManualGapOverride | None = None,
    ) -> PublishPlan:
        """公開できるかだけを判定する。**Meta へは 1 度も書かない。**

        T4.3 で、書き込み経路そのものに 2 つの条件を足した (手動でも自動でも同じ):

        - **他の公開が不確定・照合待ちなら出さない。** 照合が済むまで次へ進まない。
        - **前回の公開から 120 分空いていなければ出さない。** 人が理由を付けて明示した
          ときだけ上書きできる。自動の公開は上書きできない。
        """

        now = ensure_aware(now or datetime.now(UTC))
        if trigger not in PUB_TRIGGERS:
            raise ThreadsPublicationError(f"unknown publication trigger {trigger!r}")
        if trigger == PUB_TRIGGER_AUTOMATIC and gap_override is not None:
            raise ThreadsPublicationError("automatic publication can never override the gap")
        proposal = self._require_proposal(proposal_id)
        article = self._session.get(Article, proposal.source_article_id)
        status = self._threads.describe()
        assessment = self.assess(proposal, article=article)
        stale, reasons = assessment.stale, list(assessment.stale_reasons)

        plan = PublishPlan(
            proposal_id=proposal.id,
            source_article_id=proposal.source_article_id,
            source_article_title=getattr(article, "title", "") or "",
            angle=proposal.angle,
            publish_text=proposal.content_text,
            character_count=proposal.character_count,
            link_mode=proposal.link_mode,
            destination_url=proposal.destination_url,
            proposal_hash=proposal.proposal_hash,
            proposal_status=proposal.status,
            stale=stale,
            stale_reasons=reasons,
            threads_config={
                "enabled": status.enabled,
                "configured": status.configured,
                "api_version": status.api_version,
                "user_id_configured": status.user_id_configured,
                "access_token_configured": status.access_token_configured,
            },
        )

        if proposal.status != TP_APPROVED:
            plan.blocked_reasons.append(
                f"proposal status is {proposal.status!r}; "
                "only an approved proposal can be published"
            )
        if stale:
            plan.blocked_reasons.extend(reasons)
        plan.blocked_reasons.extend(assessment.integrity_reasons)
        if not status.configured:
            plan.blocked_reasons.append(
                "Threads is not enabled/configured; set THREADS_ENABLED, "
                "THREADS_USER_ID and THREADS_ACCESS_TOKEN"
            )

        existing = assessment.existing
        if existing is not None:
            plan.existing_publication = _publication_summary(existing)
            if assessment.already_published:
                plan.blocked_reasons.append(
                    f"this proposal is already published (media {existing.threads_media_id}); "
                    "a proposal is never published twice"
                )
            elif assessment.in_flight:
                plan.blocked_reasons.append(
                    f"a previous attempt is {existing.status!r}; reconcile it before retrying "
                    "(publish_threads_post.py --reconcile)"
                )

        plan.trigger = trigger
        self._check_other_publications(plan, proposal.id)
        self._check_gap(plan, proposal.id, now, gap_override)

        if plan.ok:
            plan.would_call = [
                "POST /{user-id}/threads (media_type=TEXT) -- create the container",
                f"wait {RECOMMENDED_PUBLISH_DELAY_SECONDS}s (officially recommended)",
                "POST /{user-id}/threads_publish (creation_id) -- publish",
                "GET /{media-id}?fields=... -- read the published post back",
            ]
        return plan

    # -- execute ---------------------------------------------------------------
    def publish(
        self,
        *,
        proposal_id: int,
        execute: bool = False,
        now: datetime | None = None,
        trigger: str = PUB_TRIGGER_MANUAL,
        gap_override: ManualGapOverride | None = None,
    ) -> PublishOutcome:
        """``execute=True`` のときだけ外へ出す。既定は PLAN と同じ。"""

        now = now or datetime.now(UTC)
        plan = self.plan(
            proposal_id=proposal_id, now=now, trigger=trigger, gap_override=gap_override
        )
        outcome = PublishOutcome(
            proposal_id=proposal_id,
            executed=False,
            outcome="planned" if plan.ok else "blocked",
            blocked_reasons=list(plan.blocked_reasons),
        )
        if not plan.ok or not execute:
            return outcome

        proposal = self._require_proposal(proposal_id)
        row = self._claim(proposal, now)
        if row is not None:
            # 誰が始めたか・間隔を上書きしたか を、外へ出す前に記録する。
            row.trigger = trigger
            row.gap_override_reason = plan.gap_overridden_reason
            row.gap_override_at = to_storage_utc(now) if plan.gap_overridden_reason else None
            self._session.commit()
        if row is None:
            outcome.outcome = "blocked"
            outcome.blocked_reasons.append(
                "another publication row already exists for this proposal"
            )
            return outcome
        outcome.publication_id = row.id
        outcome.executed = True

        # 1) コンテナを作る。
        self._set(row, PUB_CREATING, reason="creating the media container")
        started = datetime.now(UTC)
        try:
            container = self._threads.client.create_text_container(row.exact_published_text)
        except ThreadsError as exc:
            self._attempt(row, STEP_CREATE, "failed", started, error=exc)
            # コンテナ作成で落ちたなら、外には何も出ていない。再試行してよい。
            self._fail(row, exc, now=now)
            outcome.outcome = "failed"
            outcome.error = exc.as_dict()
            return outcome
        row.threads_creation_id = container.creation_id
        outcome.creation_id = container.creation_id
        self._attempt(
            row, STEP_CREATE, "succeeded", started, detail={"creation_id": container.creation_id}
        )
        self._set(row, PUB_CONTAINER_CREATED, reason="container created; waiting before publish")

        # 2) 公式が推奨する待ち時間を守る。
        self._sleep(RECOMMENDED_PUBLISH_DELAY_SECONDS)

        # 3) 公開する。ここから先の失敗は「出たかどうか分からない」になりうる。
        self._set(row, PUB_PUBLISHING, reason="publishing the container")
        started = datetime.now(UTC)
        try:
            publication = self._threads.client.publish_container(container.creation_id)
        except ThreadsError as exc:
            self._attempt(row, STEP_PUBLISH, "uncertain", started, error=exc)
            # 応答を取りこぼしただけかもしれない。**再送しない。**
            self._set(
                row,
                PUB_UNCERTAIN,
                reason=(
                    "the publish call did not return a media id; the post may or may not exist. "
                    "reconcile against the container status before any retry"
                ),
            )
            row.error_category = exc.category
            row.error_message = exc.reason
            row.last_checked_at = to_storage_utc(now)
            self._session.commit()
            outcome.outcome = "uncertain"
            outcome.error = exc.as_dict()
            outcome.notes.append(
                "run publish_threads_post.py --reconcile before attempting anything else"
            )
            return outcome

        row.threads_media_id = publication.media_id
        outcome.media_id = publication.media_id
        self._attempt(
            row, STEP_PUBLISH, "succeeded", started, detail={"media_id": publication.media_id}
        )
        self._finish_published(row, now)
        outcome.outcome = "published"

        # 4) 読み戻して、送ったつもりと実際が一致するか確かめる。
        self._readback(row, outcome, now)
        return outcome

    # -- reconcile -------------------------------------------------------------
    def reconcile(self, *, proposal_id: int, now: datetime | None = None) -> PublishOutcome:
        """``uncertain`` な行を、コンテナ状態の照会で確定させる。

        **推測で決めない。** Threads 側が ``PUBLISHED`` と言えば公開は成立して
        いたとみなし、``ERROR``/``EXPIRED`` なら出ていないとみなす。どちらとも
        言えない間は ``uncertain`` のままにして、人が待てるようにする。
        """

        now = now or datetime.now(UTC)
        row = self._existing(proposal_id)
        outcome = PublishOutcome(proposal_id=proposal_id, executed=False, outcome="blocked")
        if row is None:
            outcome.blocked_reasons.append("no publication attempt exists for this proposal")
            return outcome
        outcome.publication_id = row.id
        outcome.creation_id = row.threads_creation_id

        if row.status == PUB_PUBLISHED and row.threads_media_id:
            outcome.outcome = "published"
            outcome.media_id = row.threads_media_id
            outcome.notes.append("already reconciled; nothing to do")
            return outcome
        if not row.threads_creation_id:
            # コンテナすら作れていない = 外には何も出ていない。
            self._set(row, PUB_FAILED, reason="no container was ever created")
            outcome.outcome = "failed"
            outcome.notes.append("nothing reached Threads; this proposal may be retried")
            return outcome

        started = datetime.now(UTC)
        try:
            payload = self._threads.client.fetch_container_status(row.threads_creation_id)
        except ThreadsError as exc:
            self._attempt(row, STEP_RECONCILE, "failed", started, error=exc)
            row.last_checked_at = to_storage_utc(now)
            self._session.commit()
            outcome.outcome = "uncertain"
            outcome.error = exc.as_dict()
            outcome.notes.append("the container status could not be read; stay uncertain")
            return outcome

        status = str(payload.get("status") or "").upper()
        detail = {"status": status, "error_message": payload.get("error_message")}
        self._attempt(row, STEP_RECONCILE, "succeeded", started, detail=detail)
        row.last_checked_at = to_storage_utc(now)
        outcome.executed = True

        if status == CONTAINER_PUBLISHED:
            # 公開は成立していた。media id は読み戻しで拾う。
            self._finish_published(row, now)
            outcome.outcome = "published"
            outcome.notes.append(
                "the container reports PUBLISHED; the earlier response was simply lost"
            )
            self._readback(row, outcome, now)
            return outcome
        if status in (CONTAINER_ERROR, CONTAINER_EXPIRED):
            self._set(
                row,
                PUB_FAILED,
                reason=f"the container reports {status} ({payload.get('error_message')})",
            )
            outcome.outcome = "failed"
            outcome.notes.append("nothing was published; this proposal may be retried")
            return outcome

        # IN_PROGRESS / FINISHED / 未知の値。まだ判断しない。
        self._session.commit()
        outcome.outcome = "uncertain"
        outcome.notes.append(
            f"the container reports {status or 'an unknown status'}; "
            "wait and reconcile again rather than retrying"
        )
        return outcome

    # -- insights (read-only) ---------------------------------------------------
    def inspect(self, *, publication_id: int, metrics=MEDIA_METRICS) -> dict:
        """公開済み投稿を読むだけ。**書き込みはしない。**"""

        row = self._session.get(ThreadsPublication, publication_id)
        if row is None:
            raise ThreadsPublicationError(f"threads publication {publication_id} not found")
        if not row.threads_media_id:
            raise ThreadsPublicationError("this publication has no media id yet")

        media = self._threads.fetch_publication(row.threads_media_id)
        insights = self._threads.media_insights(row.threads_media_id, metrics)
        return {
            "publication_id": row.id,
            "proposal_id": row.proposal_id,
            "media_id": row.threads_media_id,
            "permalink": row.permalink,
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "text_matches_approved": media.get("text") == row.exact_published_text,
            "media": {k: media.get(k) for k in ("id", "permalink", "timestamp", "username")},
            "insights": insights.as_dict(),
        }

    # -- internals -------------------------------------------------------------
    def _readback(self, row: ThreadsPublication, outcome: PublishOutcome, now: datetime) -> None:
        """公開後に読み戻し、承認内容と一致するか確かめる。

        違っていても **提案は書き換えない**。承認された内容が「送るべきだったもの」
        であり続け、差異は照合すべき問題として立てる。
        """

        if not row.threads_media_id:
            return
        started = datetime.now(UTC)
        try:
            media = self._threads.fetch_publication(row.threads_media_id)
        except ThreadsError as exc:
            self._attempt(row, STEP_READBACK, "failed", started, error=exc)
            outcome.notes.append("the published post could not be read back; verify manually")
            self._session.commit()
            return

        row.permalink = media.get("permalink")
        row.remote_timestamp = media.get("timestamp")
        row.remote_username = media.get("username")
        outcome.permalink = row.permalink
        remote_text = media.get("text")
        matches = remote_text == row.exact_published_text
        self._attempt(
            row,
            STEP_READBACK,
            "succeeded" if matches else "mismatch",
            started,
            detail={
                "permalink": row.permalink,
                "timestamp": row.remote_timestamp,
                "username": row.remote_username,
                "text_matches_approved": matches,
            },
        )
        if not matches:
            row.reconciliation_required = True
            row.reconciliation_note = (
                "the published text differs from the approved text; the approved proposal "
                "remains authoritative for what should have been sent"
            )
            outcome.reconciliation_required = True
            outcome.notes.append("published text does not match the approved text")
        if row.remote_username:
            # どのアカウントに出たかは必ず人へ見せる (取り違えを黙って通さない)。
            outcome.notes.append(f"published as @{row.remote_username}")
        self._session.commit()

    def _claim(self, proposal: ThreadsPostProposal, now: datetime) -> ThreadsPublication | None:
        """公開行を 1 つだけ確保する。並行実行では片方しか取れない。"""

        existing = self._existing(proposal.id)
        if existing is not None:
            if existing.status in PUB_RETRYABLE_STATES:
                existing.retry_count += 1
                existing.status = PUB_PLANNED
                existing.publish_started_at = to_storage_utc(now)
                self._session.commit()
                return existing
            return None

        row = ThreadsPublication(
            proposal_id=proposal.id,
            proposal_hash=proposal.proposal_hash,
            source_article_id=proposal.source_article_id,
            angle=proposal.angle,
            exact_published_text=proposal.content_text,
            destination_url=proposal.destination_url,
            status=PUB_PLANNED,
            publish_started_at=to_storage_utc(now),
        )
        self._session.add(row)
        try:
            self._session.commit()
        except IntegrityError:
            # 並行実行。UNIQUE(proposal_id) が二重投稿を防いだ。
            self._session.rollback()
            return None
        self._session.refresh(row)
        return row

    def assess(self, proposal: ThreadsPostProposal, *, article=None) -> ProposalAssessment:
        """提案の中身だけを判定する。**外部にも DB の書き込みにも触れない。**"""

        if article is None:
            article = self._session.get(Article, proposal.source_article_id)
        _stale, stale_reasons = self._staleness(proposal, article)
        integrity: list[str] = []
        if proposal.character_count != len(proposal.content_text):
            integrity.append("the stored character count does not match the text")
        if not (0 < len(proposal.content_text) <= TEXT_MAX_LENGTH):
            integrity.append(f"the text must be between 1 and {TEXT_MAX_LENGTH} characters")
        return ProposalAssessment(
            stale_reasons=tuple(stale_reasons),
            integrity_reasons=tuple(integrity),
            existing=self._existing(proposal.id),
        )

    def _check_other_publications(self, plan: PublishPlan, proposal_id: int) -> None:
        """他の公開が不確定・照合待ちなら、**どの提案も** 出さない (T4.3)。"""

        blocking = self._session.scalars(
            select(ThreadsPublication)
            .where(
                ThreadsPublication.proposal_id != proposal_id,
                or_(
                    ThreadsPublication.status.in_(tuple(PUB_IN_FLIGHT_STATES)),
                    ThreadsPublication.reconciliation_required.is_(True),
                ),
            )
            .order_by(ThreadsPublication.id)
        ).all()
        for row in blocking:
            plan.blocked_reasons.append(
                f"publication {row.id} is {row.status!r}"
                + (" and requires reconciliation" if row.reconciliation_required else "")
                + "; reconcile it before publishing anything else"
            )

    def _check_gap(
        self,
        plan: PublishPlan,
        proposal_id: int,
        now: datetime,
        gap_override: ManualGapOverride | None,
    ) -> None:
        """前回の **実際の** 公開から間隔が空いているか (T4.3、書き込み経路)。"""

        last = self._session.scalars(
            select(ThreadsPublication)
            .where(
                ThreadsPublication.proposal_id != proposal_id,
                ThreadsPublication.status == PUB_PUBLISHED,
                ThreadsPublication.published_at.is_not(None),
            )
            .order_by(ThreadsPublication.published_at.desc())
            .limit(1)
        ).first()
        minutes = int(self._gap.total_seconds() // 60)
        if last is None:
            plan.gap = {"minutes": minutes, "last_publication_id": None, "elapsed": True}
            return
        last_at = ensure_aware(last.published_at)
        earliest = last_at + self._gap
        elapsed = now >= earliest
        plan.gap = {
            "minutes": minutes,
            "last_publication_id": last.id,
            "last_published_at": last_at.isoformat(),
            "earliest_at": earliest.isoformat(),
            "elapsed": elapsed,
        }
        if elapsed:
            return
        if gap_override is not None:
            plan.gap_overridden_reason = gap_override.reason
            plan.gap["overridden"] = True
            return
        plan.blocked_reasons.append(
            f"the {minutes}-minute gap since publication {last.id} has not elapsed "
            f"(earliest {earliest.isoformat()}); a human may override it with a reason"
        )

    def _existing(self, proposal_id: int) -> ThreadsPublication | None:
        return self._session.scalars(
            select(ThreadsPublication).where(ThreadsPublication.proposal_id == proposal_id)
        ).first()

    def _staleness(self, proposal: ThreadsPostProposal, article) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if article is None:
            return True, ["the source article no longer exists"]
        if compute_text_hash(article.body or "") != proposal.source_article_body_hash:
            reasons.append("the source article changed after the proposal was approved")
        if str(article.status) != "published":
            reasons.append("the source article is no longer published")
        if proposal.link_mode == "article" and not article.published_url:
            reasons.append("the source article lost its published URL")
        return bool(reasons), reasons

    def _set(self, row: ThreadsPublication, status: str, *, reason: str) -> None:
        row.status = status
        row.status_reason = reason
        self._session.commit()

    def _fail(self, row: ThreadsPublication, exc: ThreadsError, *, now: datetime) -> None:
        row.status = PUB_FAILED
        row.status_reason = exc.reason
        row.error_category = exc.category
        row.error_message = exc.reason
        row.failed_at = to_storage_utc(now)
        self._session.commit()

    def _finish_published(self, row: ThreadsPublication, now: datetime) -> None:
        row.status = PUB_PUBLISHED
        row.status_reason = "published"
        row.published_at = to_storage_utc(now)
        row.error_category = None
        row.error_message = None
        self._session.commit()

    def _attempt(
        self,
        row: ThreadsPublication,
        step: str,
        outcome: str,
        started: datetime,
        *,
        detail: dict | None = None,
        error: ThreadsError | None = None,
    ) -> None:
        self._session.add(
            ThreadsPublicationAttempt(
                threads_publication_id=row.id,
                step=step,
                outcome=outcome,
                detail_json=detail,
                error_category=error.category if error else None,
                error_message=error.reason if error else None,
                started_at=to_storage_utc(started),
                finished_at=to_storage_utc(datetime.now(UTC)),
            )
        )
        self._session.commit()

    def _require_proposal(self, proposal_id: int) -> ThreadsPostProposal:
        row = self._session.get(ThreadsPostProposal, proposal_id)
        if row is None:
            raise ThreadsPublicationError(f"threads post proposal {proposal_id} not found")
        return row


def _publication_summary(row: ThreadsPublication) -> dict:
    return {
        "publication_id": row.id,
        "status": row.status,
        "media_id": row.threads_media_id,
        "creation_id": row.threads_creation_id,
        "permalink": row.permalink,
        "retry_count": row.retry_count,
        "reconciliation_required": row.reconciliation_required,
        "published_at": row.published_at.isoformat() if row.published_at else None,
    }
