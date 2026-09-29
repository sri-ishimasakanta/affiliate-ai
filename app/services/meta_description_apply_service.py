"""メタディスクリプション (WordPress の抜粋) の適用 (C10-3 / C10-E)。**既定は PLAN。**

変更の依頼 (``change_type = meta_description``、人が書いたもの) を、**その依頼の承認** のあとに
だけ WordPress へ書く。Growth Action の承認では書かない。

- 検査 (どれか 1 つでも当たれば書かない): 方針 (``change_apply_policy.json`` の
  ``meta_description_apply_enabled``、既定 false) / 依頼が approved / 承認の hash が今の提案と
  同じ / 今の手元のメタが提案のときの値と同じ (手元の drift) / WordPress の今の抜粋が提案の
  ときの値と同じ (WordPress 側の drift) / 投稿の ID / 長さ・HTML の規則 / 既に適用済み (冪等)。
- 書き込み: ``update_post_excerpt_exact`` (``{"excerpt": ...}`` だけ) を **1 回**。リトライしない。
- 確かめ: 書いた後の read-back で抜粋が提案と同じか。違う・応答が分からない → ``outcome_unknown``
  (手元の値は変えない。人が照合する)。成功 → 手元の ``Article.meta_description`` を提案に揃え、
  依頼を applied、``change_applications`` に記録 (前後の抜粋・hash)。
- **本番の最初の実行は人の判断** (新しい WordPress の書き込みの形)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import to_storage_utc
from app.change.text_edit import check_meta_description
from app.models import (
    CHANGE_META_DESCRIPTION,
    CR_APPLIED,
    CR_APPLY_FAILED,
    CR_APPROVED,
    Article,
    ChangeApplication,
    ChangeRequest,
)

PLANNED = "planned"
BLOCKED = "blocked"
SUCCEEDED = "succeeded"
FAILED = "failed"
OUTCOME_UNKNOWN = "outcome_unknown"
_TAG = re.compile(r"<[^>]+>")


def _excerpt_text(post: dict) -> str:
    excerpt = post.get("excerpt")
    raw = excerpt.get("raw") if isinstance(excerpt, dict) else excerpt
    if raw is None and isinstance(excerpt, dict):
        raw = excerpt.get("rendered")
    return " ".join(_TAG.sub("", str(raw or "")).split())


@dataclass
class MetaApplyOutcome:
    request_id: int
    executed: bool
    outcome: str
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    current_meta: str | None = None
    proposed_meta: str | None = None
    application_id: int | None = None
    wordpress_writes: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class MetaDescriptionApplyService:
    def __init__(self, session_factory, *, settings, wordpress_client=None,
                 policy: dict | None = None) -> None:  # fmt: skip
        self._factory = session_factory
        self._settings = settings
        self._client = wordpress_client
        self._policy = policy

    def plan(self, request_id: int) -> MetaApplyOutcome:
        with self._factory() as session:
            return self._plan(session, request_id)

    def _plan(self, session, request_id: int) -> MetaApplyOutcome:
        from app.services.change_apply_policy import meta_description_apply_enabled
        from app.services.change_request_service import ChangeRequestService

        request = session.get(ChangeRequest, request_id)
        if request is None:
            return MetaApplyOutcome(request_id, False, BLOCKED,
                                    [f"change request {request_id} does not exist"])
        proposal = request.proposal_json or {}
        out = MetaApplyOutcome(request_id, False, PLANNED,
                               current_meta=proposal.get("current_meta"),
                               proposed_meta=proposal.get("proposed_meta"))
        if request.change_type != CHANGE_META_DESCRIPTION:
            out.blocked_reasons.append(f"change type is {request.change_type}, not "
                                       "meta_description")
        if not meta_description_apply_enabled(self._policy):
            out.blocked_reasons.append(
                "meta description apply is not enabled (change_apply_policy.json "
                "meta_description_apply_enabled=false; the first production write is a human "
                "decision)")
        if request.status != CR_APPROVED:
            out.blocked_reasons.append(f"request status is {request.status!r}; only "
                                       "'approved' can be applied")
        approval = ChangeRequestService(session).latest_approval(request)
        if approval is None or approval.approved_proposal_hash != request.proposal_hash:
            out.blocked_reasons.append("no approval for the current proposal hash")
        article = session.get(Article, request.article_id)
        if article is None or not article.wordpress_post_id:
            out.blocked_reasons.append("article has no WordPress post id")
        elif compute_text_hash(article.meta_description or "") != proposal.get(
                "current_meta_hash"):
            out.blocked_reasons.append("the local meta description changed after the proposal "
                                       "(drift); re-propose")
        check = check_meta_description(proposal.get("current_meta"),
                                       proposal.get("proposed_meta") or "")
        out.blocked_reasons.extend(check.problems)
        out.warnings.extend(check.warnings)
        if compute_text_hash(proposal.get("proposed_meta") or "") != proposal.get(
                "proposed_meta_hash"):
            out.blocked_reasons.append("stored proposed meta does not match its hash")
        done = session.scalars(select(ChangeApplication).where(
            ChangeApplication.change_request_id == request_id,
            ChangeApplication.outcome == SUCCEEDED)).first()  # fmt: skip
        if done is not None:
            out.blocked_reasons.append(f"already applied (application {done.id})")
            out.application_id = done.id
        if out.blocked_reasons:
            out.outcome = BLOCKED
        return out

    def apply(self, request_id: int, *, execute: bool = False,
              idempotency_key: str | None = None, now: datetime | None = None
              ) -> MetaApplyOutcome:  # fmt: skip
        from app.exceptions import WordPressAmbiguousOutcomeError

        now = now or datetime.now(UTC)
        with self._factory() as session:
            out = self._plan(session, request_id)
            if not execute or out.blocked_reasons:
                return out
            request = session.get(ChangeRequest, request_id)
            article = session.get(Article, request.article_id)
            key = idempotency_key or f"c10-meta-{request.id}-v{request.proposal_version}"
            client = self._client or self._build_client()
            post_id = int(article.wordpress_post_id)
            proposed = out.proposed_meta
            before = _excerpt_text(client.get_post(post_id))
            if before != " ".join((out.current_meta or "").split()):
                out.outcome = BLOCKED
                out.blocked_reasons.append("the live WordPress excerpt differs from the frozen "
                                           "meta description (drift on WordPress); nothing was "
                                           "written")
                return out
            application = ChangeApplication(
                change_request_id=request.id, article_id=article.id,
                proposal_hash=request.proposal_hash, proposal_version=request.proposal_version,
                source_body_hash=request.expected_source_body_hash,
                proposed_body_hash=request.proposed_body_hash,
                pre_change_body=article.body or "", wordpress_post_id=str(post_id),
                outcome=FAILED, idempotency_key=key, attempted_at=to_storage_utc(now),
                result_json={"kind": "meta_description", "pre_excerpt": before})
            session.add(application)
            session.flush()
            try:
                client.update_post_excerpt_exact(post_id, json.dumps({"excerpt": proposed},
                                                                     ensure_ascii=False))
                out.wordpress_writes = 1
                after = _excerpt_text(client.get_post(post_id))
            except WordPressAmbiguousOutcomeError as exc:
                out.wordpress_writes = 1
                application.outcome, application.error_message = OUTCOME_UNKNOWN, str(exc)[:500]
                request.status = CR_APPLY_FAILED
                session.commit()
                out.outcome, out.application_id = OUTCOME_UNKNOWN, application.id
                return out
            except Exception as exc:  # noqa: BLE001 - 失敗は記録して返す (手元の値は変えない)
                application.outcome = FAILED
                application.error_message = f"{type(exc).__name__}: {exc}"[:500]
                request.status = CR_APPLY_FAILED
                session.commit()
                out.outcome, out.application_id = FAILED, application.id
                return out
            application.finished_at = to_storage_utc(datetime.now(UTC))
            application.result_json = {**application.result_json, "post_excerpt": after,
                                       "proposed_meta_hash": compute_text_hash(proposed)}
            if after != " ".join(proposed.split()):
                application.outcome = OUTCOME_UNKNOWN
                application.error_message = "read-back excerpt does not match the proposal"
                request.status = CR_APPLY_FAILED
                out.outcome = OUTCOME_UNKNOWN
            else:
                application.outcome = SUCCEEDED
                article.meta_description = proposed
                request.status = CR_APPLIED
                out.outcome, out.executed = SUCCEEDED, True
            session.commit()
            out.application_id = application.id
            return out

    def _build_client(self):
        from app.wordpress.client import WordPressClient

        return WordPressClient(self._settings)


__all__ = ["MetaApplyOutcome", "MetaDescriptionApplyService"]
