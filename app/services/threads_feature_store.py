"""自分の投稿の特徴の保存と、記述の基準 (T6.5A、**読むだけ**)。

公開 (``threads_publications``) → 提案 (``threads_post_proposals``) → 最新の観測
(``threads_insight_snapshots``) をつなぎ、公開ごとに 1 行を作る。新しい表は作らない
(特徴は本文から決定的に作り直せる。版は ``features.FEATURE_VERSION``)。

- **生の指標** (``metrics``) と **特徴** (``features``) は分けて持つ。
- トピックは **実際に送った値** (コンテナ作成の記録の ``topic_tag``)。記録の無い古い公開は
  ``None`` (後から方針で埋めない)。
- 基準は中央値と本数だけ。少ない数は ``small_sample``。原因の言い方はしない。
- 生成・公開には何も戻さない。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import (
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
)
from app.models.threads_publication import PUB_PUBLISHED
from app.services.threads_publication_service import STEP_CREATE, parse_remote_timestamp
from app.social.threads.conversation import hook_from_provenance
from app.social.threads.features import FEATURE_VERSION, extract
from app.social.threads.performance import engagement_total
from app.social.threads.topic import (
    CONTENT_KIND_ACCOUNT_GROWTH,
    TopicPolicyError,
    content_kind,
)
from app.social.threads.trends import (
    DESCRIPTIVE_NOTE,
    MIN_GROUP_SAMPLE,
    group_summary,
    observed_extremes,
)

OWN_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
BASELINE_METRICS = (*OWN_METRICS, "engagement")
#: 「中央値が高く / 低く観測された」まとまりを示す指標。
EXTREME_METRICS = ("views", "engagement")
JST = ZoneInfo("Asia/Tokyo")
#: 基準を出す切り口 (features / 提案の来歴の値)。
OWN_DIMENSIONS = (
    "content_kind", "conversation_hook", "topic", "has_url", "char_bucket", "hour",
    "cta_class", "structure_class",
)  # fmt: skip


def _sent_topic(session: Session, publication_id: int) -> tuple[bool, str | None]:
    """コンテナ作成の記録にある、実際に送ったトピック。(記録があるか, 値)。"""

    details = session.scalars(
        select(ThreadsPublicationAttempt.detail_json)
        .where(
            ThreadsPublicationAttempt.threads_publication_id == publication_id,
            ThreadsPublicationAttempt.step == STEP_CREATE,
            ThreadsPublicationAttempt.outcome == "succeeded",
        )
        .order_by(ThreadsPublicationAttempt.id.desc())
    ).all()
    for detail in details:
        if isinstance(detail, dict) and "topic_tag" in detail:
            return True, detail.get("topic_tag")
    return False, None


def _kind(proposal) -> str:
    """読むだけなので、方針に合わない古い行でも止めずに ``unknown`` にする。"""

    if proposal is None:
        return "unknown"
    try:
        return content_kind(proposal)
    except TopicPolicyError:
        return "unknown"


def own_feature_rows(session: Session) -> list[dict]:
    """公開済みの投稿ごとの 1 行 (特徴 + 生の指標 + 来歴)。"""

    latest: dict[int, ThreadsInsightSnapshot] = {}
    for snap in session.scalars(
        select(ThreadsInsightSnapshot)
        .where(ThreadsInsightSnapshot.outcome == "observed")
        .order_by(ThreadsInsightSnapshot.observed_at, ThreadsInsightSnapshot.id)
    ):
        latest[snap.threads_publication_id] = snap
    rows = []
    for pub in session.scalars(
        select(ThreadsPublication)
        .where(ThreadsPublication.status == PUB_PUBLISHED)
        .order_by(ThreadsPublication.id)
    ):
        proposal = session.get(ThreadsPostProposal, pub.proposal_id) if pub.proposal_id else None
        kind = _kind(proposal)
        recorded, topic = _sent_topic(session, pub.id)
        posted_at = parse_remote_timestamp(pub.remote_timestamp) or (
            ensure_aware(pub.published_at) if pub.published_at else None
        )
        features = extract(
            pub.exact_published_text or "", topic=topic, media_type="text", posted_at=posted_at
        ).as_dict()
        snap = latest.get(pub.id)
        metrics = {name: getattr(snap, name, None) if snap else None for name in OWN_METRICS}
        # 生の指標から作った値は別に持つ (指標の一部が欠けていれば None)。
        derived = {"engagement": engagement_total(metrics)}
        hook = (
            CONTENT_KIND_ACCOUNT_GROWTH
            if kind == CONTENT_KIND_ACCOUNT_GROWTH
            else hook_from_provenance(getattr(proposal, "learning_guidance_json", None))
        )
        rows.append(
            {
                "publication_id": pub.id,
                "proposal_id": pub.proposal_id,
                "source_article_id": pub.source_article_id,
                "content_kind": kind,
                "topic_tag": topic,
                "conversation_hook": hook,
                "angle": getattr(proposal, "angle", None) or pub.angle,
                "link_mode": getattr(proposal, "link_mode", None),
                "posted_at": posted_at.isoformat() if posted_at else None,
                "published_at_jst": _jst(posted_at),
                "topic_recorded": recorded,
                "features": features,
                "metrics": metrics,
                "derived": derived,
                "observed_at": _iso(snap.observed_at) if snap else None,
                "observed_at_jst": _jst(snap.observed_at) if snap else None,
            }
        )
    return rows


def _flat(row: dict) -> dict:
    return {
        "content_kind": row["content_kind"],
        "conversation_hook": row["conversation_hook"],
        # トピック: 記録があって送っていない → "none"、記録が無い (古い公開) → "unknown"。
        "topic": row["features"].get("topic") or ("none" if row["topic_recorded"] else None),
        **{k: row["features"].get(k) for k in ("has_url", "char_bucket", "hour", "cta_class",
                                                "structure_class")},
        **row["metrics"],
        **row["derived"],
    }  # fmt: skip


def own_baselines(rows: list[dict], *, min_sample: int = MIN_GROUP_SAMPLE) -> dict:
    flat = [_flat(r) for r in rows]
    observed = sum(1 for r in rows if any(v is not None for v in r["metrics"].values()))
    small = len(rows) < min_sample
    dimensions = {
        dim: group_summary(flat, dim, BASELINE_METRICS, min_sample=min_sample)
        for dim in OWN_DIMENSIONS
    }
    limitations = [DESCRIPTIVE_NOTE]
    if small:
        limitations.append(f"only {len(rows)} published posts; every group is a small sample")
    limitations.append(
        "metrics are the latest observation per post (posts of different ages are not "
        "age-aligned here; scripts/analyze_threads_performance.py compares at equal ages)"
    )
    limitations.append("topic is the value recorded at container creation; older posts: unknown")
    return {
        "feature_version": FEATURE_VERSION,
        "publications": len(rows),
        "publications_with_metrics": observed,
        "small_sample": small,
        "min_group_sample": min_sample,
        "dimensions": dimensions,
        "observed_extremes": {
            dim: {metric: observed_extremes(groups, metric) for metric in EXTREME_METRICS}
            for dim, groups in dimensions.items()
        },
        "limitations": limitations,
    }


def _jst(moment: datetime | None) -> str | None:
    return ensure_aware(moment).astimezone(JST).isoformat() if moment else None


def _iso(moment: datetime | None) -> str | None:
    return ensure_aware(moment).isoformat() if moment else None


__all__ = ["OWN_DIMENSIONS", "OWN_METRICS", "own_baselines", "own_feature_rows"]
