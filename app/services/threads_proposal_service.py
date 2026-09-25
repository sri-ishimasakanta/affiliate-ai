"""ThreadsProposalService -- 記事から Threads 投稿案を作る (T2)。

C9 の変更提案と同じ約束をそのまま持ち込む:

- 提案は **明示的に指定した記事** からしか作らない。自動では作らない。
- 内容 (最終テキスト・リンク先・hash・記事本文 hash) を作成時に凍結する。
- 検査 (``errors``) を通らなければ **保存しない**。warnings は人に見せる。
- 同じ内容の提案を二重に作らない。
- 記事が変われば陳腐化する。作り直しは新しいレコードで、古いものは superseded。
- **承認も公開もしない。** 承認は C8.8、公開は T3 の担当である。
- T5.5: prompt に T5 の学習からの **弱い参考** を入れる。参考は検査 (errors) を
  変えず、既存の提案・承認・公開・並び順には触れない。これから作る提案の
  prompt と、その来歴 (``learning_guidance_json``) にだけ効く。

生成そのものは、このリポジトリの記事生成と同じく外部で人が動かす
(prompt をローカルで組み立て、結果を受け取って取り込む)。新しい AI provider の
抽象は増やさない。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import ensure_aware, to_storage_utc
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
from app.social.threads.guidance import (
    MODE_WEAK,
    ThreadsGenerationGuidance,
    build_guidance,
)
from app.social.threads.policy import (
    ThreadsStylePolicy,
    get_measurement_policy,
    get_policy,
)
from app.social.threads.prompt import ThreadsPromptPackage, build_prompt, parse_generated
from app.social.threads.proposal import (
    GENERATOR_VERSION,
    LINK_MODE_ARTICLE,
    LINK_MODES,
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
    #: この計画で使った学習の参考 (T5.5)。
    learning: dict = field(default_factory=dict)

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
            "learning": dict(self.learning),
        }


#: ``as_of`` を受け取って参考を返す関数 (テストで差し替える)。
GuidanceProvider = Callable[[datetime], ThreadsGenerationGuidance]


class ThreadsProposalService:
    def __init__(
        self,
        session: Session,
        *,
        policy: ThreadsStylePolicy | None = None,
        guidance_provider: GuidanceProvider | None = None,
    ) -> None:
        self._session = session
        self._policy = policy or get_policy()
        self._guidance_provider = guidance_provider

    # -- learning guidance (T5.5) ----------------------------------------------
    def learning_guidance(self, *, as_of: datetime | None = None) -> ThreadsGenerationGuidance:
        """``as_of`` の時点の T5 の学習結果から、生成への弱い参考を作る (読むだけ)。

        学習は計算し直さない。T5 の ``ThreadsLearningService`` が作る ``threads-learning/1``
        をそのまま読む。``as_of`` より後の観測は見ない (T5.1)。
        """

        as_of = ensure_aware(as_of or datetime.now(UTC))
        if self._guidance_provider is not None:
            return self._guidance_provider(as_of)
        from app.services.threads_learning_service import ThreadsLearningService

        measurement = get_measurement_policy()
        report = ThreadsLearningService(self._session, policy=measurement).report(now=as_of)
        return build_guidance(
            report,
            supported_values={
                "angle": tuple(self._policy.angles),
                "link_mode": LINK_MODES,
                "length_band": tuple(b["name"] for b in measurement.length_buckets),
            },
            length_bands=_length_bands(measurement),
        )

    # -- prompt ---------------------------------------------------------------
    def build_prompt(
        self,
        *,
        article_id: int,
        angles=None,
        learning_as_of: datetime | None = None,
        requested_link_mode: str | None = None,
    ) -> ThreadsPromptPackage:
        """生成に使う prompt を決定的に組み立てる (外部呼び出しなし)。

        同じ記事・同じ ``learning_as_of`` からは同じ prompt になる。
        """

        article = self._require_article(article_id)
        guidance = self.learning_guidance(as_of=learning_as_of)
        return build_prompt(
            source_article_id=article.id,
            source_article_title=article.title or "",
            source_article_body=article.body or "",
            source_article_body_hash=compute_text_hash(article.body or ""),
            angles=angles or self._policy.angles,
            policy=self._policy,
            guidance=guidance,
            requested_link_mode=requested_link_mode,
        )

    # -- proposal -------------------------------------------------------------
    def plan(
        self,
        *,
        article_id: int,
        generated_output: str,
        learning_as_of: datetime | None = None,
        expected_guidance: str | None = None,
    ) -> PreparedProposals:
        """生成結果を検査して、保存したら何ができるかを示す (保存はしない)。

        ``expected_guidance`` (prompt を作ったときの参考の指紋) を渡すと、同じ参考で
        あることを確かめる。違えば拒否する (別の参考で書かれた案として記録しない)。
        学習の参考は検査 (errors) に一切使わない。
        """

        article = self._require_article(article_id)
        guidance = self.learning_guidance(as_of=learning_as_of)
        if expected_guidance is not None and expected_guidance != guidance.fingerprint:
            raise ThreadsProposalError(
                "the learning guidance differs from the prompt's "
                f"(expected {expected_guidance[:16]}, now {guidance.fingerprint[:16]} as of "
                f"{guidance.as_of}); pass the prompt's --learning-as-of or rebuild the prompt"
            )
        body_hash = compute_text_hash(article.body or "")
        prepared = PreparedProposals(
            source_article_id=article.id,
            source_article_title=article.title or "",
            source_article_body_hash=body_hash,
            policy_version=self._policy.policy_version,
            generator_version=GENERATOR_VERSION,
            learning={
                **guidance.as_dict(),
                "verified_against_prompt": expected_guidance is not None,
            },
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
        prepared.learning["diversity_notes"] = _diversity_notes(prepared.candidates, guidance)
        return prepared

    def persist(
        self,
        *,
        article_id: int,
        generated_output: str,
        now: datetime | None = None,
        learning_as_of: datetime | None = None,
        expected_guidance: str | None = None,
    ) -> list[ThreadsPostProposal]:
        """検査を通った案だけを ``awaiting_approval`` として保存する。

        **承認はしない。公開もしない。** 使った学習の参考の小さな来歴を残す。
        """

        now = now or datetime.now(UTC)
        self._require_learning_column()
        prepared = self.plan(
            article_id=article_id,
            generated_output=generated_output,
            learning_as_of=learning_as_of or now,
            expected_guidance=expected_guidance,
        )
        provenance = self.learning_guidance(as_of=learning_as_of or now).provenance(
            verified_against_prompt=expected_guidance is not None
        )
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
                learning_guidance_json=provenance,
                created_at=to_storage_utc(now),
            )
            self._session.add(row)
            rows.append(row)
        self._session.commit()
        for row in rows:
            self._session.refresh(row)
        return rows

    # -- audit (T6) ---------------------------------------------------------------
    def audit_guidance(self, proposal_id: int) -> dict:
        """保存した提案の学習の来歴を、同じ ``as_of`` で作り直して指紋を比べる。

        **読むだけ。** 何も書かない (flush しようとしたら止める)。
        結果: ``match`` / ``mismatch`` / ``no_provenance`` / ``not_migrated``。
        """

        from app.services.threads_learning_service import read_only_session

        with read_only_session(self._session):
            row = self._require(proposal_id)
            base = {
                "proposal_id": row.id,
                "status": row.status,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            if not self.can_store_learning_provenance():
                return {
                    **base,
                    "result": "not_migrated",
                    "reason": "this database has no learning_guidance_json column",
                }
            provenance = row.learning_guidance_json
            if not provenance:
                return {
                    **base,
                    "result": "no_provenance",
                    "reason": "the proposal was saved before T5.5 or without guidance",
                }
            as_of = datetime.fromisoformat(provenance["as_of"])
            rebuilt = self.learning_guidance(as_of=as_of)
        match = rebuilt.fingerprint == provenance.get("fingerprint")
        return {
            **base,
            "result": "match" if match else "mismatch",
            "as_of": provenance["as_of"],
            "saved": {
                "fingerprint": provenance.get("fingerprint"),
                "mode": provenance.get("mode"),
                "evidence_status": provenance.get("evidence_status"),
                "learning_policy_version": provenance.get("learning_policy_version"),
                "verified_against_prompt": provenance.get("verified_against_prompt"),
            },
            "rebuilt": {
                "fingerprint": rebuilt.fingerprint,
                "mode": rebuilt.mode,
                "evidence_status": rebuilt.evidence_status,
                "learning_policy_version": rebuilt.source_policy_version,
            },
        }

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
    def can_store_learning_provenance(self) -> bool:
        """来歴の列 (migration ``afc2f36bb3ca``) がこの DB にあるか (読むだけ)。"""

        columns = {
            c["name"]
            for c in inspect(self._session.connection()).get_columns("threads_post_proposals")
        }
        return "learning_guidance_json" in columns

    def _require_learning_column(self) -> None:
        """来歴の列が無い (migration 前の) DB には保存しない。何も書かずに止める。"""

        if not self.can_store_learning_provenance():
            raise ThreadsProposalError(
                "the database has no learning_guidance_json column yet; run "
                "`uv run alembic upgrade head` first (nothing was stored)"
            )

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


def _length_bands(measurement) -> dict[str, tuple[int, int]]:
    bands, lower = {}, 1
    for bucket in measurement.length_buckets:
        upper = int(bucket["max_characters"])
        bands[str(bucket["name"])] = (lower, upper)
        lower = upper + 1
    return bands


def _diversity_notes(candidates: list[dict], guidance: ThreadsGenerationGuidance) -> list[str]:
    """参考があるとき、案が 1 つの値にそろってしまっていないかを **知らせるだけ**。

    検査ではない (拒否しない。警告として保存もしない)。
    """

    if guidance.mode != MODE_WEAK or len(candidates) < 2:
        return []
    notes = []
    for preference in guidance.prompt_preferences:
        if not preference.actionable:
            continue
        key = "length_band" if preference.dimension == "length_band" else preference.dimension
        values = {_candidate_value(c, key, guidance) for c in candidates}
        if values == {preference.value}:
            notes.append(
                f"every candidate uses {key}={preference.value}; keep at least one other "
                "value in the batch (the learning preference is weak)"
            )
    return notes


def _candidate_value(candidate: dict, key: str, guidance: ThreadsGenerationGuidance) -> str:
    if key != "length_band":
        return str(candidate.get(key))
    count = int(candidate.get("character_count") or 0)
    for name, (low, high) in guidance.length_bands.items():
        if low <= count <= high:
            return name
    return "unknown"


def _preview(proposal: ThreadsProposal, *, width: int = 120) -> str:
    text = proposal.publish_text.replace("\n", " / ")
    return text if len(text) <= width else text[: width - 3] + "..."


__all__ = [
    "PreparedProposals",
    "ThreadsProposalError",
    "ThreadsProposalService",
    "TP_PROPOSED",
]
