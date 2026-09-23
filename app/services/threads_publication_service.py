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
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import to_storage_utc
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

STEP_CREATE = "create_container"
STEP_PUBLISH = "publish_container"
STEP_READBACK = "readback"
STEP_RECONCILE = "reconcile"


class ThreadsPublicationError(ApplicationError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"threads publication error: {reason}")
        self.reason = reason


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
    ) -> None:
        self._session = session
        self._settings = settings
        self._threads = threads_service or ThreadsService(settings, sleep=sleep)
        self._sleep = sleep

    # -- plan ------------------------------------------------------------------
    def plan(self, *, proposal_id: int) -> PublishPlan:
        """公開できるかだけを判定する。**Meta へは 1 度も書かない。**"""

        proposal = self._require_proposal(proposal_id)
        article = self._session.get(Article, proposal.source_article_id)
        status = self._threads.describe()
        stale, reasons = self._staleness(proposal, article)

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
        if proposal.character_count != len(proposal.content_text):
            plan.blocked_reasons.append("the stored character count does not match the text")
        if not (0 < len(proposal.content_text) <= TEXT_MAX_LENGTH):
            plan.blocked_reasons.append(
                f"the text must be between 1 and {TEXT_MAX_LENGTH} characters"
            )
        if not status.configured:
            plan.blocked_reasons.append(
                "Threads is not enabled/configured; set THREADS_ENABLED, "
                "THREADS_USER_ID and THREADS_ACCESS_TOKEN"
            )

        existing = self._existing(proposal.id)
        if existing is not None:
            plan.existing_publication = _publication_summary(existing)
            if existing.status == PUB_PUBLISHED or existing.threads_media_id:
                plan.blocked_reasons.append(
                    f"this proposal is already published (media {existing.threads_media_id}); "
                    "a proposal is never published twice"
                )
            elif existing.status in PUB_IN_FLIGHT_STATES:
                plan.blocked_reasons.append(
                    f"a previous attempt is {existing.status!r}; reconcile it before retrying "
                    "(publish_threads_post.py --reconcile)"
                )

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
        self, *, proposal_id: int, execute: bool = False, now: datetime | None = None
    ) -> PublishOutcome:
        """``execute=True`` のときだけ外へ出す。既定は PLAN と同じ。"""

        now = now or datetime.now(UTC)
        plan = self.plan(proposal_id=proposal_id)
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
