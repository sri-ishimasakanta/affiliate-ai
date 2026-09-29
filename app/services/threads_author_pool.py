"""観察した外の投稿者の一覧 (T6.5B.2、**読むだけ**。新しい表は作らない)。

既存の ``threads_external_posts`` / ``threads_external_observations`` から作り直す。
投稿者ごと: 公開の名前・最初に見た時刻と出どころ・出どころの一覧・投稿の数・観測の数・
最後に見た時刻・知っているアカウントの画面で読んだことがあるか・基準の標本の数・
基準を作るための読み取りがまだ要るか。**非公開の情報・フォロワー数は持たない。**

基準の決まり (``trends.MIN_AUTHOR_SAMPLE``): ある投稿の「伸び」を比べるには、同じ投稿者の
**ほかの** 投稿が 5 件以上 (いいねが見えているもの) 要る。持っている投稿のどれにも基準が
付くのは 6 件から (``bootstrap_needed`` はそれまで True)。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import ThreadsExternalObservation, ThreadsExternalPost
from app.models.threads_observer import SOURCE_KNOWN_ACCOUNT
from app.social.threads.observer import selectors as sel
from app.social.threads.trends import MIN_AUTHOR_SAMPLE

#: 持っている投稿のどれにも基準が付く本数 (ほかの投稿 5 件 + その投稿)。
BOOTSTRAP_POSTS = MIN_AUTHOR_SAMPLE + 1


def author_pool(session: Session) -> list[dict]:
    """投稿者ごとの 1 行 (最初に見た順)。"""

    from app.services.threads_trend_analysis_service import observer_tables_present

    if not observer_tables_present(session):
        return []
    posts = {p.id: p for p in session.scalars(select(ThreadsExternalPost))}
    by_post: dict[int, list[ThreadsExternalObservation]] = defaultdict(list)
    for obs in session.scalars(select(ThreadsExternalObservation).order_by(
            ThreadsExternalObservation.observed_at, ThreadsExternalObservation.id)):  # fmt: skip
        by_post[obs.post_id].append(obs)
    authors: dict[str, dict] = {}
    for post_id, history in by_post.items():
        post = posts.get(post_id)
        if post is None or not post.author_handle:
            continue
        row = authors.setdefault(post.author_handle, {
            "author_handle": post.author_handle, "first_seen_at": None, "first_source_type": None,
            "first_source_query": None, "sources": set(), "posts": 0, "observations": 0,
            "baseline_sample": 0, "latest_observed_at": None, "known_account_observed": False,
        })  # fmt: skip
        row["posts"] += 1
        row["observations"] += len(history)
        first, last = history[0], history[-1]
        if row["first_seen_at"] is None or _aware(first.observed_at) < row["first_seen_at"]:
            row["first_seen_at"] = _aware(first.observed_at)
            row["first_source_type"] = first.source_type
            row["first_source_query"] = first.source_query
        latest = _aware(last.observed_at)
        if row["latest_observed_at"] is None or latest > row["latest_observed_at"]:
            row["latest_observed_at"] = latest
        row["sources"].update(o.source_type for o in history)
        row["known_account_observed"] |= any(o.source_type == SOURCE_KNOWN_ACCOUNT for o in history)
        if last.likes is not None:
            row["baseline_sample"] += 1
    verified = sel.surface_verification(SOURCE_KNOWN_ACCOUNT)["verified"]
    out = []
    for row in sorted(authors.values(), key=lambda r: (r["first_seen_at"], r["author_handle"])):
        out.append({
            **row,
            "sources": sorted(row["sources"]),
            "first_seen_at": row["first_seen_at"].isoformat(),
            "latest_observed_at": row["latest_observed_at"].isoformat(),
            "known_account_surface_verified": verified,
            "baseline_ready_for_new_posts": row["baseline_sample"] >= MIN_AUTHOR_SAMPLE,
            "bootstrap_needed": row["baseline_sample"] < BOOTSTRAP_POSTS,
        })  # fmt: skip
    return out


def _aware(moment: datetime) -> datetime:
    return ensure_aware(moment)


__all__ = ["BOOTSTRAP_POSTS", "author_pool"]
