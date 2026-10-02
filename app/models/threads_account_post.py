"""自アカウントの Threads 投稿の台帳 (2026-10-02、manual-post coexistence)。

Threads 上にある **自分のアカウントの投稿** を、由来 (``origin``) つきで 1 投稿 1 行で持つ。

- ``system``: affiliate-ai が承認済みの提案から公開した (``threads_publications`` の記録と
  Threads の投稿 ID が一致する)。公開・承認・本数の数え方の正は今までどおり
  ``threads_publications`` で、この表は数えない。
- ``manual``: Threads 上では見つかったが、affiliate-ai の公開の記録が無い (人がアプリ・
  ブラウザから投稿した)。
- ``unknown``: 安全に決められない (公開の処理中・照合待ちの system 公開がある、など)。

投稿の同一性は Threads の投稿 ID (``threads_media_id``) だけで決める。本文が同じでも ID が
違えば別の投稿。``system`` から ``manual`` へは戻さない。``manual`` / ``unknown`` から
``system`` へは、公開の記録 (同じ投稿 ID) があるときだけ。

変化は ``threads_account_post_events`` に追記だけで残す (更新・削除しない)。一覧に出なく
なっただけでは削除と決めない (``missing_from_listing`` まで)。

この表は架空の提案を作らない。manual 投稿は提案・承認・公開の記録に一切入らない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.models.base import Base

ORIGIN_SYSTEM = "system"
ORIGIN_MANUAL = "manual"
ORIGIN_UNKNOWN = "unknown"
ORIGINS = (ORIGIN_SYSTEM, ORIGIN_MANUAL, ORIGIN_UNKNOWN)

EVIDENCE_PUBLICATION_RECORD = "publication_record"
EVIDENCE_NO_PUBLICATION_RECORD = "no_publication_record"
EVIDENCE_PUBLICATION_IN_FLIGHT = "publication_in_flight"
ORIGIN_EVIDENCE = (EVIDENCE_PUBLICATION_RECORD, EVIDENCE_NO_PUBLICATION_RECORD,
                   EVIDENCE_PUBLICATION_IN_FLIGHT)  # fmt: skip

KIND_NORMAL = "normal"
KIND_GROWTH = "growth"
KIND_UNKNOWN = "unknown"
POST_KINDS = (KIND_NORMAL, KIND_GROWTH, KIND_UNKNOWN)

EV_DISCOVERED = "discovered"
EV_RECONCILED_SYSTEM = "reconciled_system"
EV_ORIGIN_CHANGED = "origin_changed"
EV_CLASSIFICATION_CHANGED = "classification_changed"
EV_TEXT_CHANGED = "text_changed"
EV_MISSING_FROM_LISTING = "missing_from_listing"
EV_REAPPEARED = "reappeared"
EV_METRICS_UNAVAILABLE = "metrics_unavailable"
#: manual 投稿と実質的に同じ提案を ``superseded`` にした (公開の成功ではない)。
EV_PROPOSAL_SUPERSEDED = "proposal_superseded"
ACCOUNT_POST_EVENTS = (EV_DISCOVERED, EV_RECONCILED_SYSTEM, EV_ORIGIN_CHANGED,
                       EV_CLASSIFICATION_CHANGED, EV_TEXT_CHANGED, EV_MISSING_FROM_LISTING,
                       EV_REAPPEARED, EV_METRICS_UNAVAILABLE, EV_PROPOSAL_SUPERSEDED)  # fmt: skip


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


class ThreadsAccountPost(Base):
    __tablename__ = "threads_account_posts"

    __table_args__ = (
        UniqueConstraint("threads_media_id", name="uq_threads_account_posts_media"),
        UniqueConstraint("threads_publication_id", name="uq_threads_account_posts_publication"),
        CheckConstraint(f"origin IN ({_in(ORIGINS)})", name="threads_account_post_origin"),
        CheckConstraint(f"origin_evidence IN ({_in(ORIGIN_EVIDENCE)})",
                        name="threads_account_post_evidence"),
        CheckConstraint(f"post_kind IN ({_in(POST_KINDS)})", name="threads_account_post_kind"),
        # system だけが公開の記録を持つ (manual / unknown は持たない)。
        CheckConstraint(
            "(origin = 'system' AND threads_publication_id IS NOT NULL) OR "
            "(origin != 'system' AND threads_publication_id IS NULL)",
            name="threads_account_post_system_has_publication",
        ),
        Index("ix_threads_account_posts_published_at", "published_at"),
    )  # fmt: skip

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Threads の投稿 ID。同一性はこれだけで決める。
    threads_media_id: Mapped[str] = mapped_column(String(64), nullable=False)
    permalink: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: Threads が返した公開時刻 (system は公開の記録の時刻)。
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_evidence: Mapped[str] = mapped_column(String(32), nullable=False)
    threads_publication_id: Mapped[int | None] = mapped_column(
        ForeignKey("threads_publications.id", ondelete="RESTRICT"), nullable=True
    )
    #: normal / growth / unknown。manual は unknown で始まり、人だけが分類を変える。
    post_kind: Mapped[str] = mapped_column(String(16), nullable=False, default=KIND_UNKNOWN)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 最後に見た本文 (重複・話題・言い回しの判定に使う)。変化は event に残る。
    latest_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 一覧に出なくなった最初の時刻 (削除とは決めない。再び出たら NULL に戻す)。
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ThreadsAccountPostEvent(Base):
    """自アカウントの投稿の出来事 (追記だけ。更新・削除しない)。"""

    __tablename__ = "threads_account_post_events"

    __table_args__ = (
        CheckConstraint(f"event_type IN ({_in(ACCOUNT_POST_EVENTS)})",
                        name="threads_account_post_event_type"),
        Index("ix_threads_account_post_events_post", "threads_account_post_id", "occurred_at"),
    )  # fmt: skip

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    threads_account_post_id: Mapped[int] = mapped_column(
        ForeignKey("threads_account_posts.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 人の操作なら人の名前、自動なら ``system:<処理>``。
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    account_post: Mapped[ThreadsAccountPost] = relationship()


@event.listens_for(ThreadsAccountPostEvent, "before_update")
def _events_are_append_only_update(_mapper, _connection, target) -> None:
    raise RuntimeError(f"threads_account_post_events are append-only (event {target.id})")


@event.listens_for(ThreadsAccountPostEvent, "before_delete")
def _events_are_append_only_delete(_mapper, _connection, target) -> None:
    raise RuntimeError(f"threads_account_post_events are append-only (event {target.id})")


@event.listens_for(Session, "before_flush")
def _link_snapshots_to_account_posts(session, _flush_context, _instances) -> None:
    """公開の記録の観測 (``threads_publication_id`` だけを持つ snapshot) を、その公開の
    system 投稿の行に結ぶ。無ければ公開の記録から作る (公開の記録が system の証拠)。

    既存の公開の観測の経路 (insights の取り込み・試験の fixture) はそのまま動き、観測は必ず
    自アカウントの投稿の行を持つ (``threads_account_post_id`` は NOT NULL)。
    """

    from app.models.threads_insight_snapshot import ThreadsInsightSnapshot

    pending = [obj for obj in session.new if isinstance(obj, ThreadsInsightSnapshot)
               and obj.threads_account_post_id is None and obj.account_post is None
               and obj.threads_publication_id is not None]  # fmt: skip
    for snapshot in pending:
        snapshot.account_post = ensure_system_account_post(
            session, snapshot.threads_publication_id, media_id=snapshot.threads_media_id)


def ensure_system_account_post(session, publication_id: int, *, media_id: str | None = None,
                               now: datetime | None = None) -> ThreadsAccountPost:  # fmt: skip
    """公開の記録の system 投稿の行を返す (無ければ作る)。投稿 ID が食い違えば止める。"""

    from sqlalchemy import select

    from app.models.threads_publication import ThreadsPublication

    publication = session.get(ThreadsPublication, publication_id)
    if publication is None:
        raise ValueError(f"threads publication {publication_id} does not exist")
    media = publication.threads_media_id or media_id
    if not media:
        raise ValueError(f"threads publication {publication_id} has no Threads post id")
    if media_id and publication.threads_media_id and media_id != publication.threads_media_id:
        raise ValueError(f"snapshot post id differs from publication {publication_id}")
    with session.no_autoflush:
        pending = [o for o in session.new if isinstance(o, ThreadsAccountPost)
                   and o.threads_media_id == media]  # fmt: skip
        post = pending[0] if pending else session.scalars(
            select(ThreadsAccountPost).where(ThreadsAccountPost.threads_media_id == media)
        ).first()
    if post is not None:
        if post.origin != ORIGIN_SYSTEM:
            # 公開の記録が見つかった: 同じ行を system に照合する (別の行を作らない)。
            reconcile_to_system(session, post, publication, now=now,
                                actor="system:insight_snapshot")
        elif post.threads_publication_id not in (None, publication.id):
            raise ValueError(f"Threads post {media} belongs to another publication")
        return post
    moment = now or datetime.now(UTC)
    post = ThreadsAccountPost(
        threads_media_id=media, permalink=publication.permalink,
        published_at=publication.published_at, origin=ORIGIN_SYSTEM,
        origin_evidence=EVIDENCE_PUBLICATION_RECORD, threads_publication_id=publication.id,
        post_kind=system_post_kind(session, publication), media_type=None,
        latest_text=publication.exact_published_text,
        latest_text_hash=text_hash(publication.exact_published_text),
        first_seen_at=moment, last_seen_at=moment)  # fmt: skip
    session.add(post)
    session.add(ThreadsAccountPostEvent(
        account_post=post, event_type=EV_RECONCILED_SYSTEM, occurred_at=moment,
        actor="system:publication_record", detail_json={"publication_id": publication.id}))
    return post


def reconcile_to_system(session, post: ThreadsAccountPost, publication, *, now=None,
                        actor: str) -> None:  # fmt: skip
    """manual / unknown の行を、公開の記録がある system に照合する (逆は無い)。"""

    moment = now or datetime.now(UTC)
    previous = {"origin": post.origin, "post_kind": post.post_kind,
                "origin_evidence": post.origin_evidence}  # fmt: skip
    post.origin = ORIGIN_SYSTEM
    post.origin_evidence = EVIDENCE_PUBLICATION_RECORD
    post.threads_publication_id = publication.id
    post.post_kind = system_post_kind(session, publication)
    session.add(ThreadsAccountPostEvent(
        account_post=post, event_type=EV_ORIGIN_CHANGED, occurred_at=moment, actor=actor,
        detail_json={"from": previous, "to": {"origin": ORIGIN_SYSTEM,
                                               "post_kind": post.post_kind},
                     "publication_id": publication.id}))  # fmt: skip


def system_post_kind(_session, publication) -> str:
    """system 投稿の種類。公開の記録の事実だけで決める (保存の途中で提案を読まない)。

    記事あり → normal、記事なし + 切り口 ``account_growth`` → growth (キューの Growth の数え方と
    同じ。本番の 54 件で提案の印と一致することを確かめた)、それ以外 → unknown (推測しない)。
    """

    if publication.source_article_id is not None:
        return KIND_NORMAL
    if publication.angle == "account_growth":
        return KIND_GROWTH
    return KIND_UNKNOWN


def text_hash(text: str | None) -> str | None:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


__all__ = ["ACCOUNT_POST_EVENTS", "EVIDENCE_NO_PUBLICATION_RECORD",
           "EVIDENCE_PUBLICATION_IN_FLIGHT", "EVIDENCE_PUBLICATION_RECORD",
           "EV_CLASSIFICATION_CHANGED",
           "EV_DISCOVERED", "EV_METRICS_UNAVAILABLE", "EV_MISSING_FROM_LISTING",
           "EV_ORIGIN_CHANGED", "EV_PROPOSAL_SUPERSEDED", "EV_REAPPEARED", "EV_RECONCILED_SYSTEM",
           "EV_TEXT_CHANGED", "KIND_GROWTH", "KIND_NORMAL", "KIND_UNKNOWN", "ORIGINS",
           "ORIGIN_EVIDENCE", "ORIGIN_MANUAL", "ORIGIN_SYSTEM", "ORIGIN_UNKNOWN", "POST_KINDS",
           "ThreadsAccountPost", "ThreadsAccountPostEvent", "ensure_system_account_post",
           "reconcile_to_system", "system_post_kind", "text_hash"]
