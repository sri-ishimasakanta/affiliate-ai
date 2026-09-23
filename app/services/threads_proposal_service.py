"""ThreadsProposalService -- 記事から Threads 投稿案を作る (T2)。

C9 の変更提案と同じ約束をそのまま持ち込む:

- 提案は **明示的に指定した記事** からしか作らない。自動では作らない。
- 内容 (最終テキスト・リンク先・hash・記事本文 hash) を作成時に凍結する。
- 検査 (``errors``) を通らなければ **保存しない**。warnings は人に見せる。
- 同じ内容の提案を二重に作らない。
- 記事が変われば陳腐化する。作り直しは新しいレコードで、古いものは superseded。
- **承認も公開もしない。** 承認は C8.8、公開は T3 の担当である。

生成そのものは、このリポジトリの記事生成と同じく外部で人が動かす
(prompt をローカルで組み立て、結果を受け取って取り込む)。新しい AI provider の
抽象は増やさない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import to_storage_utc
from app.exceptions import ApplicationError
from app.models import (
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    TP_OPEN_STATES,
    TP_PROPOSED,
    TP_STALE,
    TP_SUPERSEDED,
    Article,
    ThreadsPostProposal,
    threads_proposal_transition_allowed,
)
from app.social.threads.policy import ThreadsStylePolicy, get_policy
from app.social.threads.prompt import ThreadsPromptPackage, build_prompt, parse_generated
from app.social.threads.proposal import (
    GENERATOR_VERSION,
    LINK_MODE_ARTICLE,
    ThreadsProposal,
    ThreadsProposalDraft,
    build_proposal,
)
from app.social.threads.validators import (
    find_duplicates,
    normalized_identity,
    validate_proposal,
)


class ThreadsProposalError(ApplicationError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"threads proposal error: {reason}")
        self.reason = reason


@dataclass
class PreparedProposals:
    """保存前の計画 (PLAN)。"""

    source_article_id: int
    source_article_title: str
    source_article_body_hash: str
    policy_version: str
    generator_version: str
    prompt: dict = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    @property
    def acceptable(self) -> int:
        return len(self.candidates)

    def as_dict(self) -> dict:
        return {
            "source_article_id": self.source_article_id,
            "source_article_title": self.source_article_title,
            "source_article_body_hash": self.source_article_body_hash,
            "policy_version": self.policy_version,
            "generator_version": self.generator_version,
            "prompt": self.prompt,
            "candidates": list(self.candidates),
            "rejected": list(self.rejected),
        }


class ThreadsProposalService:
    def __init__(self, session: Session, *, policy: ThreadsStylePolicy | None = None) -> None:
        self._session = session
        self._policy = policy or get_policy()

    # -- prompt ---------------------------------------------------------------
    def build_prompt(self, *, article_id: int, angles=None) -> ThreadsPromptPackage:
        """生成に使う prompt を決定的に組み立てる (外部呼び出しなし)。"""

        article = self._require_article(article_id)
        return build_prompt(
            source_article_id=article.id,
            source_article_title=article.title or "",
            source_article_body=article.body or "",
            source_article_body_hash=compute_text_hash(article.body or ""),
            angles=angles or self._policy.angles,
            policy=self._policy,
        )

    # -- proposal -------------------------------------------------------------
    def plan(self, *, article_id: int, generated_output: str) -> PreparedProposals:
        """生成結果を検査して、保存したら何ができるかを示す (保存はしない)。"""

        article = self._require_article(article_id)
        body_hash = compute_text_hash(article.body or "")
        prepared = PreparedProposals(
            source_article_id=article.id,
            source_article_title=article.title or "",
            source_article_body_hash=body_hash,
            policy_version=self._policy.policy_version,
            generator_version=GENERATOR_VERSION,
        )

        try:
            drafts = parse_generated(generated_output)
        except ValueError as exc:
            raise ThreadsProposalError(f"the generated output could not be read: {exc}") from None

        built: list[ThreadsProposal] = []
        for item in drafts:
            draft = ThreadsProposalDraft(
                angle=item["angle"], body=item["body"], link_mode=item["link_mode"]
            )
            proposal = build_proposal(
                draft,
                source_article_id=article.id,
                source_article_body_hash=body_hash,
                article_url=article.published_url,
                policy_version=self._policy.policy_version,
            )
            result = validate_proposal(proposal, self._policy)
            proposal.warnings = list(result.warnings)
            if not result.ok:
                prepared.rejected.append(
                    {
                        "angle": proposal.angle,
                        "errors": result.errors,
                        "preview": _preview(proposal),
                    }
                )
                continue
            if draft.link_mode == LINK_MODE_ARTICLE and not article.published_url:
                prepared.rejected.append(
                    {
                        "angle": proposal.angle,
                        "errors": ["the article has no published URL to link to"],
                        "preview": _preview(proposal),
                    }
                )
                continue
            built.append(proposal)

        # 同じ内容の案を並べない (言い回しだけ違う重複を作らない)。
        for _first, second in find_duplicates(built):
            prepared.rejected.append(
                {
                    "angle": built[second].angle,
                    "errors": ["duplicate of another proposal in the same batch"],
                    "preview": _preview(built[second]),
                }
            )
        duplicate_indexes = {second for _f, second in find_duplicates(built)}
        for index, proposal in enumerate(built):
            if index in duplicate_indexes:
                continue
            existing = self._existing(proposal)
            if existing is not None:
                prepared.rejected.append(
                    {
                        "angle": proposal.angle,
                        "errors": [f"an identical proposal already exists (#{existing.id})"],
                        "preview": _preview(proposal),
                    }
                )
                continue
            prepared.candidates.append({**proposal.as_dict(), "preview": _preview(proposal)})
        return prepared

    def persist(
        self, *, article_id: int, generated_output: str, now: datetime | None = None
    ) -> list[ThreadsPostProposal]:
        """検査を通った案だけを ``awaiting_approval`` として保存する。

        **承認はしない。公開もしない。**
        """

        now = now or datetime.now(UTC)
        prepared = self.plan(article_id=article_id, generated_output=generated_output)
        if not prepared.candidates:
            raise ThreadsProposalError(
                "no candidate passed validation; nothing was stored "
                f"({len(prepared.rejected)} rejected)"
            )

        rows: list[ThreadsPostProposal] = []
        for candidate in prepared.candidates:
            row = ThreadsPostProposal(
                source_article_id=candidate["source_article_id"],
                source_article_body_hash=candidate["source_article_body_hash"],
                angle=candidate["angle"],
                link_mode=candidate["link_mode"],
                content_text=candidate["publish_text"],
                character_count=candidate["character_count"],
                destination_url=candidate["destination_url"],
                content_seed=candidate["content_seed"],
                proposal_hash=candidate["proposal_hash"],
                policy_version=candidate["policy_version"],
                generator_version=candidate["generator_version"],
                status=TP_AWAITING_APPROVAL,
                warnings_json=candidate["warnings"] or None,
                created_at=to_storage_utc(now),
            )
            self._session.add(row)
            rows.append(row)
        self._session.commit()
        for row in rows:
            self._session.refresh(row)
        return rows

    # -- state ----------------------------------------------------------------
    def evaluate_staleness(self, proposal: ThreadsPostProposal) -> tuple[bool, list[str]]:
        """記事が変わって、この提案の前提が崩れていないか。"""

        reasons: list[str] = []
        article = self._session.get(Article, proposal.source_article_id)
        if article is None:
            reasons.append("the source article no longer exists")
            return True, reasons
        current = compute_text_hash(article.body or "")
        if current != proposal.source_article_body_hash:
            reasons.append(
                "the source article changed after the proposal was created "
                f"(expected {proposal.source_article_body_hash[:16]}, now {current[:16]})"
            )
        if str(article.status) != "published":
            reasons.append("the source article is no longer published")
        if proposal.link_mode == LINK_MODE_ARTICLE and not article.published_url:
            reasons.append("the source article lost its published URL")
        return bool(reasons), reasons

    def mark_stale(self, proposal_id: int, *, reason: str) -> ThreadsPostProposal:
        row = self._require(proposal_id)
        self._transition(row, TP_STALE, reason=reason)
        return row

    def supersede(
        self, proposal_id: int, *, superseded_by_id: int, now: datetime | None = None
    ) -> ThreadsPostProposal:
        """作り直したときに、古い提案を置き換え済みにする (消さない)。"""

        row = self._require(proposal_id)
        self._transition(row, TP_SUPERSEDED, reason=f"superseded by proposal {superseded_by_id}")
        row.superseded_by_id = superseded_by_id
        row.superseded_at = to_storage_utc(now or datetime.now(UTC))
        self._session.commit()
        return row

    def list_proposals(self, *, article_id=None, status=None) -> list[ThreadsPostProposal]:
        stmt = select(ThreadsPostProposal).order_by(ThreadsPostProposal.id)
        if article_id is not None:
            stmt = stmt.where(ThreadsPostProposal.source_article_id == article_id)
        if status:
            stmt = stmt.where(ThreadsPostProposal.status == status)
        return list(self._session.scalars(stmt).all())

    # -- internals ------------------------------------------------------------
    def _existing(self, proposal: ThreadsProposal) -> ThreadsPostProposal | None:
        by_hash = self._session.scalars(
            select(ThreadsPostProposal).where(
                ThreadsPostProposal.proposal_hash == proposal.proposal_hash
            )
        ).first()
        if by_hash is not None:
            return by_hash
        # 同じ記事に、正規化して同じ本文の生きた提案があれば重複とみなす。
        identity = normalized_identity(proposal.publish_text)
        for row in self._session.scalars(
            select(ThreadsPostProposal).where(
                ThreadsPostProposal.source_article_id == proposal.source_article_id,
                ThreadsPostProposal.status.in_(tuple(TP_OPEN_STATES) + (TP_APPROVED,)),
            )
        ).all():
            if normalized_identity(row.content_text) == identity:
                return row
        return None

    def _require_article(self, article_id: int) -> Article:
        article = self._session.get(Article, article_id)
        if article is None:
            raise ThreadsProposalError(f"article {article_id} not found")
        if str(article.status) != "published":
            raise ThreadsProposalError(f"article {article_id} is not published")
        return article

    def _require(self, proposal_id: int) -> ThreadsPostProposal:
        row = self._session.get(ThreadsPostProposal, proposal_id)
        if row is None:
            raise ThreadsProposalError(f"threads post proposal {proposal_id} not found")
        return row

    def _transition(self, row: ThreadsPostProposal, target: str, *, reason: str) -> None:
        if not threads_proposal_transition_allowed(row.status, target):
            raise ThreadsProposalError(f"'{row.status}' -> '{target}' is not allowed")
        row.status = target
        row.status_reason = reason
        self._session.commit()


def _preview(proposal: ThreadsProposal, *, width: int = 120) -> str:
    text = proposal.publish_text.replace("\n", " / ")
    return text if len(text) <= width else text[: width - 3] + "..."


__all__ = [
    "PreparedProposals",
    "ThreadsProposalError",
    "ThreadsProposalService",
    "TP_PROPOSED",
]
