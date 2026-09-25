"""学習の参考を入れた Threads 投稿案の生成 (T5.5、DB と CLI)。

pin する契約:

- 参考は T5 の学習サービスから作る。本番と同じ形 (若い 3 本) では中立。
- 参考は検査 (errors) を変えない。事実・長さの制約を学習が上書きしない。
- 保存した提案には小さな来歴 (適用したか・指紋・as_of・証拠の状態) が残る。
- prompt と違う参考での保存は拒否する。
- 既存の提案・承認には一切触れない。保存は常に awaiting_approval。
- as_of より後のデータは参考を変えない。
- 来歴の列が無い (migration 前の) DB でも読む処理は動き、保存は何も書かずに止まる。
- Threads にもメールにも触れない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    PUB_PUBLISHED,
    SNAPSHOT_OBSERVED,
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    Article,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_proposal_service import ThreadsProposalError, ThreadsProposalService
from app.social.threads.guidance import MODE_NEUTRAL, MODE_WEAK, build_guidance
from app.social.threads.proposal import LINK_MODES, LINK_PLACEHOLDER

_BASE = "https://bizfluxlab.com"
_NOW = datetime(2026, 12, 1, 3, 0, tzinfo=UTC)
_BODY = "AIガイドラインの記事本文。\n体制を先に決めるほうが早い。\n## 手順\n"
_SUPPORTED = {
    "angle": ("insight", "common_mistake", "comparison", "question", "beginner_tip"),
    "link_mode": LINK_MODES,
    "length_band": ("short", "medium", "long"),
}


@pytest.fixture(autouse=True)
def _no_threads_client(monkeypatch):
    from app.social.threads import service as threads_service

    def _refuse(*_a, **_k):
        raise AssertionError("T5.5 must not build a Threads client")

    monkeypatch.setattr(threads_service.ThreadsService, "__init__", _refuse)


@pytest.fixture
def article(session: Session) -> Article:
    row = Article(
        id=21,
        title="生成AIガイドライン",
        slug="generative-ai-guidelines",
        body=_BODY,
        status="published",
        published_url=f"{_BASE}/generative-ai-guidelines/",
        published_at=_NOW - timedelta(days=90),
        article_type="informational",
    )
    session.add(row)
    session.commit()
    return row


def _generated(*items) -> str:
    return json.dumps({"proposals": list(items)}, ensure_ascii=False)


_TWO = _generated(
    {"angle": "insight", "link_mode": "none", "body": "体制を先に決めたほうが早い。"},
    {
        "angle": "question",
        "link_mode": "article",
        "body": f"AIのルール、どこから作った？\n体制からが早い。\n{LINK_PLACEHOLDER}",
    },
)


def _history(session: Session, article: Article, *, seed, hours_ago, rate, chars, link="none"):
    """成熟した過去の投稿 1 本 (73h の観測つき)。"""

    text_ = "あ" * chars
    proposal = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle="insight",
        link_mode=link,
        content_text=text_,
        character_count=chars,
        content_seed=seed * 64,
        proposal_hash=seed.upper() * 64,
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=TP_APPROVED,
    )
    session.add(proposal)
    session.flush()
    published = _NOW - timedelta(hours=hours_ago)
    row = ThreadsPublication(
        proposal_id=proposal.id,
        proposal_hash=proposal.proposal_hash,
        source_article_id=article.id,
        angle="insight",
        exact_published_text=text_,
        threads_media_id=f"m-{seed}",
        status=PUB_PUBLISHED,
        published_at=published,
    )
    session.add(row)
    session.flush()
    session.add(
        ThreadsInsightSnapshot(
            threads_publication_id=row.id,
            threads_media_id=row.threads_media_id,
            observed_at=published + timedelta(hours=73),
            age_hours=73,
            outcome=SNAPSHOT_OBSERVED,
            views=1000,
            likes=round(rate * 1000),
            replies=0,
            reposts=0,
            quotes=0,
            shares=0,
        )
    )
    session.commit()
    return row


def _medium_beats_long(session, article):
    for i, seed in enumerate("abc"):
        _history(session, article, seed=seed, hours_ago=500 + i, rate=0.10, chars=200)
    for i, seed in enumerate("def"):
        _history(session, article, seed=seed, hours_ago=600 + i, rate=0.05, chars=400)


def _service(session) -> ThreadsProposalService:
    return ThreadsProposalService(session)


# == neutral (the current production shape) =====================================
def test_with_no_mature_history_the_prompt_and_plan_are_neutral(
    session: Session, article: Article
) -> None:
    service = _service(session)
    package = service.build_prompt(article_id=article.id, learning_as_of=_NOW)
    assert package.guidance.mode == MODE_NEUTRAL
    assert "十分な証拠のある学習はまだ無い" in package.rendered_prompt
    assert package.as_dict()["learning_guidance"]["evidence_status"] == "insufficient_sample"

    prepared = service.plan(article_id=article.id, generated_output=_TWO, learning_as_of=_NOW)
    assert prepared.learning["mode"] == MODE_NEUTRAL
    assert prepared.learning["preferences"] == []
    assert prepared.acceptable == 2
    assert session.scalar(select(func.count()).select_from(ThreadsPostProposal)) == 0


# == sufficient synthetic evidence ==============================================
def test_sufficient_evidence_becomes_weak_guidance_in_the_prompt(
    session: Session, article: Article
) -> None:
    _medium_beats_long(session, article)
    package = _service(session).build_prompt(article_id=article.id, learning_as_of=_NOW)
    assert package.guidance.mode == MODE_WEAK
    assert "長さ medium (151〜300 文字) を、ほんの少しだけ優先してよい" in package.rendered_prompt
    # 依頼する切り口は減らない (多様性)。
    assert package.angles == tuple(_SUPPORTED["angle"])


def test_guidance_never_changes_what_validation_accepts_or_rejects(
    session: Session, article: Article
) -> None:
    outputs = _generated(
        {"angle": "insight", "link_mode": "none", "body": "体制を先に決めたほうが早い。"},
        {
            "angle": "question",
            "link_mode": "article",
            "body": f"AIのルール、どこから作った？\n{LINK_PLACEHOLDER}",
        },
        {"angle": "comparison", "link_mode": "none", "body": "あ" * 501},  # 長さの上限を超える
    )
    neutral = _service(session).plan(
        article_id=article.id, generated_output=outputs, learning_as_of=_NOW
    )

    _medium_beats_long(session, article)
    weak = _service(session).plan(
        article_id=article.id, generated_output=outputs, learning_as_of=_NOW
    )
    assert weak.learning["mode"] == MODE_WEAK
    strip = lambda p: ([c["proposal_hash"] for c in p.candidates], p.rejected)  # noqa: E731
    assert strip(weak) == strip(neutral)
    # 学習が「長い投稿」を好んでも、上限超えは拒否のまま (事実・文体の制約が上)。
    assert any("comparison" == r["angle"] for r in weak.rejected)


def test_a_preferred_link_mode_never_removes_article_links(
    session: Session, article: Article
) -> None:
    for i, seed in enumerate("abc"):
        _history(session, article, seed=seed, hours_ago=500 + i, rate=0.10, chars=200)
    for i, seed in enumerate("def"):
        _history(
            session, article, seed=seed, hours_ago=600 + i, rate=0.05, chars=200, link="article"
        )
    prepared = _service(session).plan(
        article_id=article.id, generated_output=_TWO, learning_as_of=_NOW
    )
    prefs = {(p["value"], p["direction"]) for p in prepared.learning["preferences"]}
    assert ("none", "prefer") in prefs
    assert {c["link_mode"] for c in prepared.candidates} == {"none", "article"}


def test_a_batch_collapsed_onto_the_preferred_value_is_flagged_not_rejected(
    session: Session, article: Article
) -> None:
    _medium_beats_long(session, article)
    medium = "体制を先に決めたほうが早い。" * 12  # 約 170 文字 (medium)
    outputs = _generated(
        {"angle": "insight", "link_mode": "none", "body": medium},
        {"angle": "question", "link_mode": "none", "body": medium + "どう？"},
    )
    prepared = _service(session).plan(
        article_id=article.id, generated_output=outputs, learning_as_of=_NOW
    )
    assert prepared.acceptable == 2
    assert any("length_band=medium" in n for n in prepared.learning["diversity_notes"])


# == provenance and approval authority ==========================================
def test_persist_records_small_provenance_and_stays_awaiting_approval(
    session: Session, article: Article
) -> None:
    _medium_beats_long(session, article)
    service = _service(session)
    fingerprint = service.build_prompt(
        article_id=article.id, learning_as_of=_NOW
    ).guidance.fingerprint
    rows = service.persist(
        article_id=article.id,
        generated_output=_TWO,
        now=_NOW,
        learning_as_of=_NOW,
        expected_guidance=fingerprint,
    )
    assert {r.status for r in rows} == {TP_AWAITING_APPROVAL}
    assert all(r.approved_at is None for r in rows)
    provenance = rows[0].learning_guidance_json
    assert provenance["applied"] is True
    assert provenance["fingerprint"] == fingerprint
    assert provenance["as_of"] == _NOW.isoformat()
    assert provenance["learning_policy_version"] == "t5.0"
    assert provenance["evidence_status"] == "sufficient_evidence"
    assert provenance["verified_against_prompt"] is True
    assert {"dimension": "length_band", "value": "medium", "direction": "prefer"} in provenance[
        "preferences"
    ]


def test_a_different_guidance_than_the_prompt_is_refused(
    session: Session, article: Article
) -> None:
    service = _service(session)
    neutral = service.build_prompt(article_id=article.id, learning_as_of=_NOW).guidance.fingerprint
    _medium_beats_long(session, article)
    with pytest.raises(ThreadsProposalError, match="differs from the prompt"):
        service.persist(
            article_id=article.id,
            generated_output=_TWO,
            now=_NOW,
            learning_as_of=_NOW,
            expected_guidance=neutral,
        )
    session.rollback()
    assert (
        session.scalar(
            select(func.count())
            .select_from(ThreadsPostProposal)
            .where(ThreadsPostProposal.status == TP_AWAITING_APPROVAL)
        )
        == 0
    )


def test_existing_proposals_and_approvals_are_never_touched(
    session: Session, article: Article
) -> None:
    _medium_beats_long(session, article)
    approved = session.scalars(select(ThreadsPostProposal).order_by(ThreadsPostProposal.id)).all()
    approved[-1].approved_at = _NOW - timedelta(hours=1)
    approved[-1].expires_at = _NOW + timedelta(days=1)
    session.commit()
    session.expire_all()  # DB に保存された値で比べる
    before = [
        (r.id, r.status, r.approved_at, r.expires_at, r.held_at, r.updated_at, r.content_text)
        for r in approved
    ]
    service = _service(session)
    service.build_prompt(article_id=article.id, learning_as_of=_NOW)
    service.plan(article_id=article.id, generated_output=_TWO, learning_as_of=_NOW)
    service.persist(article_id=article.id, generated_output=_TWO, now=_NOW)
    session.expire_all()
    after = [
        (r.id, r.status, r.approved_at, r.expires_at, r.held_at, r.updated_at, r.content_text)
        for r in session.scalars(
            select(ThreadsPostProposal)
            .where(ThreadsPostProposal.id.in_([r[0] for r in before]))
            .order_by(ThreadsPostProposal.id)
        )
    ]
    assert after == before


# == as_of ======================================================================
def test_guidance_at_an_as_of_is_unchanged_by_later_data(
    session: Session, article: Article
) -> None:
    _medium_beats_long(session, article)
    service = _service(session)
    before = service.learning_guidance(as_of=_NOW)
    # as_of の後: 新しい観測と、後に公開された投稿。
    for row in session.scalars(select(ThreadsPublication)).all():
        session.add(
            ThreadsInsightSnapshot(
                threads_publication_id=row.id,
                threads_media_id=row.threads_media_id,
                observed_at=_NOW + timedelta(hours=2),
                outcome=SNAPSHOT_OBSERVED,
                views=10**6,
                likes=0,
                replies=0,
                reposts=0,
                quotes=0,
                shares=0,
            )
        )
    session.commit()
    for i, seed in enumerate("ghijkl"):
        _history(session, article, seed=seed, hours_ago=-(1 + i), rate=0.9, chars=400)
    after = service.learning_guidance(as_of=_NOW)
    assert after.fingerprint == before.fingerprint
    assert json.dumps(after.as_dict()) == json.dumps(before.as_dict())


def test_the_service_uses_the_t5_report_rather_than_its_own_statistics(
    session: Session, article: Article
) -> None:
    from app.services.threads_learning_service import ThreadsLearningService
    from app.social.threads.policy import get_measurement_policy

    _medium_beats_long(session, article)
    report = ThreadsLearningService(session).report(now=_NOW)
    bands = {"short": (1, 150), "medium": (151, 300), "long": (301, 500)}
    expected = build_guidance(report, supported_values=_SUPPORTED, length_bands=bands)
    assert _service(session).learning_guidance(as_of=_NOW).fingerprint == expected.fingerprint
    assert get_measurement_policy().policy_version == expected.source_policy_version


# == migration safety ===========================================================
def test_before_the_migration_reads_work_and_persist_stores_nothing(
    session: Session, article: Article
) -> None:
    session.execute(text("ALTER TABLE threads_post_proposals DROP COLUMN learning_guidance_json"))
    session.commit()
    _medium_beats_long(session, article)  # 提案の行を読んで書く既存の経路は動く
    assert len(session.scalars(select(ThreadsPostProposal)).all()) == 6
    service = _service(session)
    assert service.plan(article_id=article.id, generated_output=_TWO, learning_as_of=_NOW)
    with pytest.raises(ThreadsProposalError, match="alembic upgrade head"):
        service.persist(article_id=article.id, generated_output=_TWO, now=_NOW)
    assert session.scalar(select(func.count()).select_from(ThreadsPostProposal)) == 6


# == CLI ========================================================================
def _factory(session: Session):
    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    return _Scoped()


def test_the_cli_shows_learning_in_prompt_and_plan(
    session: Session, article: Article, tmp_path, capsys
) -> None:
    from scripts.propose_threads_posts import main

    as_of = _NOW.isoformat()
    assert (
        main(
            ["--article-id", "21", "--print-prompt", "--learning-as-of", as_of],
            session_factory=_factory(session),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "learning policy      = t5.0 (threads-learning/1)" in out
    assert "learning evidence    = insufficient_sample" in out
    assert "guidance applied     = neutral" in out
    assert "angle: neutral (insufficient evidence" in out
    fingerprint = out.split("--guidance-fingerprint ")[1].split()[0]
    assert len(fingerprint) == 64

    generated = tmp_path / "out.json"
    generated.write_text(_TWO, encoding="utf-8")
    args = ["--article-id", "21", "--input", str(generated), "--learning-as-of", as_of]
    assert (
        main([*args, "--guidance-fingerprint", fingerprint], session_factory=_factory(session)) == 0
    )
    plan = capsys.readouterr().out
    assert "=== threads post proposals (PLAN) ===" in plan
    assert "guidance applied     = neutral" in plan
    assert "guidance verified    = True" in plan
    assert session.scalar(select(func.count()).select_from(ThreadsPostProposal)) == 0

    assert main([*args, "--guidance-fingerprint", "0" * 64], session_factory=_factory(session)) == 2
    assert "differs from the prompt" in capsys.readouterr().out
