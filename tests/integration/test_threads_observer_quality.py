"""T6.5B.1: 観察の質の分析の層 (run 1 と同じ形の古い記録を書き換えずに扱う)。

手元の DB (メモリの SQLite) と偽のページだけ。Threads には触れない。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import ThreadsExternalObservation, ThreadsExternalPost, ThreadsObserverRun
from app.models.threads_observer import RUN_ACCOUNTING_MISMATCH, RUN_SUCCEEDED
from app.services.threads_observer_service import record_run
from app.services.threads_trend_analysis_service import build_report, external_post_rows
from app.social.threads.features import extract
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import CollectionPlan, collect
from scripts import analyze_threads_trends
from tests.support.threads_observer_pages import FakePage, card, page

T0 = datetime(2026, 9, 28, 13, 10, tzinfo=UTC)
LEGACY_COLLECTOR = "t6.5b-collector-2"
VERIFIED = "threads-web-verified-2026-09-28-v1"


def _clock(start: datetime):
    moments = iter(start + timedelta(seconds=i) for i in range(1000))
    return lambda: next(moments)


def _legacy_run(session: Session, bodies: dict[str, tuple[str, int | None, int | None]]):
    """run 1 と同じ形の古い記録 (collector-2・候補の勘定の記録なし・本文に画面の部品)。"""

    run = ThreadsObserverRun(
        started_at=T0, finished_at=T0, status=RUN_SUCCEEDED, source_types_json=["for_you"],
        item_limit=5, items_collected=len(bodies), items_rejected=0,
        collector_version=LEGACY_COLLECTOR, selector_version=VERIFIED,
        artifacts_json={"screenshots": {}, "pages_opened": 1, "scrolls": 1},
    )  # fmt: skip
    session.add(run)
    session.flush()
    for key, (body, likes, replies) in bodies.items():
        code = key.split(":", 1)[1]
        post = ThreadsExternalPost(
            external_post_key=key, author_handle=f"author_{code}",
            permalink=f"https://www.threads.com/@author_{code}/post/{code}", body_text=body,
            body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            features_json=extract(body).as_dict(), feature_version="t6.5-features-1",
            media_type="none", has_link=False, first_seen_at=T0, last_seen_at=T0,
        )  # fmt: skip
        session.add(post)
        session.flush()
        session.add(ThreadsExternalObservation(
            post_id=post.id, run_id=run.id, observed_at=T0, source_type="for_you",
            likes=likes, replies=replies, body_hash=post.body_hash,
            extraction_status="complete", collector_version=LEGACY_COLLECTOR,
            selector_version=VERIFIED,
        ))  # fmt: skip
    session.commit()
    return run


def _snapshot(session: Session) -> tuple[list, list, list]:
    session.expire_all()
    posts = [(p.body_text, p.body_hash, json.dumps(p.features_json, sort_keys=True))
             for p in session.scalars(
                 select(ThreadsExternalPost).order_by(ThreadsExternalPost.id))]  # fmt: skip
    obs = [(o.likes, o.replies, o.reposts, o.body_hash) for o in session.scalars(
        select(ThreadsExternalObservation).order_by(ThreadsExternalObservation.id))]  # fmt: skip
    runs = [(r.status, json.dumps(r.artifacts_json, sort_keys=True)) for r in session.scalars(
        select(ThreadsObserverRun).order_by(ThreadsObserverRun.id))]  # fmt: skip
    return posts, obs, runs


def test_run1_rows_are_normalized_for_analysis_without_rewriting(session: Session) -> None:
    _legacy_run(session, {
        "threads:L1": ("noteで収益を出すぞ！！ 1/2", 2, 1),
        "threads:L2": ("meta.ai 今現在、どんな投稿が伸びる？", 1, 1),
        "threads:L3": ("こんばんは", None, None),
        "threads:L4": ("meta.ai ", 1013, 121),
    })  # fmt: skip
    before = _snapshot(session)
    report = build_report(session, now=T0)
    ext = report["external"]
    rows = {r["external_post_key"]: r for r in external_post_rows(session)}

    marker = rows["threads:L1"]
    assert marker["text"]["normalization_flags"] == ["thread_marker_removed"]
    assert marker["text"]["normalization_version"] == "threads-body-normalizer-1"
    assert marker["text"]["raw_body_hash"] != marker["text"]["normalized_body_hash"]
    assert marker["analysis_features"]["numeric_facts_count"] == 0
    assert extract("noteで収益を出すぞ！！ 1/2").numeric_facts_count == 2  # 保存した特徴
    assert "ui_chrome_removed" in marker["quality_flags"]
    assert rows["threads:L2"]["text"]["normalization_flags"] == ["meta_ai_label_removed"]
    assert rows["threads:L2"]["body_excerpt"].startswith("今現在")
    assert rows["threads:L3"]["text"]["text_quality"] == "text_clean"
    assert "partial_metrics" in rows["threads:L3"]["quality_flags"]

    # 本文が部品だけ → 意味の数え上げからは外す。いいね・返信の分析には残る。
    contaminated = rows["threads:L4"]
    assert contaminated["text"]["text_quality"] == "text_contaminated"
    assert contaminated["features"] is None
    assert ext["excluded_from_patterns"] == 1
    assert ext["high_likes"][0]["external_post_key"] == "threads:L4"
    assert ext["high_replies"][0]["replies"] == 121
    assert sum(ext["patterns"]["cta_class"]["all"].values()) == 3
    for row in rows.values():
        assert "candidate_accounting_not_guaranteed" in row["quality_flags"]
        assert {"metrics_verified", "selector_verified"} <= set(row["quality_flags"])

    [quality] = ext["run_quality"]
    assert quality == {"run_id": 1, "collection_status": "succeeded",
                       "collector_version": LEGACY_COLLECTOR, "selector_version": VERIFIED,
                       "post_collection_quality": "partial", "text_quality": "contaminated",
                       "candidate_completeness": "not_guaranteed", "posts": 4}  # fmt: skip
    # 元の記録 (本文・hash・保存した特徴・指標・実行の状態) は変わらない。
    assert _snapshot(session) == before


def test_the_run1_shape_reads_as_normalized_with_known_ui_chrome(session: Session) -> None:
    _legacy_run(session, {"threads:M1": ("本文 1/2", 2, 1),
                          "threads:M2": ("meta.ai 本文2", 1, 1),
                          "threads:M3": ("本文3", 1, None)})  # fmt: skip
    [quality] = build_report(session, now=T0)["external"]["run_quality"]
    assert quality["collection_status"] == "succeeded"  # 元の状態はそのまま
    assert quality["post_collection_quality"] == "partial"
    assert quality["text_quality"] == "normalized_with_known_ui_chrome"
    assert quality["candidate_completeness"] == "not_guaranteed"


def test_a_new_run_stores_complete_candidate_accounting(session: Session) -> None:
    fake = FakePage({sel.for_you_url(): page(*[card("w", f"W{i}", f"本文{i}") for i in range(7)])})
    run = record_run(session, collect(fake, CollectionPlan(for_you=True),
                                      limits={"for_you": 5}, clock=_clock(T0)))  # fmt: skip
    accounting = run.artifacts_json["candidate_accounting"]
    assert accounting["candidate_cards"] == 7
    assert accounting["complete"] is True and accounting["equality_holds"] is True
    assert accounting["by_reason"] == {"accepted": 5, "filtered_over_limit": 2}
    assert run.collector_version == sel.COLLECTOR_VERSION == "t6.5b-collector-5"
    [quality] = build_report(session, now=T0)["external"]["run_quality"]
    assert quality["candidate_completeness"] == "complete"
    assert quality["text_quality"] == "clean"
    assert quality["post_collection_quality"] == "complete"


def test_a_thread_marker_card_is_stored_clean(session: Session) -> None:
    fake = FakePage({sel.for_you_url(): page(card("t", "T1", "本気出す！！", likes="2",
                                                  thread_marker="1/2"))})  # fmt: skip
    record_run(session, collect(fake, CollectionPlan(for_you=True), clock=_clock(T0)))
    post = session.scalars(select(ThreadsExternalPost)).one()
    assert post.body_text == "本気出す！！"
    assert post.features_json["numeric_facts_count"] == 0
    [row] = external_post_rows(session)
    assert row["text"]["text_quality"] == "text_clean"


def test_an_accounting_mismatch_run_stores_no_posts(session: Session) -> None:
    hidden = ('<div><a href="/@x/post/HIDDEN1"><time datetime="2026-09-27T01:00:00Z">1日</time>'
              "</a></div>")  # fmt: skip
    fake = FakePage({sel.for_you_url(): page(card("k", "K1", "本文", inner=hidden))})
    run = record_run(session, collect(fake, CollectionPlan(for_you=True), clock=_clock(T0)))
    assert run.status == RUN_ACCOUNTING_MISMATCH
    assert run.artifacts_json["candidate_accounting"]["complete"] is False
    assert session.scalars(select(ThreadsExternalPost)).all() == []
    assert session.scalars(select(ThreadsExternalObservation)).all() == []


def test_tiny_external_samples_say_evidence_is_insufficient(session: Session, capsys) -> None:
    fake = FakePage({sel.for_you_url(): page(card("y", "Y1", "本文", likes="3"))})
    record_run(session, collect(fake, CollectionPlan(for_you=True), clock=_clock(T0)))
    evidence = build_report(session, now=T0)["external"]["evidence"]
    assert evidence["status"] == "insufficient_evidence"
    assert evidence["sample_size"] == 1 and evidence["authors_with_baseline"] == 0
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    analyze_threads_trends.main([], session_factory=factory, now=T0)
    out = capsys.readouterr().out
    assert "外部投稿の観測数が少ないため、現時点では傾向を判断できません。" in out
    assert "伸びる要因を示す証拠ではありません。" in out
    assert "標本: 1 件" in out  # 数は隠さない

