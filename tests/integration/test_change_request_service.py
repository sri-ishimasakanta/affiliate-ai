"""ChangeRequestService の統合テスト (C9.1)。

pin する契約:

- 候補は **自動では** 提案にならない。候補 id を明示したときだけ作られる。
- 提案は作成時に凍結され、承認はその 1 つの ``proposal_hash`` に結び付く。
- 提案が作り直されたら、古い承認は新しい提案に移らない。
- 承認だけでは記事も WordPress も変わらない。
- 候補が消えても request は自動で取り消されない (判断は人に残す)。
- 却下には理由が必須。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    CR_APPROVED,
    CR_AWAITING_APPROVAL,
    CR_REJECTED,
    Article,
    ChangeRequest,
    ChangeRequestApproval,
    SeoImprovementCandidate,
    SeoImprovementRun,
)
from app.services.change_request_service import ChangeRequestError, ChangeRequestService

_BASE = "https://example.com"
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

_SOURCE_BODY = """# RPA 導入

導入の段落です。ここでは手順を整理します。

## 事前準備

参考: [公式](https://official.example.jp/docs)
"""


def _article(session: Session, article_id: int, slug: str, *, body: str) -> Article:
    article = Article(
        id=article_id,
        title=f"記事{article_id}",
        slug=slug,
        body=body,
        status="published",
        published_url=f"{_BASE}/{slug}/",
        published_at=_NOW - timedelta(days=30),
        article_type="informational",
        monetization_mode="supporting",
        wordpress_post_id=str(1000 + article_id),
    )
    session.add(article)
    session.commit()
    return article


def _candidate(
    session: Session,
    *,
    article_id: int,
    target_article_id: int,
    dedupe_key: str = "internal-link:18->17",
    candidate_type: str = "INTERNAL_LINK_OPPORTUNITY",
) -> SeoImprovementCandidate:
    run = session.scalars(select(SeoImprovementRun).limit(1)).first()
    if run is None:
        run = SeoImprovementRun(
            policy_version="seo-policy-1",
            window_start=date(2026, 5, 1),
            window_end=date(2026, 5, 31),
            evaluated_article_count=2,
            candidate_count=1,
        )
        session.add(run)
        session.commit()
    candidate = SeoImprovementCandidate(
        seo_improvement_run_id=run.id,
        article_id=article_id,
        candidate_type=candidate_type,
        reason_code="DEFERRED_PAIR",
        priority="low",
        evidence_strength="structural",
        suggested_action="内部リンクを 1 本足す",
        evidence_json={"target_article_id": target_article_id, "relation": "deferred pair"},
        dedupe_key=dedupe_key,
    )
    session.add(candidate)
    session.commit()
    return candidate


@pytest.fixture
def pair(session: Session):
    source = _article(session, 18, "rpa-implementation", body=_SOURCE_BODY)
    target = _article(session, 17, "rpa-comparison", body="# 比較\n\n本文\n")
    candidate = _candidate(session, article_id=source.id, target_article_id=target.id)
    return source, target, candidate


def test_proposal_is_created_only_for_an_explicit_candidate(session: Session, pair) -> None:
    _, _, candidate = pair
    service = ChangeRequestService(session)

    # 何もしなければ提案は存在しない。
    assert session.scalars(select(ChangeRequest)).all() == []

    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)

    assert request.status == CR_AWAITING_APPROVAL
    assert len(session.scalars(select(ChangeRequest)).all()) == 1


def test_proposal_freezes_its_content_and_candidate_link(session: Session, pair) -> None:
    source, target, candidate = pair
    request = ChangeRequestService(session).propose_from_seo_candidate(
        candidate_id=candidate.id, now=_NOW
    )

    assert request.source_engine == "seo"
    assert request.source_candidate_id == candidate.id
    assert request.source_candidate_dedupe_key == candidate.dedupe_key
    assert request.source_candidate_priority == "low"
    assert request.source_policy_version == "seo-policy-1"
    assert request.expected_source_body_hash == compute_text_hash(source.body)
    assert request.proposed_body_hash == compute_text_hash(request.proposed_body)
    assert target.published_url in request.proposed_body


def test_proposing_the_same_thing_twice_returns_the_same_request(session: Session, pair) -> None:
    _, _, candidate = pair
    service = ChangeRequestService(session)

    first = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)
    second = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)

    assert first.id == second.id
    assert len(session.scalars(select(ChangeRequest)).all()) == 1


def test_approval_requires_the_exact_proposal_hash(session: Session, pair) -> None:
    _, _, candidate = pair
    service = ChangeRequestService(session)
    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)

    with pytest.raises(ChangeRequestError, match="proposal hash mismatch"):
        service.approve(request.id, proposal_hash="0" * 64, now=_NOW)

    assert session.get(ChangeRequest, request.id).status == CR_AWAITING_APPROVAL


def test_approval_does_not_change_the_article(session: Session, pair) -> None:
    source, _, candidate = pair
    before = source.body
    service = ChangeRequestService(session)
    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)

    service.approve(request.id, proposal_hash=request.proposal_hash, now=_NOW)

    session.refresh(source)
    assert source.body == before
    assert session.get(ChangeRequest, request.id).status == CR_APPROVED


def test_an_old_approval_does_not_migrate_to_a_revised_proposal(session: Session, pair) -> None:
    source, _, candidate = pair
    service = ChangeRequestService(session)
    first = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)
    service.approve(first.id, proposal_hash=first.proposal_hash, now=_NOW)
    approved_hash = first.proposal_hash

    # 記事が編集されたので、同じ候補から別内容の提案が生まれる。
    source.body = source.body.replace("手順を整理します", "手順と費用を整理します")
    session.commit()
    second = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)

    assert second.id != first.id
    assert second.proposal_hash != approved_hash
    assert second.proposal_version == first.proposal_version + 1
    assert service.latest_approval(second) is None


def test_staleness_detects_a_changed_source_body(session: Session, pair) -> None:
    source, _, candidate = pair
    service = ChangeRequestService(session)
    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)

    assert service.evaluate_staleness(request).stale is False

    source.body = source.body + "\n追記\n"
    session.commit()
    report = service.evaluate_staleness(request)

    assert report.stale
    assert any("body changed" in reason for reason in report.reasons)


def test_staleness_detects_a_link_added_by_someone_else(session: Session, pair) -> None:
    source, target, candidate = pair
    service = ChangeRequestService(session)
    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)

    source.body = f"{source.body}\n[手動で追加]({target.published_url})\n"
    session.commit()
    report = service.evaluate_staleness(request)

    assert report.stale
    assert any("already links" in reason for reason in report.reasons)


def test_a_disappearing_candidate_does_not_cancel_the_request(session: Session, pair) -> None:
    _, _, candidate = pair
    service = ChangeRequestService(session)
    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)

    # 新しい評価 run では候補が出なかった。
    session.add(
        SeoImprovementRun(
            policy_version="seo-policy-1",
            window_start=date(2026, 6, 1),
            window_end=date(2026, 6, 30),
            evaluated_article_count=2,
            candidate_count=0,
        )
    )
    session.commit()

    assert service.candidate_currently_present(request) is False
    assert session.get(ChangeRequest, request.id).status == CR_AWAITING_APPROVAL
    # 候補の不在は staleness の理由にならない。
    assert service.evaluate_staleness(request).stale is False


def test_rejection_requires_a_reason(session: Session, pair) -> None:
    _, _, candidate = pair
    service = ChangeRequestService(session)
    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)

    with pytest.raises(ChangeRequestError, match="reason is required"):
        service.reject(request.id, reason="  ", now=_NOW)

    service.reject(request.id, reason="リンク先が弱い", now=_NOW)
    assert session.get(ChangeRequest, request.id).status == CR_REJECTED


def test_a_rejected_request_cannot_be_approved(session: Session, pair) -> None:
    _, _, candidate = pair
    service = ChangeRequestService(session)
    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)
    service.reject(request.id, reason="不要", now=_NOW)

    with pytest.raises(ChangeRequestError):
        service.approve(request.id, proposal_hash=request.proposal_hash, now=_NOW)


def test_approval_history_is_append_only(session: Session, pair) -> None:
    _, _, candidate = pair
    service = ChangeRequestService(session)
    request = service.propose_from_seo_candidate(candidate_id=candidate.id, now=_NOW)
    service.approve(request.id, proposal_hash=request.proposal_hash, now=_NOW)
    service.reject(request.id, reason="やはり見送る", now=_NOW)

    rows = session.scalars(select(ChangeRequestApproval).order_by(ChangeRequestApproval.id)).all()
    assert [row.decision for row in rows] == ["approved", "rejected"]
    assert all(row.approved_proposal_hash == request.proposal_hash for row in rows)


def test_only_add_internal_link_is_generated_in_v1(session: Session, pair) -> None:
    _, _, candidate = pair

    with pytest.raises(ChangeRequestError, match="not generated in V1"):
        ChangeRequestService(session).propose_from_seo_candidate(
            candidate_id=candidate.id, change_type="text_edit", now=_NOW
        )


def test_a_non_internal_link_candidate_cannot_produce_a_proposal(session: Session, pair) -> None:
    source, target, _ = pair
    other = _candidate(
        session,
        article_id=source.id,
        target_article_id=target.id,
        dedupe_key="ctr:18",
        candidate_type="CTR_IMPROVEMENT",
    )

    with pytest.raises(ChangeRequestError, match="cannot produce an internal-link proposal"):
        ChangeRequestService(session).propose_from_seo_candidate(candidate_id=other.id, now=_NOW)


def test_duplicate_link_is_refused_at_proposal_time(session: Session, pair) -> None:
    source, target, candidate = pair
    source.body = f"{source.body}\n[既出]({target.published_url})\n"
    session.commit()

    with pytest.raises(ChangeRequestError, match="LINK_ALREADY_PRESENT"):
        ChangeRequestService(session).propose_from_seo_candidate(
            candidate_id=candidate.id, now=_NOW
        )
