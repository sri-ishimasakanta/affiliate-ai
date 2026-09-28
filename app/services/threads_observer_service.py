"""外の Threads の観察の保存 (T6.5B)。**自分の提案・公開の表とは混ぜない。**

- 1 回の観察 = ``threads_observer_runs`` の 1 行 (止まった実行も、理由つきで残す)。
- 外の投稿 = ``threads_external_posts`` (投稿ごとに 1 行)。最初に見た本文を残す。後で本文が
  変われば、観測の ``body_hash`` で分かる (上書きしない)。
- 観測 = ``threads_external_observations`` (**積むだけ**)。同じ投稿を何度も見れば、見た回数
  だけ行が増える (いいね等の推移)。見えなかった指標は NULL (0 にしない)。表示回数は持たない。
- トピックの一覧 = ``threads_trending_topics`` (名前ごと。``source_type`` は一覧の種類。
  例 ``topic_for_you`` = 検索の最初の画面の「おすすめのトピック」)。
- ログインが要る / 画面の形が違う実行は、実行の行だけを残し、投稿は保存しない。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.models import (
    ThreadsExternalObservation,
    ThreadsExternalPost,
    ThreadsObserverRun,
    ThreadsTrendingTopic,
)
from app.models.threads_observer import SOURCE_TRENDING_TOPIC
from app.social.threads.features import FEATURE_VERSION
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import CollectionResult

EXTRACTION_COMPLETE = "complete"
#: 確かめた指標 (いいね・返信) の一部が読めなかった (読めない指標は NULL)。
EXTRACTION_PARTIAL = "partial_metrics"
_VISIBLE_METRICS = sel.VERIFIED_METRICS


def record_run(session: Session, result: CollectionResult) -> ThreadsObserverRun:
    """1 回の観察を保存する (1 回の commit)。"""

    run = ThreadsObserverRun(
        started_at=to_storage_utc(result.started_at),
        finished_at=to_storage_utc(result.finished_at) if result.finished_at else None,
        status=result.status,
        source_types_json=list(result.source_types),
        item_limit=result.item_limit,
        items_collected=len(result.posts),
        items_rejected=result.rejected,
        failure_reason=result.reason,
        collector_version=sel.COLLECTOR_VERSION,
        selector_version=sel.SELECTOR_VERSION,
        artifacts_json=(
            {"screenshots": dict(result.screenshots), "pages_opened": result.pages_opened,
             "scrolls": result.scrolls,
             # T6.5B.1: 候補ごとの結果と理由 (勘定が合うか)。件数の上限の意味は collector。
             "candidate_accounting": result.accounting_summary()}
        ),  # fmt: skip
    )
    session.add(run)
    session.flush()
    observed_at = to_storage_utc(result.finished_at or result.started_at)
    for item in result.posts:
        post = _upsert_post(session, item.record, observed_at)
        record = item.record
        complete = all(getattr(record, name) is not None for name in _VISIBLE_METRICS)
        session.add(
            ThreadsExternalObservation(
                post_id=post.id,
                run_id=run.id,
                observed_at=observed_at,
                source_type=item.source_type,
                source_query=item.source_query,
                likes=record.likes,
                replies=record.replies,
                reposts=record.reposts,
                quotes=record.quotes,
                shares=record.shares,
                body_hash=record.body_hash,
                extraction_status=EXTRACTION_COMPLETE if complete else EXTRACTION_PARTIAL,
                collector_version=sel.COLLECTOR_VERSION,
                selector_version=sel.SELECTOR_VERSION,
            )
        )
    # トピックの一覧の種類 (例 ``topic_for_you``) を source_type にする。一覧の順・リンク・
    # 語は実行の ``artifacts_json.candidate_accounting.topic_list`` に残る。
    kinds = {e["name"]: e["kind"] for e in (result.topic_accounting or {}).get("sequence", [])}
    for name, sample in result.trending_topics:
        _upsert_topic(session, name, sample, observed_at, run.id,
                      source_type=kinds.get(name, SOURCE_TRENDING_TOPIC))  # fmt: skip
    session.commit()
    return run


def _upsert_post(session: Session, record, observed_at: datetime) -> ThreadsExternalPost:
    post = session.scalars(
        select(ThreadsExternalPost).where(
            ThreadsExternalPost.external_post_key == record.external_post_key
        )
    ).first()
    if post is None:
        post = ThreadsExternalPost(
            external_post_key=record.external_post_key,
            author_handle=record.author_handle,
            permalink=record.permalink,
            post_timestamp=(
                to_storage_utc(record.post_timestamp) if record.post_timestamp else None
            ),
            body_text=record.body_text,
            body_hash=record.body_hash,
            topic=record.topic,
            media_type=record.media_type,
            has_link=record.has_link,
            features_json=record.features,
            feature_version=FEATURE_VERSION,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
        session.add(post)
        session.flush()
        return post
    # 最初に見た本文・特徴は残す。欠けていた値だけを埋める (読み替えない)。
    post.last_seen_at = observed_at
    if post.post_timestamp is None and record.post_timestamp is not None:
        post.post_timestamp = to_storage_utc(record.post_timestamp)
    if post.topic is None and record.topic is not None:
        post.topic = record.topic
    return post


def _upsert_topic(session: Session, name: str, sample: int, observed_at: datetime,
                  run_id: int, *, source_type: str = SOURCE_TRENDING_TOPIC) -> None:  # fmt: skip
    row = session.scalars(
        select(ThreadsTrendingTopic).where(
            ThreadsTrendingTopic.topic_name == name,
            ThreadsTrendingTopic.source_type == source_type,
        )
    ).first()
    if row is None:
        row = ThreadsTrendingTopic(
            topic_name=name, source_type=source_type, first_seen_at=observed_at,
            last_seen_at=observed_at, observations=0, sample_post_count=0,
        )  # fmt: skip
        session.add(row)
    row.last_seen_at = observed_at
    row.observations = (row.observations or 0) + 1
    row.sample_post_count = (row.sample_post_count or 0) + int(sample)
    row.last_run_id = run_id


__all__ = ["EXTRACTION_COMPLETE", "EXTRACTION_PARTIAL", "record_run"]
