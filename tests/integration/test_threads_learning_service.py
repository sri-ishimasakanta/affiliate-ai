"""Threads の学習の報告 (T5、DB から読むだけ)。

pin する契約:

- DB を 1 行も変えない。書こうとしたら止める。Threads には問い合わせない。
- 本番と同じ形 (若い 3 本) では「証拠が足りない」とだけ言い、所見も助言も出さない。
- 欠測 (NULL) は欠測のまま、0 は 0 のまま読む。
- 公開済みでない投稿 (不確定など) は学習に入れない。
- CLI は人が読む形と、決定的な JSON を出す。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    PUB_PUBLISHED,
    PUB_UNCERTAIN,
    SNAPSHOT_FAILED,
    SNAPSHOT_OBSERVED,
    TP_APPROVED,
    Article,
    Keyword,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_learning_service import ThreadsLearningError, ThreadsLearningService
from app.social.threads.learning import (
    EVIDENCE_COMPARABLE,
    EVIDENCE_IMMATURE,
    FINDING_OBSERVED_DIFFERENCE,
    SCHEMA_VERSION,
    STATUS_INSUFFICIENT_SAMPLE,
)

_BASE = "https://bizfluxlab.com"
#: 本番の報告の頃 (2026-09-25 13:10 JST)。
_NOW = datetime(2026, 9, 25, 4, 10, tzinfo=UTC)
_TEXT = "体制を先に決めたほうが早い。\n参照先と版を残しておくほうが更新しやすい。" * 4


@pytest.fixture
def article(session: Session) -> Article:
    keyword = Keyword(keyword="生成AI ガイドライン")
    session.add(keyword)
    session.flush()
    row = Article(
        id=21,
        title="生成AIガイドライン｜要点と実務上の注意点",
        slug="generative-ai-guidelines",
        body="記事本文。",
        status="published",
        published_url=f"{_BASE}/generative-ai-guidelines/",
        published_at=_NOW - timedelta(days=30),
        article_type="informational",
        keyword_id=keyword.id,
    )
    session.add(row)
    session.commit()
    return row


def _published(
    session: Session,
    article: Article,
    *,
    seed: str,
    published_at: datetime,
    angle="insight",
    trigger="automatic",
    status=PUB_PUBLISHED,
) -> ThreadsPublication:
    from app.article.draft_promotion_canonical import compute_text_hash

    proposal = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle=angle,
        link_mode="none",
        content_text=_TEXT,
        character_count=len(_TEXT),
        destination_url=None,
        content_seed=seed * 64,
        proposal_hash=seed.upper() * 64,
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=TP_APPROVED,
    )
    session.add(proposal)
    session.flush()
    row = ThreadsPublication(
        proposal_id=proposal.id,
        proposal_hash=proposal.proposal_hash,
        source_article_id=article.id,
        angle=angle,
        exact_published_text=_TEXT,
        threads_media_id=f"media-{seed}" if status == PUB_PUBLISHED else None,
        status=status,
        trigger=trigger,
        published_at=published_at if status == PUB_PUBLISHED else None,
    )
    session.add(row)
    session.commit()
    return row


def _observe(session, publication, age_hours, *, outcome=SNAPSHOT_OBSERVED, **metrics):
    row = ThreadsInsightSnapshot(
        threads_publication_id=publication.id,
        threads_media_id=publication.threads_media_id or "",
        observed_at=publication.published_at + timedelta(hours=age_hours),
        age_hours=age_hours,
        outcome=outcome,
        **metrics,
    )
    session.add(row)
    session.commit()
    return row


_ZEROS = {"views": 0, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0, "shares": 0}


def _production_like(session: Session, article: Article) -> list[ThreadsPublication]:
    """本番と同じ形: 手動 1 本 (約 34h)、自動 2 本 (約 3.5h と 0.4h)。"""

    first = _published(
        session, article, seed="a", published_at=_NOW - timedelta(hours=33.8), trigger="manual"
    )
    second = _published(
        session, article, seed="b", published_at=_NOW - timedelta(hours=3.5), angle="beginner_tip"
    )
    third = _published(session, article, seed="c", published_at=_NOW - timedelta(hours=0.4))
    _observe(session, first, 0.48, **{**_ZEROS, "views": 83})
    _observe(session, first, 27.1, outcome=SNAPSHOT_FAILED)
    _observe(session, first, 33.2, **{**_ZEROS, "views": 146})
    _observe(session, second, 2.96, **{**_ZEROS, "views": 16})
    _observe(session, third, 0.0, **_ZEROS)
    return [first, second, third]


def _counts(session: Session) -> tuple:
    return tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (ThreadsPublication, ThreadsInsightSnapshot, ThreadsPostProposal, Article)
    )


def _service(session: Session) -> ThreadsLearningService:
    return ThreadsLearningService(session)


# == the current production shape ==============================================
def test_the_production_shape_reports_insufficient_evidence(
    session: Session, article: Article
) -> None:
    _production_like(session, article)
    report = _service(session).report(now=_NOW)

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["counts"] == {
        "total": 3,
        "mature": 0,
        "immature": 3,
        "comparable": 0,
        "missing_comparable_data": 0,
        "awaiting_observation": 0,
        "no_comparable_observation": 0,
    }
    assert report["evidence_status"] == STATUS_INSUFFICIENT_SAMPLE
    assert report["findings"] == []
    assert report["comparable_dimensions"] == []
    for dimension in ("angle", "link_mode", "length_band", "hour", "daypart", "weekday"):
        for value in report["dimensions"][dimension]["values"]:
            assert value["status"] == STATUS_INSUFFICIENT_SAMPLE, (dimension, value["value"])
            assert value["interaction_rate"]["median"] is None
    assert {p["evidence_state"] for p in report["publications"]} == {EVIDENCE_IMMATURE}
    assert report["changes"]["material"] is False


def test_the_report_changes_nothing_and_calls_nothing(
    session: Session, article: Article, monkeypatch
) -> None:
    from app.social.threads import service as threads_service

    def _refuse(*_a, **_k):
        raise AssertionError("the learning report must not build a Threads client")

    monkeypatch.setattr(threads_service.ThreadsService, "__init__", _refuse)
    publications = _production_like(session, article)
    before = _counts(session)
    stamps = [(p.id, p.status, p.updated_at) for p in publications]

    report = _service(session).report(now=_NOW)

    assert _counts(session) == before
    session.expire_all()
    assert [
        (p.id, p.status, p.updated_at)
        for p in session.scalars(select(ThreadsPublication).order_by(ThreadsPublication.id))
    ] == stamps
    assert report["side_effects"] == {
        "database_writes": 0,
        "threads_calls": 0,
        "threads_writes": 0,
        "emails": 0,
        "wordpress_writes": 0,
    }


def test_a_write_during_the_report_is_refused(
    session: Session, article: Article, monkeypatch
) -> None:
    """学習の途中で誰かが flush / commit しようとしたら、そこで止める。"""

    _production_like(session, article)
    service = _service(session)
    original = service.facts

    def facts_then_write():
        session.add(Keyword(keyword="write attempted during the report"))
        session.commit()
        return original()

    monkeypatch.setattr(service, "facts", facts_then_write)
    with pytest.raises(ThreadsLearningError):
        service.report(now=_NOW)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(Keyword)) == 1


def test_unpublished_rows_are_not_learning_examples(session: Session, article: Article) -> None:
    _published(session, article, seed="u", published_at=_NOW, status=PUB_UNCERTAIN)
    assert _service(session).report(now=_NOW)["counts"]["total"] == 0


# == mature evidence from the database =========================================
def _mature(session, article, seed, hours_ago, angle, views, likes, **extra):
    row = _published(
        session, article, seed=seed, published_at=_NOW - timedelta(hours=hours_ago), angle=angle
    )
    _observe(session, row, 30, **{**_ZEROS, "views": views // 2})  # 若い時点の値は使わない
    _observe(session, row, 73, **{**_ZEROS, "views": views, "likes": likes, **extra})
    _observe(session, row, 200, **{**_ZEROS, "views": views * 50, "likes": 0})  # 窓の後
    return row


def test_mature_database_evidence_produces_a_described_difference(
    session: Session, article: Article
) -> None:
    for i, seed in enumerate("abc"):
        _mature(session, article, seed, 300 + i, "insight", 1000, 100)
    for i, seed in enumerate("def"):
        _mature(session, article, seed, 400 + i, "comparison", 1000, 50)
    report = _service(session).report(now=_NOW)

    assert report["counts"]["comparable"] == 6
    assert {p["snapshot"]["age_hours"] for p in report["publications"]} == {73}
    finding = next(f for f in report["findings"] if f["dimension"] == "angle")
    assert finding["kind"] == FINDING_OBSERVED_DIFFERENCE
    assert finding["higher"] == "insight"
    topic = report["dimensions"]["topic"]["values"]
    assert [v["value"] for v in topic] == ["生成AI ガイドライン"]
    assert report["dimensions"]["source_article"]["values"][0]["label"] == article.title


def test_null_metrics_stay_missing_and_zero_stays_zero(session: Session, article: Article) -> None:
    missing = _published(session, article, seed="m", published_at=_NOW - timedelta(hours=300))
    _observe(session, missing, 73, likes=0, replies=0, reposts=0, quotes=0, shares=0)  # views NULL
    zero = _published(session, article, seed="z", published_at=_NOW - timedelta(hours=301))
    _observe(session, zero, 73, **_ZEROS)
    report = _service(session).report(now=_NOW)

    posts = {p["publication_id"]: p for p in report["publications"]}
    assert posts[missing.id]["evidence_state"] == EVIDENCE_COMPARABLE
    assert posts[missing.id]["metrics"]["views"] is None
    assert posts[missing.id]["missing_metrics"] == ["views"]
    assert posts[zero.id]["metrics"]["views"] == 0
    assert report["overall"]["missing"]["views"] == 1
    assert report["overall"]["zero_views"] == 1


# == CLI =======================================================================
def _factory(session: Session):
    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    return _Scoped()


def test_the_cli_prints_an_honest_report_and_deterministic_json(
    session: Session, article: Article, tmp_path, capsys
) -> None:
    from scripts.report_threads_learning import main

    _production_like(session, article)
    before = _counts(session)
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    for path in paths:
        assert main(["--json", str(path)], session_factory=_factory(session), now=_NOW) == 0
    out = capsys.readouterr().out

    assert "analysis time    = 2026-09-25T13:10+09:00 (Asia/Tokyo)" in out
    assert "publications     = total 3 / mature 0 / immature 3 / missing comparable data 0" in out
    assert "overall evidence = insufficient_sample" in out
    assert "--- findings ---\n  (none)" in out
    assert "nothing is recommended" in out
    assert "--- trigger (diagnostic only; never compared) ---" in out
    assert "no material change" in out
    assert "read-only: database writes = 0, Threads calls = 0, emails = 0" in out
    assert paths[0].read_text(encoding="utf-8") == paths[1].read_text(encoding="utf-8")
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    assert payload["evidence_status"] == STATUS_INSUFFICIENT_SAMPLE
    assert _counts(session) == before
    for word in ("best", "winner", "optimal", "guarantee"):
        assert word not in out.lower()


# == T5.1: as-of reproducibility from the database =============================
def test_an_as_of_report_is_byte_identical_after_newer_data_arrives(
    session: Session, article: Article, tmp_path, capsys
) -> None:
    from scripts.report_threads_learning import main, to_json

    # 実時計から遠い時刻で固定する (成熟は as_of で判定され、実時計は見ない)。
    as_of = datetime(2031, 5, 10, 3, 0, tzinfo=UTC)
    rows = [
        _published(
            session, article, seed=seed, published_at=as_of - timedelta(hours=hours), angle=angle
        )
        for seed, hours, angle in (
            ("a", 300, "insight"),
            ("b", 301, "insight"),
            ("c", 302, "insight"),
            ("d", 303, "comparison"),
            ("e", 80, "comparison"),  # as_of の時点で窓が開いている
        )
    ]
    for row in rows[:4]:
        _observe(session, row, 73, **{**_ZEROS, "views": 1000, "likes": 40})
    _observe(session, rows[4], 73, likes=0, replies=0, reposts=0, quotes=0, shares=0)

    first = to_json(_service(session).report(now=as_of))
    assert (
        main(
            ["--as-of", as_of.isoformat(), "--json", str(tmp_path / "1.json")],
            session_factory=_factory(session),
        )
        == 0
    )

    # as_of の後に観測・公開されたもの (窓の中の後の観測を含む)。
    for row in rows[:4]:
        _observe(session, row, 400, **{**_ZEROS, "views": 10**6, "likes": 0})
    _observe(session, rows[4], 90, **{**_ZEROS, "views": 9000, "likes": 3000})
    late = _published(session, article, seed="f", published_at=as_of + timedelta(hours=1))
    _observe(session, late, 0.5, **{**_ZEROS, "views": 10**7})

    assert to_json(_service(session).report(now=as_of)) == first
    assert (
        main(
            ["--as-of", as_of.isoformat(), "--json", str(tmp_path / "2.json")],
            session_factory=_factory(session),
        )
        == 0
    )
    assert (tmp_path / "1.json").read_text(encoding="utf-8") == (tmp_path / "2.json").read_text(
        encoding="utf-8"
    )
    capsys.readouterr()

    # 後の時点では、後の証拠が見える (窓が閉じた e は 90h の観測で例になる)。
    later = _service(session).report(now=as_of + timedelta(hours=30))
    row_e = next(p for p in later["publications"] if p["publication_id"] == rows[4].id)
    assert row_e["evidence_state"] == EVIDENCE_COMPARABLE
    assert row_e["metrics"]["views"] == 9000


def test_the_collector_stamps_observations_with_the_collection_time(
    session: Session, article: Article
) -> None:
    """as-of の再現性の前提: 観測は取り込んだ時刻で積まれ、過去の時刻では差し込まれない。"""

    from app.services.threads_insights_service import ThreadsInsightsService
    from app.social.threads.models import ThreadsInsights

    class _Reader:
        def media_insights(self, media_id, metrics=None):
            return ThreadsInsights(subject=f"media:{media_id}", values={"views": 5}, missing=())

    row = _published(session, article, seed="k", published_at=_NOW - timedelta(hours=80))

    class _Settings:
        threads_enabled = True

    collected_at = _NOW + timedelta(minutes=7)
    ThreadsInsightsService(session, settings=_Settings(), threads_service=_Reader()).collect(
        publication_id=row.id, execute=True, now=collected_at
    )
    stored = session.scalars(select(ThreadsInsightSnapshot)).one()
    assert stored.observed_at.replace(tzinfo=UTC) == collected_at
