"""ThreadsAccountPostService -- 自アカウントの投稿の発見・照合・分類・観測 (2026-10-02)。

manual-post coexistence: 人が Threads のアプリ・ブラウザから投稿しても、

- 自動公開の数え方 (本数・Growth の 1 日 1 本・承認・公開の成功) は ``threads_publications``
  のまま。manual 投稿はそこに入らない (架空の提案も作らない)。
- 間隔 (cooldown)・投稿の密度・重複・話題・言い回し・観測 (insights)・分析には入る。

照合の規則 (同一性は Threads の投稿 ID だけ。本文では統合しない):

A. 公開の記録 (``status = published``) と投稿 ID が一致 → ``system``
B. 公開の記録が無く、処理中の公開も無い → ``manual``
C. 安全に決められない (同じ ID の公開が処理中・照合待ち / ID の分からない処理中の公開がある)
   → ``unknown``
D. 後から公開の記録が見つかった → 同じ行を ``system`` に照合する (別の行を作らない)

``system`` から ``manual`` へは戻さない。一覧に出なくなっただけでは削除と決めない
(``missing_from_listing`` の出来事まで)。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware, to_storage_utc
from app.exceptions import ApplicationError
from app.models import (
    PUB_IN_FLIGHT_STATES,
    PUB_PUBLISHED,
    SNAPSHOT_EMPTY,
    SNAPSHOT_FAILED,
    SNAPSHOT_OBSERVED,
    TP_APPROVED,
    TP_OPEN_STATES,
    TP_SUPERSEDED,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.models.threads_account_post import (
    EV_CLASSIFICATION_CHANGED,
    EV_DISCOVERED,
    EV_METRICS_UNAVAILABLE,
    EV_MISSING_FROM_LISTING,
    EV_ORIGIN_CHANGED,
    EV_PROPOSAL_SUPERSEDED,
    EV_REAPPEARED,
    EV_TEXT_CHANGED,
    EVIDENCE_NO_PUBLICATION_RECORD,
    EVIDENCE_PUBLICATION_IN_FLIGHT,
    KIND_GROWTH,
    KIND_NORMAL,
    KIND_UNKNOWN,
    ORIGIN_MANUAL,
    ORIGIN_SYSTEM,
    ORIGIN_UNKNOWN,
    ThreadsAccountPost,
    ThreadsAccountPostEvent,
    ensure_system_account_post,
    reconcile_to_system,
    text_hash,
)
from app.social.threads.errors import ThreadsError
from app.social.threads.models import MEDIA_METRICS

MIGRATION = "3d5382e2a6bd"
#: 自分の文章ではない投稿 (他の人の投稿の repost)。台帳には載せるが、間隔・重複には使わない。
NON_AUTHORED_MEDIA_TYPES = frozenset({"REPOST_FACADE"})
#: ID の分からない処理中の公開があるとき、その開始の何分前までの投稿を unknown にするか。
IN_FLIGHT_MARGIN = timedelta(minutes=10)
#: 提案を ``superseded`` にしてよい manual 投稿の新しさ。最初の一覧の読みで、ずっと前の
#: 手の投稿 (この仕組みより前のもの) が今の提案を消さないため。古い投稿も台帳・分析・
#: 同一の本文の判定には入る。
COLLISION_WINDOW = timedelta(days=14)
_SHORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


class AccountPostDiscoveryError(ApplicationError):
    """一覧を安全に読めなかった (公開は安全側に倒して延期する)。"""

    def __init__(self, reason: str, *, category: str = "unknown") -> None:
        super().__init__(f"account post discovery failed: {reason}")
        self.reason = reason
        self.category = category


class AccountPostError(ApplicationError):
    pass


def account_posts_ready(session: Session) -> bool:
    """migration ``3d5382e2a6bd`` が当たった DB か (読むだけ)。"""

    inspector = inspect(session.connection())
    if not inspector.has_table("threads_account_posts"):
        return False
    return any(c["name"] == "threads_account_post_id"
               for c in inspector.get_columns("threads_insight_snapshots"))


def parse_remote_time(value) -> datetime | None:
    """Threads の ``timestamp`` (例 ``2026-10-02T03:00:00+0000``)。読めなければ ``None``。"""

    if not isinstance(value, str) or not value:
        return None
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value.strip())
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def is_authored(post: ThreadsAccountPost) -> bool:
    return (post.media_type or "") not in NON_AUTHORED_MEDIA_TYPES


class ThreadsAccountPostService:
    def __init__(self, session: Session, *, threads_service=None, listing_limit: int = 100,
                 measurement_policy=None) -> None:  # fmt: skip
        self._session = session
        self._threads = threads_service
        self._limit = max(1, min(int(listing_limit), 100))
        self._measurement = measurement_policy

    # -- 読むだけの事実 -------------------------------------------------------------------
    def posts(self) -> list[ThreadsAccountPost]:
        return list(self._session.scalars(
            select(ThreadsAccountPost).order_by(ThreadsAccountPost.id)))  # fmt: skip

    def events(self, account_post_id: int) -> list[ThreadsAccountPostEvent]:
        return list(self._session.scalars(
            select(ThreadsAccountPostEvent)
            .where(ThreadsAccountPostEvent.threads_account_post_id == account_post_id)
            .order_by(ThreadsAccountPostEvent.id)))  # fmt: skip

    def latest_non_system_post_at(self) -> datetime | None:
        """manual / unknown の自分の投稿で、いちばん新しい公開時刻 (間隔・密度に使う)。"""

        rows = self._session.scalars(
            select(ThreadsAccountPost).where(
                ThreadsAccountPost.origin.in_((ORIGIN_MANUAL, ORIGIN_UNKNOWN)),
                ThreadsAccountPost.published_at.is_not(None),
            )).all()  # fmt: skip
        moments = [ensure_aware(r.published_at) for r in rows if is_authored(r)]
        return max(moments) if moments else None

    def recent_texts(self, *, limit: int) -> list[dict]:
        """manual / unknown の自分の投稿の本文 (新しい順)。重複・話題・言い回しに使う。"""

        rows = self._session.scalars(
            select(ThreadsAccountPost)
            .where(ThreadsAccountPost.origin.in_((ORIGIN_MANUAL, ORIGIN_UNKNOWN)),
                   ThreadsAccountPost.latest_text.is_not(None))
            .order_by(ThreadsAccountPost.published_at.desc(), ThreadsAccountPost.id.desc())
        ).all()  # fmt: skip
        out = []
        for row in rows:
            if not is_authored(row) or not (row.latest_text or "").strip():
                continue
            out.append({"ref": f"{row.origin} Threads post {row.threads_media_id}",
                        "text": row.latest_text, "origin": row.origin,
                        "post_kind": row.post_kind, "account_post_id": row.id,
                        "at": ensure_aware(row.published_at) if row.published_at else None})
            if len(out) >= limit:
                break
        return out

    # -- 発見 (一覧を読む) ----------------------------------------------------------------
    def refresh(self, *, now: datetime, source: str) -> dict:
        """一覧を 1 回読んで照合する。読めなければ ``AccountPostDiscoveryError``。"""

        if self._threads is None:
            raise AccountPostDiscoveryError("no Threads service", category="not_configured")
        if not account_posts_ready(self._session):
            self._session.rollback()
            raise AccountPostDiscoveryError(f"account post tables missing (migration {MIGRATION}"
                                            " not applied)", category="schema_not_ready")
        try:
            items = self._threads.list_account_posts(limit=self._limit)
        except ThreadsError as exc:
            raise AccountPostDiscoveryError(exc.reason, category=exc.category) from None
        except Exception as exc:  # noqa: BLE001 - 読めないときは安全側 (公開しない)
            raise AccountPostDiscoveryError(type(exc).__name__) from None
        return self.reconcile(items, now=now, source=source)

    def reconcile(self, items: list[dict], *, now: datetime, source: str) -> dict:
        """一覧の項目を台帳に照合する (DB だけ。外に問い合わせない)。"""

        now = ensure_aware(now)
        stamp = to_storage_utc(now)
        actor = f"system:discovery:{source}"[:64]
        summary = {"source": source, "listed": 0, "discovered": [], "reconciled_system": [],
                   "origin_changed": [], "text_changed": [], "reappeared": [], "missing": [],
                   "superseded_proposals": [], "skipped": 0}  # fmt: skip
        listed_ids: set[str] = set()
        times: list[datetime] = []
        for item in items:
            media_id = str(item.get("id") or "").strip()
            if not media_id or len(media_id) > 64:
                summary["skipped"] += 1
                continue
            listed_ids.add(media_id)
            summary["listed"] += 1
            published = parse_remote_time(item.get("timestamp"))
            if published is not None:
                times.append(published)
            self._reconcile_item(media_id, item, published, stamp, actor, summary)
        self._mark_missing(listed_ids, times, stamp, actor, summary)
        self._session.commit()
        return summary

    def _publication_for(self, media_id: str) -> ThreadsPublication | None:
        return self._session.scalars(
            select(ThreadsPublication).where(ThreadsPublication.threads_media_id == media_id)
        ).first()

    def _in_flight_unidentified(self, published: datetime | None) -> list[int]:
        """投稿 ID がまだ分からない処理中の公開 (その投稿かもしれない)。"""

        rows = self._session.scalars(select(ThreadsPublication).where(
            or_(ThreadsPublication.status.in_(tuple(PUB_IN_FLIGHT_STATES)),
                ThreadsPublication.reconciliation_required.is_(True)),
            ThreadsPublication.threads_media_id.is_(None))).all()  # fmt: skip
        out = []
        for row in rows:
            started = row.publish_started_at or row.created_at
            if published is None or started is None or (
                    ensure_aware(started) - IN_FLIGHT_MARGIN <= published):
                out.append(row.id)
        return out

    def _origin_for(self, media_id: str, published: datetime | None):
        pub = self._publication_for(media_id)
        if pub is not None and pub.status == PUB_PUBLISHED:
            return ORIGIN_SYSTEM, pub, None
        if pub is not None:
            return ORIGIN_UNKNOWN, None, {"publication_id": pub.id, "status": pub.status}
        pending = self._in_flight_unidentified(published)
        if pending:
            return ORIGIN_UNKNOWN, None, {"in_flight_publications": pending}
        return ORIGIN_MANUAL, None, None

    def _event(self, post, event_type: str, stamp, actor: str, detail: dict | None) -> None:
        self._session.add(ThreadsAccountPostEvent(
            account_post=post, event_type=event_type, occurred_at=stamp, actor=actor,
            detail_json=detail))  # fmt: skip

    def _reconcile_item(self, media_id, item, published, stamp, actor, summary) -> None:
        text = item.get("text") if isinstance(item.get("text"), str) else None
        permalink = item.get("permalink") if isinstance(item.get("permalink"), str) else None
        media_type = item.get("media_type") if isinstance(item.get("media_type"), str) else None
        post = self._session.scalars(
            select(ThreadsAccountPost).where(ThreadsAccountPost.threads_media_id == media_id)
        ).first()
        origin, pub, unknown_detail = self._origin_for(media_id, published)
        if post is None:
            if origin == ORIGIN_SYSTEM:
                post = ensure_system_account_post(self._session, pub.id, media_id=media_id,
                                                  now=stamp)  # fmt: skip
                summary["reconciled_system"].append(media_id)
            else:
                post = ThreadsAccountPost(
                    threads_media_id=media_id, permalink=permalink,
                    published_at=to_storage_utc(published) if published else None,
                    origin=origin,
                    origin_evidence=(EVIDENCE_NO_PUBLICATION_RECORD if origin == ORIGIN_MANUAL
                                     else EVIDENCE_PUBLICATION_IN_FLIGHT),
                    threads_publication_id=None, post_kind=KIND_UNKNOWN, media_type=media_type,
                    latest_text=text, latest_text_hash=text_hash(text),
                    first_seen_at=stamp, last_seen_at=stamp)  # fmt: skip
                self._session.add(post)
                self._event(post, EV_DISCOVERED, stamp, actor,
                            {"origin": origin, "evidence": post.origin_evidence,
                             **({"why_unknown": unknown_detail} if unknown_detail else {})})
                summary["discovered"].append({"media_id": media_id, "origin": origin})
                self._session.flush()
                if origin == ORIGIN_MANUAL:
                    summary["superseded_proposals"] += self.resolve_collisions(
                        post, stamp=stamp, actor=actor)
            # system の行も、一覧で見た事実で補う (公開の記録は変えない)
            post.permalink = post.permalink or permalink
            post.media_type = post.media_type or media_type
            post.last_seen_at = stamp
            if post.latest_text is None and text:
                post.latest_text, post.latest_text_hash = text, text_hash(text)
            return

        post.last_seen_at = stamp
        post.permalink = post.permalink or permalink
        post.media_type = post.media_type or media_type
        if post.published_at is None and published is not None:
            post.published_at = to_storage_utc(published)
        if post.missing_since is not None:
            self._event(post, EV_REAPPEARED, stamp, actor,
                        {"missing_since": post.missing_since.isoformat()})
            post.missing_since = None
            summary["reappeared"].append(media_id)
        if text is not None and text_hash(text) != post.latest_text_hash:
            self._event(post, EV_TEXT_CHANGED, stamp, actor,
                        {"previous_hash": post.latest_text_hash, "new_hash": text_hash(text)})
            post.latest_text, post.latest_text_hash = text, text_hash(text)
            summary["text_changed"].append(media_id)
            if post.origin == ORIGIN_MANUAL:
                summary["superseded_proposals"] += self.resolve_collisions(
                    post, stamp=stamp, actor=actor)
        if post.origin == ORIGIN_SYSTEM:
            return  # system は manual / unknown に戻さない
        if origin == ORIGIN_SYSTEM:
            reconcile_to_system(self._session, post, pub, now=stamp, actor=actor)
            summary["origin_changed"].append({"media_id": media_id, "to": ORIGIN_SYSTEM})
        elif post.origin == ORIGIN_UNKNOWN and origin == ORIGIN_MANUAL:
            self._event(post, EV_ORIGIN_CHANGED, stamp, actor,
                        {"from": {"origin": ORIGIN_UNKNOWN}, "to": {"origin": ORIGIN_MANUAL},
                         "reason": "no publication record and no publication in flight"})
            post.origin, post.origin_evidence = ORIGIN_MANUAL, EVIDENCE_NO_PUBLICATION_RECORD
            summary["origin_changed"].append({"media_id": media_id, "to": ORIGIN_MANUAL})
            self._session.flush()
            summary["superseded_proposals"] += self.resolve_collisions(
                post, stamp=stamp, actor=actor)

    def _mark_missing(self, listed_ids, times, stamp, actor, summary) -> None:
        """一覧の時間の範囲の中にあるはずなのに出なかった投稿 (削除とは決めない)。"""

        if not times:
            return
        oldest = to_storage_utc(min(times))
        for post in self._session.scalars(select(ThreadsAccountPost).where(
                ThreadsAccountPost.missing_since.is_(None),
                ThreadsAccountPost.published_at.is_not(None))).all():  # fmt: skip
            if post.threads_media_id in listed_ids or post.published_at < oldest:
                continue
            post.missing_since = stamp
            self._event(post, EV_MISSING_FROM_LISTING, stamp, actor,
                        {"note": "not in the listing window; deletion is not assumed"})
            summary["missing"].append(post.threads_media_id)

    # -- 提案との重なり ------------------------------------------------------------------
    def resolve_collisions(self, post: ThreadsAccountPost, *, stamp, actor: str) -> list[dict]:
        """manual 投稿と実質的に同じ (既存の重複の方針で) まだ出していない提案を
        ``superseded`` にする。**公開の成功としては記録しない** (公開の行を作らない)。"""

        from app.social.threads.growth import recent_similarity
        from app.social.threads.quality import overlap
        from app.social.threads.topic import is_account_growth
        from app.social.threads.validators import normalized_identity

        text = (post.latest_text or "").strip()
        if not text or post.origin != ORIGIN_MANUAL or not is_authored(post):
            return []
        if post.published_at is None or ensure_aware(post.published_at) < ensure_aware(
                stamp) - COLLISION_WINDOW:  # fmt: skip
            return []  # 新しさが分からない・古い手の投稿では提案を消さない
        published = set(self._session.scalars(select(ThreadsPublication.proposal_id)).all())
        rows = self._session.scalars(select(ThreadsPostProposal).where(
            ThreadsPostProposal.status.in_((*TP_OPEN_STATES, TP_APPROVED)))).all()  # fmt: skip
        ref = f"manual Threads post {post.threads_media_id}"
        done = []
        for proposal in rows:
            if proposal.id in published:
                continue  # 公開 (の試み) がある提案は触らない
            body = proposal.content_text or ""
            if is_account_growth(proposal):
                check = recent_similarity(body, [{"ref": ref, "text": text}])
                rule = ("growth:" + ",".join(check["blocked_rules"])) if check["blocked"] else None
            elif normalized_identity(body) == normalized_identity(text):
                rule = "identical"
            else:
                rule = "topic_overlap" if overlap(body, text)["high"] else None
            if rule is None:
                continue
            previous = proposal.status
            proposal.status = TP_SUPERSEDED
            proposal.superseded_at = stamp
            proposal.status_reason = (f"{ref} made proposal unnecessary ({rule}); not a system "
                                      "publication")[:500]
            self._event(post, EV_PROPOSAL_SUPERSEDED, stamp, actor,
                        {"proposal_id": proposal.id, "rule": rule, "previous_status": previous})
            done.append({"proposal_id": proposal.id, "rule": rule,
                         "media_id": post.threads_media_id})
        return done

    # -- 人の分類 ------------------------------------------------------------------------
    def classify(self, account_post_id: int, *, kind: str, by: str, reason: str,
                 execute: bool = False, now: datetime | None = None) -> dict:  # fmt: skip
        """manual / unknown の投稿を人が normal / growth に分類する (既定は PLAN)。

        値は上書きせず、出来事 (``classification_changed``) を追記する。system の種類は提案から
        決まるので変えない。
        """

        now = ensure_aware(now or datetime.now(UTC))
        post = self._session.get(ThreadsAccountPost, account_post_id)
        if post is None:
            raise AccountPostError(f"account post {account_post_id} does not exist")
        if kind not in (KIND_NORMAL, KIND_GROWTH):
            raise AccountPostError("kind is normal or growth")
        if post.origin == ORIGIN_SYSTEM:
            raise AccountPostError("a system post's kind comes from its proposal; it is not "
                                   "classified by hand")
        who = (by or "").strip()
        if not _SHORT_NAME.match(who) or "@" in who:
            raise AccountPostError("--by is a short name (no email)")
        why = (reason or "").strip()
        if len(why) < 3 or len(why) > 200 or "http" in why.lower():
            raise AccountPostError("--reason is 3-200 characters without a URL")
        if post.post_kind == kind:
            self._session.rollback()
            return {"executed": False, "changed": False, "reason": f"already {kind}"}
        plan = {"account_post_id": post.id, "threads_media_id": post.threads_media_id,
                "origin": post.origin, "from": post.post_kind, "to": kind, "by": who,
                "reason": why}  # fmt: skip
        if not execute:
            self._session.rollback()
            return {"executed": False, "changed": True, "plan": plan,
                    "note": "PLAN only; re-run with --execute"}
        stamp = to_storage_utc(now)
        self._event(post, EV_CLASSIFICATION_CHANGED, stamp, who[:64],
                    {"from": post.post_kind, "to": kind, "reason": why, "provenance": "human"})
        post.post_kind = kind
        self._session.commit()
        return {"executed": True, "changed": True, "plan": plan}

    # -- manual / unknown の投稿の観測 --------------------------------------------------
    def due_for_insights(self, now: datetime, *, by_maturity: dict, stop_after_hours: float,
                         default_minutes: int = 60) -> dict:  # fmt: skip
        """観測の期限が来た manual / unknown の投稿 (system は公開の記録の経路で観測する)。

        返り値: ``{"due": [account_post_id...], "intervals": [timedelta...]}``
        """

        from app.social.threads.measurement import classify_maturity

        due, intervals = [], []
        for post in self._session.scalars(select(ThreadsAccountPost).where(
                ThreadsAccountPost.origin.in_((ORIGIN_MANUAL, ORIGIN_UNKNOWN)),
                ThreadsAccountPost.published_at.is_not(None))).all():  # fmt: skip
            age = (now - ensure_aware(post.published_at)).total_seconds() / 3600.0
            if age > stop_after_hours or not is_authored(post):
                continue
            stage = classify_maturity(age, self._measurement_policy()).stage
            interval = timedelta(minutes=int(by_maturity.get(stage, default_minutes)))
            last = self._last_observed_at(post.id)
            intervals.append(interval)
            if last is None or last + interval <= now:
                due.append(post.id)
        return {"due": due, "intervals": intervals}

    def collect_insights(self, account_post_ids, *, now: datetime) -> dict:
        """指標を読み、観測を 1 行積む (manual / unknown)。欠測は NULL。失敗も 1 行残す。"""

        now = ensure_aware(now)
        spacing = timedelta(minutes=self._measurement_policy().min_observation_spacing_minutes)
        details, calls = [], 0
        for post_id in account_post_ids:
            post = self._session.get(ThreadsAccountPost, post_id)
            if post is None or post.origin == ORIGIN_SYSTEM:
                continue
            last = self._last_observed_at(post.id)
            if last is not None and now - last < spacing:
                details.append({"account_post_id": post.id, "result": "unchanged"})
                continue
            calls += 1
            try:
                insights = self._threads.media_insights(post.threads_media_id, MEDIA_METRICS)
            except ThreadsError as exc:
                previous = self._latest_snapshot(post.id)
                self._append(post, now, {}, MEDIA_METRICS, SNAPSHOT_FAILED, exc)
                if previous is None or previous.outcome != SNAPSHOT_FAILED:
                    self._event(post, EV_METRICS_UNAVAILABLE, to_storage_utc(now),
                                "system:insights", {"category": exc.category})
                    self._session.commit()
                details.append({"account_post_id": post.id, "result": "failed",
                                "category": exc.category, "reason": exc.reason})
                continue
            values = dict(insights.values)
            stored = self._append(post, now, values, list(insights.missing),
                                  SNAPSHOT_OBSERVED if values else SNAPSHOT_EMPTY, None)
            details.append({"account_post_id": post.id,
                            "result": "imported" if stored else "unchanged"})
        return {"network_calls": calls, "details": details}

    def _measurement_policy(self):
        if self._measurement is None:
            from app.social.threads.policy import get_measurement_policy

            self._measurement = get_measurement_policy()
        return self._measurement

    def _append(self, post, now, values, missing, outcome, error):
        age = ((now - ensure_aware(post.published_at)).total_seconds() / 3600.0
               if post.published_at else None)
        snapshot = ThreadsInsightSnapshot(
            threads_publication_id=None, threads_account_post_id=post.id,
            threads_media_id=post.threads_media_id, observed_at=to_storage_utc(now),
            age_hours=age, outcome=outcome, missing_json=list(missing) or None,
            error_category=error.category if error else None,
            error_message=error.reason if error else None)  # fmt: skip
        for metric in MEDIA_METRICS:
            if metric in values and values[metric] is not None:
                setattr(snapshot, metric, int(values[metric]))
        self._session.add(snapshot)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()  # 同じ投稿・同じ観測時刻は 1 行だけ
            return None
        return snapshot

    def _latest_snapshot(self, post_id: int):
        return self._session.scalars(
            select(ThreadsInsightSnapshot)
            .where(ThreadsInsightSnapshot.threads_account_post_id == post_id)
            .order_by(ThreadsInsightSnapshot.observed_at.desc(), ThreadsInsightSnapshot.id.desc())
            .limit(1)).first()  # fmt: skip

    def _last_observed_at(self, post_id: int) -> datetime | None:
        latest = self._latest_snapshot(post_id)
        return ensure_aware(latest.observed_at) if latest is not None else None

    # -- 分析の材料 (origin つき、T6.6 の単位) ----------------------------------------------
    def dataset(self, *, now: datetime, tz=None) -> list[dict]:
        """投稿ごとの観測の系列。**origin と種類を必ず持つ**。分からないものは unknown。"""

        from app.operations.local_time import to_local

        now = ensure_aware(now)
        rows = []
        for post in self.posts():
            publication = (self._session.get(ThreadsPublication, post.threads_publication_id)
                           if post.threads_publication_id else None)
            snapshots = self._session.scalars(
                select(ThreadsInsightSnapshot)
                .where(ThreadsInsightSnapshot.threads_account_post_id == post.id)
                .order_by(ThreadsInsightSnapshot.observed_at, ThreadsInsightSnapshot.id)).all()
            published = ensure_aware(post.published_at) if post.published_at else None
            text = post.latest_text or ""
            rows.append({
                "account_post_id": post.id, "threads_media_id": post.threads_media_id,
                "origin": post.origin, "origin_evidence": post.origin_evidence,
                "post_kind": post.post_kind,
                "angle": publication.angle if publication is not None else "unknown",
                "publication_id": post.threads_publication_id,
                "published_at": published.isoformat() if published else None,
                "published_local_hour": (to_local(published, tz).hour
                                         if published is not None and tz is not None else None),
                "missing_since": post.missing_since.isoformat() if post.missing_since else None,
                "features": {"character_count": len(text), "has_link": "http" in text,
                             "authored": is_authored(post), "media_type": post.media_type},
                "observations": [{
                    "observed_at": ensure_aware(s.observed_at).isoformat(),
                    "post_age_hours": (round((ensure_aware(s.observed_at) - published)
                                             .total_seconds() / 3600.0, 2)
                                       if published else None),
                    "outcome": s.outcome,
                    "metrics": {m: getattr(s, m) for m in MEDIA_METRICS}} for s in snapshots],
            })  # fmt: skip
        return rows


__all__ = ["AccountPostDiscoveryError", "AccountPostError", "MIGRATION",
           "ThreadsAccountPostService", "account_posts_ready", "is_authored",
           "parse_remote_time"]
