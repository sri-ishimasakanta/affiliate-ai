"""ChangeRequestService -- 提案の作成・承認・却下・陳腐化判定 (C9.1)。

C6/C7 の候補は **助言** であって指示ではない。この service は、人が判断できる
「正確で不変な 1 つの提案」に変換し、承認をその提案 1 つに結び付ける。

守る約束:

- 候補を **自動で提案に変えない**。呼び出し側が候補 id を明示したときだけ作る。
- 提案の内容 (本文・アンカー・挿入位置・前後 hash) を作成時に凍結する。
  内容が変われば新しい ``proposal_version`` の別 request になる。
- 承認は ``proposal_hash`` を要求する。ズレていれば承認しない。
- 承認だけでは **WordPress は何も変わらない** (適用は別 service・別コマンド)。
- 候補が消えても request を勝手に取り消さない。``candidate_currently_present``
  として提示し、判断は人に残す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import to_storage_utc
from app.change.internal_link import (
    build_internal_link_proposal,
    compute_proposal_hash,
    has_link_to,
)
from app.exceptions import ApplicationError
from app.models import (
    CHANGE_ADD_INTERNAL_LINK,
    CR_APPROVED,
    CR_AWAITING_APPROVAL,
    CR_OPEN_STATUSES,
    CR_REJECTED,
    CR_STALE,
    Article,
    ChangeRequest,
    ChangeRequestApproval,
    SeoImprovementCandidate,
    SeoImprovementRun,
    change_request_transition_allowed,
)


class ChangeRequestError(ApplicationError):
    """提案/承認の不正な操作。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"change request error: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class StalenessReport:
    """適用前/承認前に確認する「まだこの提案は成り立つか」。"""

    stale: bool
    reasons: tuple[str, ...]
    current_source_body_hash: str | None
    candidate_currently_present: bool | None
    target_url: str | None


class ChangeRequestService:
    def __init__(self, session: Session) -> None:
        self._session = session

    # -- 提案の作成 -----------------------------------------------------------
    def propose_from_seo_candidate(
        self,
        *,
        candidate_id: int,
        change_type: str = CHANGE_ADD_INTERNAL_LINK,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ChangeRequest:
        """明示された C6 候補 1 件から、正確な提案を 1 つ作る。

        承認も適用も行わない -- 作るのは ``awaiting_approval`` の request だけ。
        """

        now = now or datetime.now(UTC)
        if change_type != CHANGE_ADD_INTERNAL_LINK:
            raise ChangeRequestError(
                f"change type {change_type!r} is representable but not generated in V1"
            )

        candidate = self._session.get(SeoImprovementCandidate, candidate_id)
        if candidate is None:
            raise ChangeRequestError(f"seo candidate {candidate_id} not found")
        if candidate.candidate_type != "INTERNAL_LINK_OPPORTUNITY":
            raise ChangeRequestError(
                f"candidate {candidate_id} is {candidate.candidate_type}, "
                "which cannot produce an internal-link proposal"
            )

        source = self._session.get(Article, candidate.article_id)
        target_id = (candidate.evidence_json or {}).get("target_article_id")
        target = self._session.get(Article, target_id) if target_id else None
        if source is None or target is None:
            raise ChangeRequestError("source or target article is missing")
        if str(source.status) != "published" or str(target.status) != "published":
            raise ChangeRequestError("both articles must be published")

        proposal, rejection = build_internal_link_proposal(
            source_article_id=source.id,
            source_body=source.body or "",
            target_article_id=target.id,
            target_title=target.title or "",
            target_url=target.published_url or "",
            body_hasher=compute_text_hash,
        )
        if proposal is None:
            raise ChangeRequestError(f"{rejection.reason_code}: {rejection.detail}")

        proposal_hash = compute_proposal_hash(
            change_type=change_type,
            source_article_id=source.id,
            target_article_id=target.id,
            source_body_hash=proposal.source_body_hash,
            proposed_body_hash=proposal.proposed_body_hash,
            anchor_text=proposal.anchor_text,
            inserted_paragraph=proposal.inserted_paragraph,
            insertion_line=proposal.insertion_line,
        )

        existing = self._session.scalars(
            select(ChangeRequest).where(
                ChangeRequest.article_id == source.id,
                ChangeRequest.proposal_hash == proposal_hash,
            )
        ).first()
        if existing is not None:
            # 同じ内容の提案は作り直さない (二重に人へ出さない)。
            return existing

        # 同じ記事 x 同じリンク先の過去提案があれば、版を上げる。
        previous = self._session.scalars(
            select(ChangeRequest)
            .where(
                ChangeRequest.article_id == source.id,
                ChangeRequest.target_article_id == target.id,
                ChangeRequest.change_type == change_type,
            )
            .order_by(ChangeRequest.proposal_version.desc())
            .limit(1)
        ).first()
        version = (previous.proposal_version + 1) if previous else 1

        run = self._session.get(SeoImprovementRun, candidate.seo_improvement_run_id)
        request = ChangeRequest(
            source_engine="seo",
            source_candidate_run_id=candidate.seo_improvement_run_id,
            source_candidate_id=candidate.id,
            source_candidate_dedupe_key=candidate.dedupe_key,
            source_candidate_type=candidate.candidate_type,
            source_candidate_priority=candidate.priority,
            source_policy_version=run.policy_version if run else None,
            article_id=source.id,
            target_article_id=target.id,
            change_type=change_type,
            proposal_version=version,
            proposal_hash=proposal_hash,
            expected_source_body_hash=proposal.source_body_hash,
            proposed_body_hash=proposal.proposed_body_hash,
            proposed_body=proposal.proposed_body,
            rationale=(
                f"C6 candidate {candidate.id} ({candidate.priority}) proposes linking "
                f"article {source.id} to article {target.id}: "
                f"{(candidate.evidence_json or {}).get('relation', '')}"
            ),
            proposal_json=proposal.as_dict(),
            evidence_json=candidate.evidence_json or {},
            status=CR_AWAITING_APPROVAL,
            idempotency_key=idempotency_key,
            created_at=to_storage_utc(now),
        )
        self._session.add(request)
        self._session.commit()
        self._session.refresh(request)
        return request

    # -- 状態確認 -------------------------------------------------------------
    def evaluate_staleness(self, request: ChangeRequest) -> StalenessReport:
        """「まだこの提案は成り立つか」をローカル状態から判定する。

        WordPress への問い合わせは行わない (適用 service が別途行う)。
        """

        reasons: list[str] = []
        source = self._session.get(Article, request.article_id)
        current_hash = compute_text_hash(source.body or "") if source else None
        if source is None:
            reasons.append("source article no longer exists")
        elif current_hash != request.expected_source_body_hash:
            reasons.append(
                "source article body changed after the proposal was created "
                f"(expected {request.expected_source_body_hash[:16]}, "
                f"now {(current_hash or '')[:16]})"
            )

        target_url = None
        if request.target_article_id is not None:
            target = self._session.get(Article, request.target_article_id)
            if target is None or str(target.status) != "published":
                reasons.append("target article is no longer published")
            else:
                target_url = target.published_url
                proposed_url = (request.proposal_json or {}).get("target_url")
                if proposed_url and target_url != proposed_url:
                    reasons.append(f"target canonical URL changed ({proposed_url} -> {target_url})")
                if source is not None and has_link_to(source.body or "", proposed_url or ""):
                    reasons.append("the source article already links to the target")

        return StalenessReport(
            stale=bool(reasons),
            reasons=tuple(reasons),
            current_source_body_hash=current_hash,
            candidate_currently_present=self.candidate_currently_present(request),
            target_url=target_url,
        )

    def candidate_currently_present(self, request: ChangeRequest) -> bool | None:
        """元になった候補が **最新の** 評価にまだ存在するか。

        消えていても request は取り消さない -- 「消えた = 直った」ではないので、
        判断は人に残す。
        """

        if request.source_engine != "seo" or not request.source_candidate_dedupe_key:
            return None
        latest = self._session.scalars(
            select(SeoImprovementRun).order_by(SeoImprovementRun.id.desc()).limit(1)
        ).first()
        if latest is None:
            return None
        return (
            self._session.scalars(
                select(SeoImprovementCandidate).where(
                    SeoImprovementCandidate.seo_improvement_run_id == latest.id,
                    SeoImprovementCandidate.dedupe_key == request.source_candidate_dedupe_key,
                )
            ).first()
            is not None
        )

    # -- 承認 / 却下 ----------------------------------------------------------
    def approve(
        self,
        request_id: int,
        *,
        proposal_hash: str,
        decided_by: str = "human",
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ChangeRequestApproval:
        """**この 1 つの提案** を承認する。hash がズレていれば承認しない。"""

        request = self._require(request_id)
        if request.proposal_hash != proposal_hash:
            raise ChangeRequestError(
                "proposal hash mismatch: the proposal changed since it was reviewed; "
                "re-read it and approve the current hash"
            )
        if request.status not in CR_OPEN_STATUSES:
            raise ChangeRequestError(
                f"request {request_id} is {request.status!r} and cannot be approved"
            )
        if not change_request_transition_allowed(request.status, CR_APPROVED):
            raise ChangeRequestError(f"'{request.status}' -> '{CR_APPROVED}' is not allowed")

        now = now or datetime.now(UTC)
        approval = ChangeRequestApproval(
            change_request_id=request.id,
            decision="approved",
            approved_proposal_hash=request.proposal_hash,
            approved_proposal_version=request.proposal_version,
            decided_by=decided_by,
            reason=reason,
            candidate_present_at_decision=self.candidate_currently_present(request),
            created_at=to_storage_utc(now),
        )
        self._session.add(approval)
        request.status = CR_APPROVED
        request.status_reason = reason
        self._session.commit()
        self._session.refresh(approval)
        return approval

    def reject(
        self,
        request_id: int,
        *,
        reason: str,
        decided_by: str = "human",
        now: datetime | None = None,
    ) -> ChangeRequestApproval:
        request = self._require(request_id)
        if not (reason or "").strip():
            raise ChangeRequestError("a rejection reason is required")
        if not change_request_transition_allowed(request.status, CR_REJECTED):
            raise ChangeRequestError(f"'{request.status}' -> '{CR_REJECTED}' is not allowed")
        now = now or datetime.now(UTC)
        approval = ChangeRequestApproval(
            change_request_id=request.id,
            decision="rejected",
            approved_proposal_hash=request.proposal_hash,
            approved_proposal_version=request.proposal_version,
            decided_by=decided_by,
            reason=reason,
            candidate_present_at_decision=self.candidate_currently_present(request),
            created_at=to_storage_utc(now),
        )
        self._session.add(approval)
        request.status = CR_REJECTED
        request.status_reason = reason
        self._session.commit()
        self._session.refresh(approval)
        return approval

    def mark_stale(self, request_id: int, *, reason: str) -> ChangeRequest:
        request = self._require(request_id)
        if not change_request_transition_allowed(request.status, CR_STALE):
            raise ChangeRequestError(f"'{request.status}' -> '{CR_STALE}' is not allowed")
        request.status = CR_STALE
        request.status_reason = reason
        self._session.commit()
        return request

    def latest_approval(self, request: ChangeRequest) -> ChangeRequestApproval | None:
        return self._session.scalars(
            select(ChangeRequestApproval)
            .where(
                ChangeRequestApproval.change_request_id == request.id,
                ChangeRequestApproval.decision == "approved",
            )
            .order_by(ChangeRequestApproval.id.desc())
            .limit(1)
        ).first()

    def list_requests(self, *, status: str | None = None) -> list[ChangeRequest]:
        stmt = select(ChangeRequest).order_by(ChangeRequest.id)
        if status:
            stmt = stmt.where(ChangeRequest.status == status)
        return list(self._session.scalars(stmt).all())

    def _require(self, request_id: int) -> ChangeRequest:
        request = self._session.get(ChangeRequest, request_id)
        if request is None:
            raise ChangeRequestError(f"change request {request_id} not found")
        return request
