"""Growth Action から既存の流れへ渡した手元の依頼 (C9-B)。**外には書かない。**

- 作る: ``GrowthActionConversionService`` (承認・固定した指紋・最新の版・いまの評価・止める理由・
  重なりを確かめてから) だけが作る。同じ変換の 2 回目は同じ依頼を返す (``idempotency_key`` が一意)。
- Threads の生成の依頼 (``threads_generation``): 既存の在庫の保守が、生成が要るときに使う
  (``pending_targeted``)。使うのは方針のスイッチ (``growth_action_policy.json`` の
  ``threads_generation_requests.consume_in_stock_maintenance``、既定 false) が true のとき。
  使ったら ``claimed`` (生成の依頼の ID)、提案ができたら ``proposal_created`` (提案の ID)。
  OpenAI を呼ぶのは既存の在庫の保守そのもので、呼び出しの数は増えない (承認・公開も既存のまま)。
- 記事の計画の依頼 (``article_planning``): 人が承認・却下し、既存の流れで作った記事と結ぶ
  (``materialize``)。この依頼が記事を作ることはない。
- 変更の準備の依頼 (``change_preparation``): 具体的な変更の中身は無い。人が既存の流れで具体的な
  変更 (変更の依頼・編集の版・リンクの置き換え) を作ったら結ぶ (``prepare``)。結ぶのは、固定した
  元の hash の上に作ったものだけ (元が変わっていたら断る)。先の承認・適用は独自のまま。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.fact_freshness import ensure_aware
from app.models import (
    Article,
    ArticleEditorialRevision,
    ArticleLinkSubstitutionMapping,
    ChangeApplication,
    ChangeRequest,
    ThreadsPostProposal,
)
from app.models.growth_handoff import (
    GH_APPROVED,
    GH_ARTICLE_PLANNING,
    GH_CANCELLED,
    GH_CHANGE_AFFILIATE_PLACEMENT,
    GH_CHANGE_BODY_UPDATE,
    GH_CHANGE_META_DESCRIPTION,
    GH_CHANGE_PREPARATION,
    GH_CLAIMED,
    GH_MATERIALIZED,
    GH_OPEN_STATUSES,
    GH_PENDING,
    GH_PREPARED,
    GH_PROPOSAL_CREATED,
    GH_REJECTED,
    GH_THREADS_GENERATION,
    GrowthHandoffRequest,
    growth_handoff_transition_allowed,
)

HANDOFF_REVISION = "4fe83827d695"

#: 変更の準備と結んでよい、先の実体の種類 (変更の種類ごと)。
DS_CHANGE_REQUEST = "change_request"
DS_EDITORIAL_REVISION = "editorial_revision"
DS_LINK_MAPPING = "link_mapping"
PREPARATION_DOWNSTREAM: dict[str, tuple[str, ...]] = {
    GH_CHANGE_BODY_UPDATE: (DS_CHANGE_REQUEST, DS_EDITORIAL_REVISION),
    GH_CHANGE_META_DESCRIPTION: (DS_EDITORIAL_REVISION,),
    GH_CHANGE_AFFILIATE_PLACEMENT: (DS_LINK_MAPPING, DS_CHANGE_REQUEST),
}
#: 記事の計画と結んでよい記事の状態 (人が計画を承認した後の状態。archived は結ばない)。
MATERIALIZED_ARTICLE_STATES = ("planned", "drafting", "review", "approved", "published")


class GrowthHandoffError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _sha(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)  # fmt: skip
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _iso(value) -> str | None:
    return ensure_aware(value).isoformat() if value is not None else None


def handoff_ready(session: Session) -> bool:
    return "growth_handoff_requests" in set(inspect(session.connection()).get_table_names())


def source_hashes(article: Article) -> dict:
    """記事の今の本文とメタディスクリプションの hash (編集の版・変更の依頼と同じ求め方)。"""

    return {"body_hash": compute_text_hash(article.body or ""),
            "meta_hash": compute_text_hash(article.meta_description or "")}


def consume_targeted_enabled(policy=None) -> bool:
    """在庫の保守が Threads の生成の依頼を使ってよいか (方針のスイッチ。既定 false)。"""

    from app.growth.policy import load_policy

    raw = (policy or load_policy()).raw.get("threads_generation_requests") or {}
    return raw.get("consume_in_stock_maintenance") is True


class GrowthHandoffService:
    def __init__(self, session: Session) -> None:
        self._session = session

    # -- 作る (変換だけが呼ぶ) ------------------------------------------------------------------
    def existing(self, idempotency_key: str) -> GrowthHandoffRequest | None:
        return self._session.scalars(select(GrowthHandoffRequest).where(
            GrowthHandoffRequest.idempotency_key == idempotency_key)).first()  # fmt: skip

    def open_requests(self, workflow: str, *, article_id: int | None = None,
                      keyword_id: int | None = None,
                      change_type: str | None = None) -> list[GrowthHandoffRequest]:  # fmt: skip
        if not handoff_ready(self._session):
            return []
        query = select(GrowthHandoffRequest).where(
            GrowthHandoffRequest.workflow == workflow,
            GrowthHandoffRequest.status.in_(tuple(GH_OPEN_STATUSES)))
        if article_id is not None:
            query = query.where(GrowthHandoffRequest.article_id == article_id)
        if keyword_id is not None:
            query = query.where(GrowthHandoffRequest.keyword_id == keyword_id)
        if change_type is not None:
            query = query.where(GrowthHandoffRequest.change_type == change_type)
        return list(self._session.scalars(query.order_by(GrowthHandoffRequest.id)))

    def create(self, *, workflow: str, idempotency_key: str, growth_action_id: int,
               review_id: int, candidate_fingerprint: str, frozen: dict, now: datetime,
               article_id: int | None = None, keyword_id: int | None = None,
               lane: str | None = None, change_type: str | None = None,
               requested_angle: str | None = None
               ) -> tuple[GrowthHandoffRequest, bool]:  # fmt: skip
        """依頼を 1 つ作る (既にあればそれを返す: 返り値の 2 つ目が False)。**commit しない。**"""

        found = self.existing(idempotency_key)
        if found is not None:
            return found, False
        row = GrowthHandoffRequest(
            workflow=workflow, status=GH_PENDING, lane=lane, change_type=change_type,
            source_growth_action_id=growth_action_id, source_review_id=review_id,
            source_candidate_fingerprint=candidate_fingerprint,
            idempotency_key=idempotency_key, article_id=article_id, keyword_id=keyword_id,
            requested_angle=requested_angle, frozen_json=frozen, frozen_hash=_sha(frozen),
            created_at=now, updated_at=now)  # fmt: skip
        self._session.add(row)
        self._session.flush()
        return row, True

    # -- 状態 ----------------------------------------------------------------------------------
    def get(self, request_id: int) -> GrowthHandoffRequest:
        row = self._session.get(GrowthHandoffRequest, request_id)
        if row is None:
            raise GrowthHandoffError(f"handoff request {request_id} does not exist")
        return row

    def transition(self, row: GrowthHandoffRequest, target: str, *, now: datetime,
                   decided_by: str | None = None, note: str | None = None) -> None:  # fmt: skip
        if not growth_handoff_transition_allowed(row.workflow, row.status, target):
            raise GrowthHandoffError(
                f"handoff request {row.id} ({row.workflow}) cannot go {row.status} → {target}")
        row.status, row.updated_at = target, now
        if decided_by:
            row.decided_by = decided_by
        if note:
            row.resolution_note = note
        if target not in GH_OPEN_STATUSES:
            row.resolved_at = now

    # -- Threads: 在庫の保守の側 -----------------------------------------------------------------
    def pending_targeted(self) -> list[GrowthHandoffRequest]:
        return [r for r in self.open_requests(GH_THREADS_GENERATION) if r.status == GH_PENDING]

    def claim(self, request_id: int, *, generation_request_id: str, now: datetime) -> None:
        row = self.get(request_id)
        if row.status == GH_CLAIMED and row.generation_request_id == generation_request_id:
            return
        self.transition(row, GH_CLAIMED, now=now)
        row.generation_request_id = generation_request_id

    def proposal_created(self, generation_request_id: str, proposal_ids: list[int], *,
                         now: datetime) -> GrowthHandoffRequest | None:  # fmt: skip
        if not handoff_ready(self._session) or not proposal_ids:
            return None
        row = self._session.scalars(select(GrowthHandoffRequest).where(
            GrowthHandoffRequest.generation_request_id == generation_request_id
        )).first()  # fmt: skip
        if row is None or row.status == GH_PROPOSAL_CREATED:
            return row
        self.transition(row, GH_PROPOSAL_CREATED, now=now)
        row.downstream_type, row.downstream_ids_json = "threads_proposal", sorted(proposal_ids)
        return row

    def release(self, generation_request_id: str, *, now: datetime, note: str) -> None:
        """生成の依頼が答えなしで終わった: 依頼を pending に戻す (次の生成で使える)。"""

        if not handoff_ready(self._session):
            return
        row = self._session.scalars(select(GrowthHandoffRequest).where(
            GrowthHandoffRequest.generation_request_id == generation_request_id
        )).first()  # fmt: skip
        if row is not None and row.status == GH_CLAIMED:
            self.transition(row, GH_PENDING, now=now, note=note)
            row.generation_request_id = None

    # -- 人の判断 (記事の計画・変更の準備) ---------------------------------------------------------
    def decide_planning(self, request_id: int, *, approve: bool, now: datetime,
                        decided_by: str = "human", reason: str | None = None) -> None:  # fmt: skip
        row = self.get(request_id)
        if row.workflow != GH_ARTICLE_PLANNING:
            raise GrowthHandoffError(f"handoff request {row.id} is not an article planning request")
        if not approve and not (reason or "").strip():
            raise GrowthHandoffError("a rejection needs a reason")
        self.transition(row, GH_APPROVED if approve else GH_REJECTED, now=now,
                        decided_by=decided_by, note=reason)

    def materialize(self, request_id: int, *, article_id: int, now: datetime) -> None:
        """人が既存の流れで作った記事と結ぶ (この依頼は記事を作らない)。

        結ぶのは、同じキーワードの・計画の承認より後の状態の (archived でない)・依頼より後に作った・
        ほかの依頼と結んでいない記事だけ。
        """

        row = self.get(request_id)
        if row.workflow != GH_ARTICLE_PLANNING or row.status != GH_APPROVED:
            raise GrowthHandoffError(f"handoff request {row.id} must be an approved article "
                                     "planning request")
        article = self._session.get(Article, article_id)
        if article is None:
            raise GrowthHandoffError(f"article {article_id} does not exist")
        if row.keyword_id is None or article.keyword_id != row.keyword_id:
            raise GrowthHandoffError(f"article {article_id} is not linked to keyword "
                                     f"{row.keyword_id}")
        if str(article.status) not in MATERIALIZED_ARTICLE_STATES:
            raise GrowthHandoffError(f"article {article_id} is {article.status}; link an article "
                                     "whose plan was approved in the existing flow")
        if _older(article.created_at, row.created_at):
            raise GrowthHandoffError(f"article {article_id} existed before the planning request")
        self._refuse_claimed("article", article_id, row.id)
        self.transition(row, GH_MATERIALIZED, now=now)
        row.downstream_type, row.downstream_ids_json = "article", [article_id]

    def prepare(self, request_id: int, *, downstream_type: str, downstream_id: int,
                now: datetime) -> None:  # fmt: skip
        """人が既存の流れで作った具体的な変更と結ぶ (承認・適用は先の流れ)。

        結ぶのは、同じ記事の・依頼より後に作った・固定した元の hash の上に作った・終わっていない
        (却下・古いではない)・ほかの依頼と結んでいないものだけ。元が変わっていたら断る。
        """

        row = self.get(request_id)
        if row.workflow != GH_CHANGE_PREPARATION or row.status != GH_PENDING:
            raise GrowthHandoffError(f"handoff request {row.id} must be a pending change "
                                     "preparation request")
        allowed = PREPARATION_DOWNSTREAM.get(row.change_type or "", ())
        if downstream_type not in allowed:
            raise GrowthHandoffError(f"a {row.change_type} preparation links to "
                                     f"{' / '.join(allowed)}, not {downstream_type}")
        source = (row.frozen_json or {}).get("source") or {}
        entity, based_on, created = self._downstream_entity(downstream_type, downstream_id)
        if entity is None or entity.article_id != row.article_id:
            raise GrowthHandoffError(f"{downstream_type} {downstream_id} is not for article "
                                     f"{row.article_id}")
        if _older(created, row.created_at):
            raise GrowthHandoffError(f"{downstream_type} {downstream_id} existed before the "
                                     "preparation request")
        for field, value in based_on.items():
            if source.get(field) != value:
                raise GrowthHandoffError(f"{downstream_type} {downstream_id} was not made from the "
                                         f"frozen source ({field} differs): the article changed "
                                         "after the conversion; close this request and re-review")
        state = str(getattr(entity, "status", "") or "")
        if state in ("rejected", "stale", "revoked", "superseded"):
            raise GrowthHandoffError(f"{downstream_type} {downstream_id} is {state}")
        self._refuse_claimed(downstream_type, downstream_id, row.id)
        self.transition(row, GH_PREPARED, now=now)
        row.downstream_type, row.downstream_ids_json = downstream_type, [downstream_id]

    def _downstream_entity(self, kind: str, entity_id: int):
        """(実体, 元にした hash, 作った時刻)。"""

        if kind == DS_CHANGE_REQUEST:
            cr = self._session.get(ChangeRequest, entity_id)
            if cr is None:
                return None, {}, None
            return cr, {"body_hash": cr.expected_source_body_hash}, cr.created_at
        if kind == DS_EDITORIAL_REVISION:
            rev = self._session.get(ArticleEditorialRevision, entity_id)
            if rev is None:
                return None, {}, None
            based = {"body_hash": rev.previous_body_hash, "meta_hash": rev.previous_meta_hash}
            return rev, based, rev.created_at
        if kind == DS_LINK_MAPPING:
            mapping = self._session.get(ArticleLinkSubstitutionMapping, entity_id)
            if mapping is None:
                return None, {}, None
            # リンクの置き換えは今の本文の出現に結ぶ: 今の本文が固定した元のままであること。
            article = self._session.get(Article, mapping.article_id)
            based = {"body_hash": source_hashes(article)["body_hash"]} if article else {}
            return mapping, based, mapping.created_at
        return None, {}, None

    def _refuse_claimed(self, kind: str, entity_id: int, own_id: int) -> None:
        for other in self.list():
            if (other.id != own_id and other.downstream_type == kind
                    and entity_id in (other.downstream_ids_json or [])):
                raise GrowthHandoffError(f"{kind} {entity_id} is already linked to handoff "
                                         f"request {other.id}")

    def close(self, request_id: int, *, now: datetime, reason: str,
              rejected: bool = False) -> None:  # fmt: skip
        if not (reason or "").strip():
            raise GrowthHandoffError("closing a request needs a reason")
        row = self.get(request_id)
        self.transition(row, GH_REJECTED if rejected else GH_CANCELLED, now=now, note=reason)

    # -- 観測 (読むだけ) -------------------------------------------------------------------------
    def observe(self, row: GrowthHandoffRequest) -> dict:
        """依頼の状態と、その先の実体の状態 (別々に)。"""

        out = {"id": row.id, "workflow": row.workflow, "lane": row.lane,
               "change_type": row.change_type, "status": row.status,
               "article_id": row.article_id, "keyword_id": row.keyword_id,
               "requested_angle": row.requested_angle,
               "generation_request_id": row.generation_request_id,
               "created_at": _iso(row.created_at), "resolved_at": _iso(row.resolved_at),
               "downstream": [], "external_effect": None}  # fmt: skip
        source = (row.frozen_json or {}).get("source") or {}
        if row.workflow == GH_CHANGE_PREPARATION and row.status == GH_PENDING and source:
            article = self._session.get(Article, row.article_id) if row.article_id else None
            now_hashes = source_hashes(article) if article else {}
            out["source_drift"] = sorted(k for k in ("body_hash", "meta_hash")
                                         if k in source and now_hashes.get(k) != source[k])
        for downstream_id in row.downstream_ids_json or []:
            if row.downstream_type == "threads_proposal":
                proposal = self._session.get(ThreadsPostProposal, downstream_id)
                from app.models import PUB_PUBLISHED, ThreadsPublication

                published = self._session.scalars(select(ThreadsPublication).where(
                    ThreadsPublication.proposal_id == downstream_id,
                    ThreadsPublication.status == PUB_PUBLISHED)).first()  # fmt: skip
                out["downstream"].append({
                    "type": "threads_proposal", "id": downstream_id,
                    "state": proposal.status if proposal else "missing",
                    "published_at": _iso(published.published_at) if published else None})
                if published is not None:
                    out["external_effect"] = _iso(published.published_at)
            elif row.downstream_type == "article":
                article = self._session.get(Article, downstream_id)
                out["downstream"].append({
                    "type": "article", "id": downstream_id,
                    "state": article.status if article else "missing",
                    "published_at": _iso(article.published_at) if article else None})
                if article is not None and article.status == "published":
                    out["external_effect"] = _iso(article.published_at)
            elif row.downstream_type == DS_EDITORIAL_REVISION:
                rev = self._session.get(ArticleEditorialRevision, downstream_id)
                out["downstream"].append({"type": DS_EDITORIAL_REVISION, "id": downstream_id,
                                          "state": "recorded" if rev else "missing",
                                          "revised_at": _iso(rev.revised_at) if rev else None})
            elif row.downstream_type == DS_LINK_MAPPING:
                mapping = self._session.get(ArticleLinkSubstitutionMapping, downstream_id)
                out["downstream"].append({
                    "type": DS_LINK_MAPPING, "id": downstream_id,
                    "state": mapping.status if mapping else "missing",
                    "approved_at": _iso(mapping.approved_at) if mapping else None})
            elif row.downstream_type == "change_request":
                request = self._session.get(ChangeRequest, downstream_id)
                applied = self._session.scalars(select(ChangeApplication).where(
                    ChangeApplication.change_request_id == downstream_id,
                    ChangeApplication.outcome.in_(("succeeded", "reconciled"))
                )).first()  # fmt: skip
                out["downstream"].append({
                    "type": "change_request", "id": downstream_id,
                    "state": request.status if request else "missing",
                    "applied_at": _iso(applied.finished_at) if applied else None})
                if applied is not None:
                    out["external_effect"] = _iso(applied.finished_at)
        return out

    def list(self, *, workflow: str | None = None, status: str | None = None
             ) -> list[GrowthHandoffRequest]:  # fmt: skip
        if not handoff_ready(self._session):
            return []
        query = select(GrowthHandoffRequest)
        if workflow:
            query = query.where(GrowthHandoffRequest.workflow == workflow)
        if status:
            query = query.where(GrowthHandoffRequest.status == status)
        return list(self._session.scalars(query.order_by(GrowthHandoffRequest.id)))


def _older(value, reference) -> bool:
    """``value`` が ``reference`` より前か (秒の丸めの差は同じと見る)。"""

    if value is None or reference is None:
        return False
    return (ensure_aware(reference) - ensure_aware(value)).total_seconds() > 1


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = ["DS_CHANGE_REQUEST", "DS_EDITORIAL_REVISION", "DS_LINK_MAPPING", "GH_CANCELLED",
           "HANDOFF_REVISION", "PREPARATION_DOWNSTREAM", "GrowthHandoffError",
           "GrowthHandoffService", "consume_targeted_enabled", "handoff_ready", "source_hashes"]
