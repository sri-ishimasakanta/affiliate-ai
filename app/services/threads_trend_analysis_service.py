"""外の観察と自分の投稿の記述 (T6.5A-B、**読むだけ・DB に書かない**)。

- 外の投稿ごとの **最新の観測** を使う (観測の回数・推移も出す)。
- 伸びた候補 (breakout) は、同じ投稿者のほかの投稿の中央値と比べる (``trends``)。
- いいね・返信の多い投稿、繰り返し出るトピック、特徴の形の数え上げ。
- 自分の投稿の基準 (``threads_feature_store``)。
- **提案にも生成にも戻さない。** 「〜だから伸びた」とは言わない。

T6.5B.1: 本文は **保存したまま** (``raw_body``) を証拠として残し、分析には決定的に部品を除いた
``analysis_body`` (``observer.normalize``) から作り直した特徴を使う。部品が残る本文
(``text_contaminated``) は、特徴の形の数え上げから外す (いいね・返信の数の分析には使う)。
実行ごとの質 (候補の勘定がそろっているか・本文の質) も出す。記録そのものは書き換えない。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import (
    ThreadsExternalObservation,
    ThreadsExternalPost,
    ThreadsObserverRun,
    ThreadsTrendingTopic,
)
from app.services.threads_feature_store import own_baselines, own_feature_rows
from app.social.threads.features import extract
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.normalize import (
    TEXT_CLEAN,
    TEXT_CONTAMINATED,
    TEXT_UI_CHROME_REMOVED,
    normalize_body,
)
from app.social.threads.trends import (
    BREAKOUT_CANDIDATE,
    DESCRIPTIVE_NOTE,
    MIN_AUTHOR_SAMPLE,
    MIN_GROUP_SAMPLE,
    author_breakouts,
)

SCHEMA_VERSION = "threads-trend-intelligence/1"
EXTERNAL_METRICS = ("likes", "replies", "reposts", "shares")
PATTERN_FEATURES = ("cta_class", "structure_class", "char_bucket", "media_type", "has_url")
TOP_N = 10
JST = ZoneInfo("Asia/Tokyo")
#: 外の投稿の傾向を「言える」と表示する最小の本数 (表示の目安。原因の証拠の基準ではない)。
EXTERNAL_MIN_POSTS_FOR_PATTERNS = 30
EVIDENCE_INSUFFICIENT = "insufficient_evidence"
EVIDENCE_DESCRIPTIVE = "descriptive_only"
INSUFFICIENT_EVIDENCE_JA = (
    "外部投稿の観測数が少ないため、現時点では傾向を判断できません。"
    "以下は観測値の一覧であり、伸びる要因を示す証拠ではありません。"
)
#: 質の印。
FLAG_METRICS_VERIFIED = "metrics_verified"
FLAG_METRICS_UNVERIFIED = "metrics_unverified"
FLAG_PARTIAL_METRICS = "partial_metrics"
FLAG_SELECTOR_VERIFIED = "selector_verified"
FLAG_ACCOUNTING_COMPLETE = "candidate_accounting_complete"
FLAG_ACCOUNTING_NOT_GUARANTEED = "candidate_accounting_not_guaranteed"


def _verified_selector(version: str | None) -> bool:
    return bool(version) and version.startswith("threads-web-verified")


def _accounting_complete(run: ThreadsObserverRun | None) -> bool:
    accounting = ((run.artifacts_json or {}) if run else {}).get("candidate_accounting") or {}
    return accounting.get("complete") is True and accounting.get("equality_holds") is True


def run_quality(session: Session, rows: list[dict]) -> list[dict]:
    """実行ごとの質 (元の状態は書き換えない。分析の層での見方)。"""

    out = []
    for run in session.scalars(select(ThreadsObserverRun).order_by(ThreadsObserverRun.id)):
        texts = [r["text"]["text_quality"] for r in rows if run.id in r["run_ids"]]
        if TEXT_CONTAMINATED in texts:
            text_quality = "contaminated"
        elif TEXT_UI_CHROME_REMOVED in texts:
            text_quality = "normalized_with_known_ui_chrome"
        else:
            text_quality = "clean" if texts else "no_posts"
        complete = _accounting_complete(run)
        good = complete and run.status == "succeeded" and text_quality in ("clean", "no_posts")
        out.append({
            "run_id": run.id,
            "collection_status": run.status,
            "collector_version": run.collector_version,
            "selector_version": run.selector_version,
            "post_collection_quality": "complete" if good else "partial",
            "text_quality": text_quality,
            "candidate_completeness": "complete" if complete else "not_guaranteed",
            "posts": len(texts),
        })  # fmt: skip
    return out


def external_post_rows(session: Session) -> list[dict]:
    """外の投稿ごとの 1 行 (最新の観測の指標と、観測の回数)。"""

    observations: dict[int, list[ThreadsExternalObservation]] = defaultdict(list)
    for obs in session.scalars(
        select(ThreadsExternalObservation).order_by(
            ThreadsExternalObservation.observed_at, ThreadsExternalObservation.id
        )
    ):
        observations[obs.post_id].append(obs)
    runs = {r.id: r for r in session.scalars(select(ThreadsObserverRun))}
    rows = []
    for post in session.scalars(select(ThreadsExternalPost).order_by(ThreadsExternalPost.id)):
        history = observations.get(post.id, [])
        last = history[-1] if history else None
        raw_features = post.features_json or {}
        text = normalize_body(post.body_text or "")
        posted = ensure_aware(post.post_timestamp) if post.post_timestamp else None
        analysis = extract(text.analysis_body, topic=post.topic, media_type=post.media_type,
                           posted_at=posted).as_dict()  # fmt: skip
        flags = [text.text_quality]
        verified = last is not None and _verified_selector(last.selector_version)
        flags.append(FLAG_METRICS_VERIFIED if verified else FLAG_METRICS_UNVERIFIED)
        if verified:
            flags.append(FLAG_SELECTOR_VERIFIED)
        if last is None or last.likes is None or last.replies is None:
            flags.append(FLAG_PARTIAL_METRICS)
        run_ids = sorted({o.run_id for o in history})
        complete = bool(run_ids) and all(_accounting_complete(runs.get(i)) for i in run_ids)
        flags.append(FLAG_ACCOUNTING_COMPLETE if complete else FLAG_ACCOUNTING_NOT_GUARANTEED)
        usable_text = text.text_quality in (TEXT_CLEAN, TEXT_UI_CHROME_REMOVED)
        rows.append(
            {
                "external_post_key": post.external_post_key,
                "author_handle": post.author_handle,
                "permalink": post.permalink,
                "topic": post.topic,
                "post_timestamp": _iso(post.post_timestamp),
                "snapshots": len(history),
                "sources": sorted({o.source_type for o in history}),
                "body_changed": len({o.body_hash for o in history if o.body_hash}) > 1,
                "observed_at": _iso(last.observed_at) if last else None,
                "observed_at_jst": (
                    ensure_aware(last.observed_at).astimezone(JST).isoformat() if last else None
                ),
                **{m: getattr(last, m) if last else None for m in EXTERNAL_METRICS},
                "likes_history": [o.likes for o in history],
                "run_ids": run_ids,
                # 分析用の本文から作り直した特徴 (部品が残る本文なら None = 数え上げに使わない)。
                "features": (
                    {k: analysis.get(k) for k in PATTERN_FEATURES} if usable_text else None
                ),
                "analysis_features": analysis if usable_text else None,
                # 集めたときに保存した特徴 (証拠。比べるために残す)。
                "raw_features": {k: raw_features.get(k) for k in PATTERN_FEATURES},
                "text": text.as_dict(),
                "quality_flags": flags,
                "body_excerpt": text.analysis_body[:80],
            }
        )
    return rows


def _top(rows: list[dict], metric: str) -> list[dict]:
    ranked = sorted((r for r in rows if r.get(metric) is not None),
                    key=lambda r: (-r[metric], r["external_post_key"]))  # fmt: skip
    return [
        {k: r[k] for k in ("external_post_key", "author_handle", "topic", metric, "snapshots",
                           "body_excerpt")}
        for r in ranked[:TOP_N]
    ]  # fmt: skip


def _patterns(rows: list[dict], keys: set[str]) -> dict:
    """特徴ごとの数 (全体 / 伸びた候補)。数え上げだけ。部品が残る本文は数えない。"""

    rows = [r for r in rows if r["features"] is not None]
    out = {}
    for feature in PATTERN_FEATURES:
        everyone = Counter(str(r["features"].get(feature)) for r in rows)
        chosen = Counter(
            str(r["features"].get(feature)) for r in rows if r["external_post_key"] in keys
        )
        out[feature] = {"all": dict(sorted(everyone.items())),
                        "candidate_breakouts": dict(sorted(chosen.items()))}  # fmt: skip
    return out


def observer_tables_present(session: Session) -> bool:
    """観察の表があるか (migration ``2cfa0ccb2059`` の前の DB では無い)。"""

    from sqlalchemy import inspect

    names = set(inspect(session.get_bind()).get_table_names())
    return {"threads_observer_runs", "threads_external_posts",
            "threads_external_observations", "threads_trending_topics"} <= names  # fmt: skip


def _evidence(rows: list[dict], authors: Counter, min_author_sample: int) -> dict:
    """外の観測で傾向を言えるか。少なければ「判断できない」と明示する (値は隠さない)。"""

    usable = sum(1 for r in rows if r["features"] is not None)
    with_baseline = sum(1 for n in authors.values() if n - 1 >= min_author_sample)
    insufficient = usable < EXTERNAL_MIN_POSTS_FOR_PATTERNS or with_baseline == 0
    return {
        "status": EVIDENCE_INSUFFICIENT if insufficient else EVIDENCE_DESCRIPTIVE,
        "sample_size": len(rows),
        "usable_for_patterns": usable,
        "min_posts_for_patterns": EXTERNAL_MIN_POSTS_FOR_PATTERNS,
        "authors_with_baseline": with_baseline,
        "message_ja": INSUFFICIENT_EVIDENCE_JA if insufficient else None,
    }


def build_report(
    session: Session,
    *,
    now: datetime | None = None,
    min_author_sample: int = MIN_AUTHOR_SAMPLE,
    min_group_sample: int = MIN_GROUP_SAMPLE,
) -> dict:
    now = now or datetime.now(UTC)
    if not observer_tables_present(session):
        own_rows = own_feature_rows(session)
        return {
            "schema_version": SCHEMA_VERSION,
            "as_of": now.isoformat(),
            "read_only": True,
            "fed_back_to_generation": False,
            "external": {"available": False,
                         "reason": "observer tables are not in this database (migration "
                                   "2cfa0ccb2059 is not applied here)"},
            "own": own_baselines(own_rows, min_sample=min_group_sample),
            "limitations": [DESCRIPTIVE_NOTE],
        }  # fmt: skip
    runs = dict(
        session.execute(
            select(ThreadsObserverRun.status, func.count()).group_by(ThreadsObserverRun.status)
        ).all()
    )
    rows = external_post_rows(session)
    breakouts = author_breakouts(rows, min_sample=min_author_sample)
    candidates = [
        b for b in breakouts
        if BREAKOUT_CANDIDATE in (b["likes_breakout"], b["replies_breakout"])
    ]  # fmt: skip
    candidates.sort(key=lambda b: -(b["likes_breakout_ratio"] or b["replies_breakout_ratio"] or 0))
    authors = Counter(r["author_handle"] for r in rows if r["author_handle"])
    topics = [
        {"topic": t.topic_name, "observations": t.observations,
         "sample_post_count": t.sample_post_count, "last_seen_at": _iso(t.last_seen_at)}
        for t in session.scalars(
            select(ThreadsTrendingTopic).order_by(
                ThreadsTrendingTopic.observations.desc(), ThreadsTrendingTopic.topic_name
            )
        )
    ]  # fmt: skip
    post_topics = Counter(r["topic"] for r in rows if r["topic"])
    own_rows = own_feature_rows(session)
    limitations = [
        DESCRIPTIVE_NOTE,
        "external views/impressions are not visible and are never estimated",
        "likes/replies are cumulative at the time of observation; posts of different ages "
        "are compared as observed (no age alignment for external posts yet)",
        f"author baseline needs >= {min_author_sample} other observed posts by the same author",
        f"selector version {sel.SELECTOR_VERSION} (verified={sel.SELECTOR_VERIFIED})",
    ]
    if not rows:
        limitations.append("no external observations yet (the manual read-only pilot is next)")
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": now.isoformat(),
        "read_only": True,
        "fed_back_to_generation": False,
        "external": {
            "available": True,
            "runs_by_status": dict(sorted(runs.items())),
            "posts": len(rows),
            "observations": sum(r["snapshots"] for r in rows),
            "posts_with_repeated_snapshots": sum(1 for r in rows if r["snapshots"] > 1),
            "authors": len(authors),
            "authors_with_baseline": sum(1 for n in authors.values() if n - 1 >= min_author_sample),
            "candidate_breakouts": candidates[:TOP_N],
            "high_likes": _top(rows, "likes"),
            "high_replies": _top(rows, "replies"),
            "trending_topics": topics,
            "repeated_trending_topics": [t for t in topics if t["observations"] >= 2],
            "post_topics": dict(post_topics.most_common(TOP_N)),
            "patterns": _patterns(rows, {c["external_post_key"] for c in candidates}),
            "text_quality": dict(sorted(Counter(r["text"]["text_quality"] for r in rows).items())),
            "excluded_from_patterns": sum(1 for r in rows if r["features"] is None),
            "run_quality": run_quality(session, rows),
            "evidence": _evidence(rows, authors, min_author_sample),
        },
        "own": own_baselines(own_rows, min_sample=min_group_sample),
        "limitations": limitations,
    }


def _iso(moment: datetime | None) -> str | None:
    return ensure_aware(moment).isoformat() if moment else None


__all__ = ["EXTERNAL_MIN_POSTS_FOR_PATTERNS", "INSUFFICIENT_EVIDENCE_JA", "SCHEMA_VERSION",
           "build_report", "external_post_rows", "observer_tables_present", "run_quality"]
