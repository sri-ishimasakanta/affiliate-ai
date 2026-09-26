"""公開した投稿を「会話のきっかけ」ごとに並べる (T6.3。**読むだけ・観測だけ**)。

公開 (``threads_publications``) → 提案 (``threads_post_proposals``) の来歴の
``generation_brief.conversation_hook`` → 最新の指標 (``threads_insight_snapshots``) をつなぎ、
返信率・引用率・共有率を出す。T6.3 より前の提案は ``legacy``。

**生成のやり方を自動で変えない。** 少ない数の比較から勝ち負けを決めない (T5 の学習の規則に
任せる)。指標の取り込みは変えない (views・likes・replies・reposts・quotes・shares のまま)。
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ThreadsInsightSnapshot, ThreadsPostProposal, ThreadsPublication
from app.social.threads.conversation import engagement_rates, hook_from_provenance


def publication_rows(session: Session) -> list[dict]:
    """公開ごとの 1 行 (公開 ID・提案 ID・きっかけ・切り口・リンク・最新の指標・率)。"""

    latest: dict[int, ThreadsInsightSnapshot] = {}
    for snap in session.scalars(
        select(ThreadsInsightSnapshot)
        .where(ThreadsInsightSnapshot.outcome == "observed")
        .order_by(ThreadsInsightSnapshot.observed_at, ThreadsInsightSnapshot.id)
    ):
        latest[snap.threads_publication_id] = snap
    rows = []
    for pub in session.scalars(select(ThreadsPublication).order_by(ThreadsPublication.id)):
        proposal = session.get(ThreadsPostProposal, pub.proposal_id) if pub.proposal_id else None
        snap = latest.get(pub.id)
        metrics = {
            name: getattr(snap, name, None) if snap else None
            for name in ("views", "likes", "replies", "reposts", "quotes", "shares")
        }
        rows.append(
            {
                "publication_id": pub.id,
                "proposal_id": pub.proposal_id,
                "status": pub.status,
                "conversation_hook": hook_from_provenance(
                    getattr(proposal, "learning_guidance_json", None)
                ),
                "angle": getattr(proposal, "angle", None) or pub.angle,
                "link_mode": getattr(proposal, "link_mode", None),
                "observed_at": snap.observed_at.isoformat() if snap and snap.observed_at else None,
                **metrics,
                **engagement_rates(
                    views=metrics["views"],
                    replies=metrics["replies"],
                    quotes=metrics["quotes"],
                    shares=metrics["shares"],
                ),
            }
        )
    return rows


def by_hook(rows: list[dict]) -> dict[str, dict]:
    """きっかけごとの件数と合計 (率は合計から出す。サンプルが少ない間は参考にとどめる)。"""

    groups: dict[str, dict] = defaultdict(
        lambda: {"publications": 0, "views": 0, "replies": 0, "quotes": 0, "shares": 0}
    )
    for row in rows:
        group = groups[row["conversation_hook"]]
        group["publications"] += 1
        for name in ("views", "replies", "quotes", "shares"):
            group[name] += row.get(name) or 0
    return {
        hook: {
            **group,
            **engagement_rates(
                views=group["views"],
                replies=group["replies"],
                quotes=group["quotes"],
                shares=group["shares"],
            ),
        }
        for hook, group in sorted(groups.items())
    }
