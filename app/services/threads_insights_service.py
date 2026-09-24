"""ThreadsInsightsService -- Threads の計測と、そこから言えることだけ (T4)。

やること:

1. 公開済み投稿の指標を **読むだけ** で取得し、観測ごとに 1 行積む。
2. 成熟度を経過時間だけで判定する (公開直後の 0 を成績と読まない)。
3. 事実としての観察と、母数が足りたときだけの集計を出す。
4. 次の提案づくりへの **控えめな** 助言を作る。

やらないこと:

- 投稿しない。作成/公開エンドポイントを呼ばない。
- 提案の文面を書き換えない。
- 提案を自動生成しない。承認しない。公開しない。
- 1 本の結果から「この切り口が良い」と言わない。
- 欠測を 0 で埋めない。

Threads の view 数と GA4 のセッションは別の計測系である。片方がもう片方を
説明すると **主張しない**。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware, to_storage_utc
from app.exceptions import ApplicationError
from app.models import (
    PUB_PUBLISHED,
    SNAPSHOT_EMPTY,
    SNAPSHOT_FAILED,
    SNAPSHOT_OBSERVED,
    Ga4PageDaily,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.operations.local_time import local_hour, local_weekday, to_local
from app.operations.policy import get_policy as get_operations_policy
from app.operations.threads_health import ThreadsHealthInput, build_threads_alert_drafts
from app.social.threads.errors import ThreadsError
from app.social.threads.measurement import (
    classify_maturity,
    interaction_total,
    interactions_per_view,
    length_bucket,
    observe,
)
from app.social.threads.models import MEDIA_METRICS
from app.social.threads.policy import ThreadsMeasurementPolicy, get_measurement_policy
from app.social.threads.service import ThreadsService
from app.social.threads.style import POST_ANGLES

#: 助言の種類。どれも **人が次に何を試すか** の提案であって、自動実行はしない。
REC_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
REC_COLLECT_MORE = "COLLECT_MORE_OBSERVATIONS"
REC_ANGLE_DIVERSIFY = "ANGLE_DIVERSIFY"
REC_ANGLE_RETRY = "ANGLE_RETRY"
REC_OPENING_REWRITE = "OPENING_REWRITE"
REC_LENGTH_EXPERIMENT = "LENGTH_EXPERIMENT"
REC_LINK_MODE_EXPERIMENT = "LINK_MODE_EXPERIMENT"
REC_TOPIC_REUSE = "TOPIC_REUSE"
RECOMMENDATION_TYPES = (
    REC_INSUFFICIENT_DATA,
    REC_COLLECT_MORE,
    REC_ANGLE_DIVERSIFY,
    REC_ANGLE_RETRY,
    REC_OPENING_REWRITE,
    REC_LENGTH_EXPERIMENT,
    REC_LINK_MODE_EXPERIMENT,
    REC_TOPIC_REUSE,
)


class ThreadsInsightsError(ApplicationError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"threads insights error: {reason}")
        self.reason = reason


@dataclass
class ImportOutcome:
    executed: bool
    checked: int = 0
    imported: int = 0
    unchanged: int = 0
    failed: int = 0
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "executed": self.executed,
            "checked": self.checked,
            "imported": self.imported,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "details": list(self.details),
        }


class ThreadsInsightsService:
    def __init__(
        self,
        session: Session,
        *,
        settings,
        threads_service: ThreadsService | None = None,
        policy: ThreadsMeasurementPolicy | None = None,
        timezone: ZoneInfo | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._threads = threads_service or ThreadsService(settings)
        self._policy = policy or get_measurement_policy()
        # 運用タイムゾーンは C8 と同じ設定から取る (Threads 専用の設定を作らない)。
        self._tz = timezone or get_operations_policy().timezone

    # -- collection -----------------------------------------------------------
    def plan(self, *, publication_id: int | None = None, now: datetime | None = None) -> dict:
        """何を見に行くかだけを示す。**Meta へは 1 度も問い合わせない。**"""

        now = now or datetime.now(UTC)
        status = self._threads.describe()
        rows = self._publications(publication_id)
        return {
            "executed": False,
            "policy_version": self._policy.policy_version,
            "supported_metrics": list(MEDIA_METRICS),
            "unsupported_metrics": list(self._policy.unsupported_metrics),
            "metric_caveats": self._policy.metric_caveats,
            "threads_config": {
                "enabled": status.enabled,
                "configured": status.configured,
                "api_version": status.api_version,
            },
            "publications": [
                {
                    "publication_id": row.id,
                    "media_id": row.threads_media_id,
                    "published_at": _iso(row.published_at),
                    "age_hours": self._age_hours(row, now),
                    "maturity": classify_maturity(
                        self._age_hours(row, now), self._policy
                    ).as_dict(),
                    "last_snapshot": self._last_snapshot_summary(row.id),
                }
                for row in rows
            ],
            "threads_writes": 0,
        }

    def collect(
        self,
        *,
        publication_id: int | None = None,
        execute: bool = False,
        now: datetime | None = None,
    ) -> ImportOutcome:
        """指標を読み、観測を 1 行積む。``execute=False`` では何も書かない。"""

        now = now or datetime.now(UTC)
        outcome = ImportOutcome(executed=execute)
        rows = self._publications(publication_id)
        outcome.checked = len(rows)
        if not execute:
            for row in rows:
                outcome.details.append(
                    {"publication_id": row.id, "result": "would_fetch", "reason": None}
                )
            return outcome

        spacing = timedelta(minutes=self._policy.min_observation_spacing_minutes)
        for row in rows:
            detail = {"publication_id": row.id, "media_id": row.threads_media_id}
            # C8 の日次取り込みと常駐 worker は同じこの経路を通る。どちらかが直前に
            # 観測したばかりなら、もう一度 Meta に取りに行かない (T4.2)。
            last = self._last_observed_at(row.id)
            if last is not None and now - last < spacing:
                outcome.unchanged += 1
                minutes = round((now - last).total_seconds() / 60, 1)
                outcome.details.append(
                    {**detail, "result": "unchanged", "reason": f"observed {minutes} min ago"}
                )
                continue
            try:
                insights = self._threads.media_insights(row.threads_media_id, MEDIA_METRICS)
            except ThreadsError as exc:
                self._append(
                    row,
                    now,
                    values={},
                    missing=MEDIA_METRICS,
                    outcome_value=SNAPSHOT_FAILED,
                    error=exc,
                )
                outcome.failed += 1
                outcome.details.append({**detail, "result": "failed", "reason": exc.reason})
                continue

            values = dict(insights.values)
            missing = list(insights.missing)
            snapshot_outcome = SNAPSHOT_OBSERVED if values else SNAPSHOT_EMPTY
            stored = self._append(
                row, now, values=values, missing=missing, outcome_value=snapshot_outcome
            )
            if stored is None:
                outcome.unchanged += 1
                outcome.details.append(
                    {**detail, "result": "unchanged", "reason": "an observation already exists"}
                )
                continue
            outcome.imported += 1
            outcome.details.append(
                {
                    **detail,
                    "result": "imported",
                    "snapshot_id": stored.id,
                    "observed_at": _iso(stored.observed_at),
                    "values": values,
                    "missing": missing,
                }
            )
        return outcome

    # -- reporting -------------------------------------------------------------
    def report(self, *, now: datetime | None = None) -> dict:
        """観測から **言えることだけ** をまとめる。"""

        now = now or datetime.now(UTC)
        publications = self._publications(None)
        per_post: list[dict] = []
        for row in publications:
            latest = self._latest_snapshot(row.id)
            age = self._age_hours(row, now)
            maturity = classify_maturity(age, self._policy)
            snapshot = _snapshot_values(latest)
            proposal = self._session.get(ThreadsPostProposal, row.proposal_id)
            per_post.append(
                {
                    "publication_id": row.id,
                    "proposal_id": row.proposal_id,
                    "article_id": row.source_article_id,
                    "angle": row.angle,
                    "link_mode": getattr(proposal, "link_mode", None),
                    "policy_version": getattr(proposal, "policy_version", None),
                    "generator_version": getattr(proposal, "generator_version", None),
                    "character_count": getattr(proposal, "character_count", None),
                    "length_bucket": (
                        length_bucket(proposal.character_count, self._policy) if proposal else None
                    ),
                    # 生の時刻は UTC のまま。時刻・曜日は **運用タイムゾーンの壁時計** で出す
                    # (人が投稿を読む生活時間で意味を持つ値だから)。
                    "published_at": _iso(row.published_at),
                    "published_local_at": (
                        to_local(row.published_at, self._tz).isoformat()
                        if row.published_at
                        else None
                    ),
                    "published_local_hour": (
                        local_hour(row.published_at, self._tz) if row.published_at else None
                    ),
                    "published_local_weekday": (
                        local_weekday(row.published_at, self._tz) if row.published_at else None
                    ),
                    "permalink": row.permalink,
                    "maturity": maturity.as_dict(),
                    "observed_at": _iso(latest.observed_at) if latest else None,
                    "metrics": snapshot,
                    "interaction_total": interaction_total(snapshot),
                    "interactions_per_view": interactions_per_view(
                        snapshot, self._policy, maturity
                    ),
                    "observations": observe(snapshot, maturity, self._policy),
                    "snapshot_count": self._snapshot_count(row.id),
                }
            )

        mature = [p for p in per_post if p["maturity"]["comparable"]]
        return {
            "generated_at": now.isoformat(),
            "policy_version": self._policy.policy_version,
            "publication_count": len(per_post),
            "mature_count": len(mature),
            "minimum_mature_posts_per_dimension": self._policy.minimum_mature_posts,
            "publications": per_post,
            "by_angle": self._aggregate(mature, "angle"),
            "by_link_mode": self._aggregate(mature, "link_mode"),
            "by_length_bucket": self._aggregate(mature, "length_bucket"),
            "local_timezone": self._tz.key,
            "by_published_local_hour": self._aggregate(mature, "published_local_hour"),
            "by_published_local_weekday": self._aggregate(mature, "published_local_weekday"),
            "website_attribution": self._attribution(per_post),
            "recommendations": self._recommendations(per_post, mature),
            "caveats": [
                'Threads の views は公式に "in development" と注記されている。',
                "返信の返信は集計に含まれない (公式の注記)。",
                "1 本の結果から切り口の優劣は言えない。",
                "Threads の view と サイトのセッションは別の計測系であり、"
                "片方がもう片方を説明するとは主張しない。",
            ],
        }

    # -- health ----------------------------------------------------------------
    def health_inputs(self) -> list[ThreadsHealthInput]:
        """アラート判定の材料を DB だけから作る (**追加の API 呼び出しをしない**)。

        指標の **値は一切見ない**。見るのは「取得できているか」「承認された文面と
        一致しているか」だけである。数字が伸びないことは障害ではない。

        公開中の文面が遠隔で書き換わっていないかの照合は
        ``scripts/check_threads_post.py`` 側 (T3) が担当する。ここでは日次で
        API を増やさないため、手元に保存した公開文字列と承認文面を突き合わせる。
        """

        inputs: list[ThreadsHealthInput] = []
        for row in self._publications(None):
            recent = self._session.scalars(
                select(ThreadsInsightSnapshot)
                .where(ThreadsInsightSnapshot.threads_publication_id == row.id)
                .order_by(ThreadsInsightSnapshot.observed_at.desc())
                .limit(10)
            ).all()
            consecutive = 0
            category = None
            reason = None
            for snapshot in recent:
                if snapshot.outcome != SNAPSHOT_FAILED:
                    break
                consecutive += 1
                if category is None:
                    category = snapshot.error_category
                    reason = snapshot.error_message

            proposal = self._session.get(ThreadsPostProposal, row.proposal_id)
            matches = proposal is None or row.exact_published_text == proposal.content_text
            inputs.append(
                ThreadsHealthInput(
                    publication_id=row.id,
                    failure_category=category,
                    failure_reason=reason,
                    consecutive_failures=consecutive,
                    media_readable=category != "threads_response",
                    text_matches_approved=matches,
                )
            )
        return inputs

    def alert_drafts(self) -> list:
        """計測の故障だけをアラート草案にする。**成績では 1 件も出さない。**"""

        return build_threads_alert_drafts(self.health_inputs())

    # -- aggregation -----------------------------------------------------------
    def _aggregate(self, mature: list[dict], key: str) -> dict:
        """母数が足りた次元だけ数字を出す。足りなければ件数だけ。"""

        grouped: dict[object, list[dict]] = defaultdict(list)
        for post in mature:
            grouped[post.get(key)].append(post)

        out: dict[str, dict] = {}
        for value, posts in grouped.items():
            views = [p["metrics"].get("views") for p in posts]
            observed_views = [v for v in views if v is not None]
            totals = [p["interaction_total"] for p in posts]
            observed_totals = [t for t in totals if t is not None]
            enough = len(posts) >= self._policy.minimum_mature_posts
            out[str(value)] = {
                "sample": len(posts),
                "sufficient_sample": enough,
                # 母数が足りないうちは平均を出さない (出すと比較したくなる)。
                "median_views": _median(observed_views) if enough else None,
                "median_interactions": _median(observed_totals) if enough else None,
                "views_observed_for": len(observed_views),
                "note": (
                    None
                    if enough
                    else f"{self._policy.minimum_mature_posts} 本そろうまで比較しない"
                ),
            }
        return out

    def _attribution(self, per_post: list[dict]) -> dict:
        """UTM を持つ投稿だけ、GA4 と突き合わせる **入口** を用意する。

        突き合わせるのは「同じ UTM を持つセッションがあるか」までで、Threads の
        表示がその訪問を生んだとは言わない。
        """

        linked = []
        for post in per_post:
            proposal = self._session.get(ThreadsPostProposal, post["proposal_id"])
            if proposal is None or proposal.link_mode != "article":
                continue
            linked.append(
                {
                    "publication_id": post["publication_id"],
                    "destination_url": proposal.destination_url,
                    "utm_campaign": f"article-{proposal.source_article_id}-{proposal.angle}",
                    "utm_content": proposal.content_seed[:16],
                }
            )
        ga4_rows = self._session.scalar(select(Ga4PageDaily.id).limit(1))
        return {
            "linked_publications": linked,
            "ga4_data_present": ga4_rows is not None,
            "join_key": "utm_source=threads / utm_medium=social / utm_campaign / utm_content",
            "limitations": [
                "GA4 の既定のレポートでは utm_content までは分解されないことがある。",
                "link_mode=none の投稿には UTM が無く、サイト側の帰属は **存在しない**。",
                "同じ記事へ他経路からも流入するため、UTM 一致は相関であって因果ではない。",
            ],
            "unavailable_reason": (
                None if linked else "no published post carries a website link yet"
            ),
        }

    # -- recommendations --------------------------------------------------------
    def _recommendations(self, per_post: list[dict], mature: list[dict]) -> list[dict]:
        """**控えめに。** 1 本しかないなら「まだ言えない」としか言わない。"""

        recs: list[dict] = []
        if not per_post:
            return [
                {
                    "type": REC_INSUFFICIENT_DATA,
                    "reason": "no Threads post has been published yet",
                    "action": "publish one approved proposal, then collect observations",
                }
            ]
        if len(mature) < self._policy.minimum_mature_posts:
            recs.append(
                {
                    "type": REC_COLLECT_MORE,
                    "reason": (
                        f"{len(mature)} post(s) have reached the comparison stage; "
                        f"{self._policy.minimum_mature_posts} are needed before comparing"
                    ),
                    "action": "keep publishing approved proposals and importing insights",
                }
            )
        immature = [p for p in per_post if not p["maturity"]["comparable"]]
        for post in immature:
            recs.append(
                {
                    "type": REC_INSUFFICIENT_DATA,
                    "publication_id": post["publication_id"],
                    "reason": (
                        f"publication {post['publication_id']} is {post['maturity']['stage']} "
                        f"({post['maturity']['age_hours']}h old); its numbers are not a verdict"
                    ),
                    "action": "observe again later; do not treat current values as performance",
                }
            )
        if len(mature) < self._policy.minimum_mature_posts:
            # ここで角度や長さの優劣を言い始めない。
            return recs

        angles = self._aggregate(mature, "angle")
        used = {a for a, data in angles.items() if data["sample"] > 0}
        unused = [a for a in POST_ANGLES if a not in used]
        if unused:
            recs.append(
                {
                    "type": REC_ANGLE_DIVERSIFY,
                    "reason": f"no mature post exists for: {', '.join(unused)}",
                    "action": "propose those angles so the comparison has something to compare",
                }
            )
        link_modes = self._aggregate(mature, "link_mode")
        if len(link_modes) < 2:
            recs.append(
                {
                    "type": REC_LINK_MODE_EXPERIMENT,
                    "reason": "every mature post uses the same link mode",
                    "action": "publish one of the other mode to see whether it reads differently",
                }
            )
        buckets = self._aggregate(mature, "length_bucket")
        if len(buckets) < 2:
            recs.append(
                {
                    "type": REC_LENGTH_EXPERIMENT,
                    "reason": "every mature post falls in the same length bucket",
                    "action": "try a shorter and a longer post before drawing length conclusions",
                }
            )
        return recs

    # -- internals -------------------------------------------------------------
    def _publications(self, publication_id: int | None) -> list[ThreadsPublication]:
        stmt = select(ThreadsPublication).where(
            ThreadsPublication.status == PUB_PUBLISHED,
            ThreadsPublication.threads_media_id.is_not(None),
        )
        if publication_id is not None:
            stmt = stmt.where(ThreadsPublication.id == publication_id)
        return list(self._session.scalars(stmt.order_by(ThreadsPublication.id)).all())

    def _age_hours(self, row: ThreadsPublication, now: datetime) -> float | None:
        if row.published_at is None:
            return None
        return (now - ensure_aware(row.published_at)).total_seconds() / 3600.0

    def _append(
        self,
        row: ThreadsPublication,
        now: datetime,
        *,
        values: dict,
        missing,
        outcome_value: str,
        error: ThreadsError | None = None,
    ) -> ThreadsInsightSnapshot | None:
        snapshot = ThreadsInsightSnapshot(
            threads_publication_id=row.id,
            threads_media_id=row.threads_media_id or "",
            observed_at=to_storage_utc(now),
            age_hours=self._age_hours(row, now),
            outcome=outcome_value,
            missing_json=list(missing) or None,
            error_category=error.category if error else None,
            error_message=error.reason if error else None,
        )
        # 観測できた指標だけを入れる。**欠測は NULL のまま。**
        for metric in MEDIA_METRICS:
            if metric in values and values[metric] is not None:
                setattr(snapshot, metric, int(values[metric]))
        self._session.add(snapshot)
        try:
            self._session.commit()
        except IntegrityError:
            # 同じ観測時刻の二重取り込み。
            self._session.rollback()
            return None
        self._session.refresh(snapshot)
        return snapshot

    def _latest_snapshot(self, publication_id: int) -> ThreadsInsightSnapshot | None:
        return self._session.scalars(
            select(ThreadsInsightSnapshot)
            .where(
                ThreadsInsightSnapshot.threads_publication_id == publication_id,
                ThreadsInsightSnapshot.outcome != SNAPSHOT_FAILED,
            )
            .order_by(ThreadsInsightSnapshot.observed_at.desc())
            .limit(1)
        ).first()

    def _last_observed_at(self, publication_id: int) -> datetime | None:
        """最後に観測を試みた時刻 (失敗も含む。失敗直後に連打しないため)。"""

        moment = self._session.scalars(
            select(ThreadsInsightSnapshot.observed_at)
            .where(ThreadsInsightSnapshot.threads_publication_id == publication_id)
            .order_by(ThreadsInsightSnapshot.observed_at.desc())
            .limit(1)
        ).first()
        return ensure_aware(moment) if moment is not None else None

    def _snapshot_count(self, publication_id: int) -> int:
        return len(
            self._session.scalars(
                select(ThreadsInsightSnapshot.id).where(
                    ThreadsInsightSnapshot.threads_publication_id == publication_id
                )
            ).all()
        )

    def _last_snapshot_summary(self, publication_id: int) -> dict | None:
        latest = self._latest_snapshot(publication_id)
        if latest is None:
            return None
        return {
            "snapshot_id": latest.id,
            "observed_at": _iso(latest.observed_at),
            "outcome": latest.outcome,
            "values": _snapshot_values(latest),
        }


def _snapshot_values(snapshot: ThreadsInsightSnapshot | None) -> dict:
    """観測値。**未観測は None のまま返す** (0 に丸めない)。"""

    if snapshot is None:
        return dict.fromkeys(MEDIA_METRICS)
    return {metric: getattr(snapshot, metric) for metric in MEDIA_METRICS}


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "RECOMMENDATION_TYPES",
    "ImportOutcome",
    "ThreadsInsightsError",
    "ThreadsInsightsService",
]
