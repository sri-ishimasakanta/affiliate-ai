"""ArticleReferenceFact: 製品ではない一次情報を記事の根拠として扱う経路。

比較対象を 1 件も持たない解説記事が、ガイドライン等の参照文献だけで
frozen prompt に根拠を載せられることを検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.draft_prompt_package import EditorialOverridesV1, build_prompt_package
from app.article.draft_prompt_render import render_prompt
from app.article.planning import ArticleType
from app.article.schemas import SourceCreate
from app.exceptions import EntityNotFoundError, FactValidationError
from app.models import Article, ArticleReferenceFact
from app.services.article_reference_fact_service import ArticleReferenceFactService
from app.services.draft_input_snapshot_builder import DraftInputSnapshotBuilder
from app.services.fact_pack_service import FactPackService
from app.services.source_service import SourceService
from tests.integration.test_content_production_foundations import (  # noqa: F401
    _approve,
    _catalog,
    _keyword,
)

NOW = datetime(2026, 9, 22, tzinfo=UTC)
_URL = "https://www.soumu.go.jp/main_sosiki/kenkyu/ai_network/02ryutsu20_04000019.html"


def _article(session: Session) -> int:
    _catalog(session)
    read = _approve(
        session, "AI ガバナンス", slug="ai-governance-ref",
        monetization_mode="supporting", article_type=ArticleType.INFORMATIONAL,
    )
    return read.id


def _source(session: Session, article_id: int) -> int:
    return SourceService(session).create(
        article_id,
        SourceCreate(source_type="official_docs", source_url=_URL,
                     title="AI事業者ガイドライン 掲載ページ", checked_at=NOW),
    ).id


def test_reference_evidence_needs_no_product_subject(session: Session) -> None:
    aid = _article(session)
    sid = _source(session, aid)
    svc = ArticleReferenceFactService(session)
    svc.create(aid, source_id=sid, reference_key="guideline_version",
               statement="第1.2版が令和8年3月31日に公表されている。",
               section_label="掲載ページ", position=0)
    svc.create(aid, source_id=sid, reference_key="subject_categories",
               statement="AI開発者・AI提供者・AI利用者の3主体に整理されている。",
               position=1)

    rows = svc.list_for_article(aid)
    assert [r.reference_key for r in rows] == ["guideline_version", "subject_categories"]
    assert all(r.source_id == sid for r in rows)

    pack = FactPackService(session).build(aid, now=NOW)
    assert pack.readiness.comparison_subject_count == 0
    assert pack.readiness.reference_evidence_count == 2
    assert pack.readiness.drafting_allowed is True


def test_snapshot_and_prompt_carry_reference_evidence(session: Session) -> None:
    aid = _article(session)
    sid = _source(session, aid)
    ArticleReferenceFactService(session).create(
        aid, source_id=sid, reference_key="basic_principles",
        statement="基本理念として Dignity / Diversity and Inclusion / Sustainability の"
                  "3つが示されている。",
        section_label="第2部 A. 基本理念", position=0)

    payload = DraftInputSnapshotBuilder(session).build(aid, now=NOW).payload
    evidence = payload["reference_evidence"]
    assert [e["reference_key"] for e in evidence] == ["basic_principles"]
    assert evidence[0]["section_label"] == "第2部 A. 基本理念"
    # 出典は referenced-source union に載る
    assert [s["source_url"] for s in payload["sources"]] == [_URL]

    pkg = build_prompt_package(
        snapshot_payload=payload, snapshot_id=1, snapshot_content_hash="x" * 64,
        overrides=EditorialOverridesV1(primary=None, comparison_set_size=0), now=NOW)
    assert pkg["reference_evidence"][0]["source"]["source_url"] == _URL
    assert pkg["reference_evidence"][0]["source"]["title"] == "AI事業者ガイドライン 掲載ページ"

    prompt = render_prompt(pkg)
    assert "基本理念" in prompt
    assert "[参照文献エビデンスの扱い]" in prompt
    # 拘束力の誤解を招かないよう明示的に禁じている
    assert "法的義務" in prompt


def test_articles_without_reference_evidence_keep_their_exact_payload(
    session: Session,
) -> None:
    """参照文献を持たない記事の payload は C4.6 導入前と同じ形のまま。"""
    _catalog(session)
    read = _approve(
        session, "RPA おすすめ", slug="rpa-no-ref", monetization_mode="supporting",
        article_type=ArticleType.RECOMMENDATION_ROUNDUP,
        content_subject_keys=["uipath", "winactor"],
    )
    payload = DraftInputSnapshotBuilder(session).build(read.id, now=NOW).payload
    assert "reference_evidence" not in payload

    pkg = build_prompt_package(
        snapshot_payload=payload, snapshot_id=1, snapshot_content_hash="x" * 64,
        overrides=EditorialOverridesV1(primary=None, comparison_set_size=2), now=NOW)
    assert "reference_evidence" not in pkg
    assert "[参照文献エビデンスの扱い]" not in render_prompt(pkg)


def test_evidence_requires_a_source_from_the_same_article(session: Session) -> None:
    a = _article(session)
    _catalog(session)
    other = _approve(
        session, "生成AI ガイドライン", slug="gen-ai-guidelines-ref",
        monetization_mode="supporting", article_type=ArticleType.INFORMATIONAL,
    ).id
    foreign = _source(session, other)

    with pytest.raises(FactValidationError) as e:
        ArticleReferenceFactService(session).create(
            a, source_id=foreign, reference_key="k", statement="s")
    assert "belongs to article" in str(e.value)
    assert session.scalar(select(func.count()).select_from(ArticleReferenceFact)) == 0


def test_blank_and_oversized_statements_are_rejected(session: Session) -> None:
    aid = _article(session)
    sid = _source(session, aid)
    svc = ArticleReferenceFactService(session)
    with pytest.raises(FactValidationError):
        svc.create(aid, source_id=sid, reference_key="k", statement="   ")
    with pytest.raises(FactValidationError):
        svc.create(aid, source_id=sid, reference_key="", statement="s")
    with pytest.raises(FactValidationError) as e:
        svc.create(aid, source_id=sid, reference_key="k", statement="あ" * 2001)
    assert "too long" in str(e.value)
    assert session.scalar(select(func.count()).select_from(ArticleReferenceFact)) == 0


def test_identical_evidence_is_not_duplicated(session: Session) -> None:
    aid = _article(session)
    sid = _source(session, aid)
    svc = ArticleReferenceFactService(session)
    first = svc.create(aid, source_id=sid, reference_key="k", statement="同じ記述")
    again = svc.create(aid, source_id=sid, reference_key="k", statement="同じ記述")
    assert again.id == first.id
    assert session.scalar(select(func.count()).select_from(ArticleReferenceFact)) == 1


def test_updating_a_key_appends_and_latest_wins(session: Session) -> None:
    aid = _article(session)
    sid = _source(session, aid)
    svc = ArticleReferenceFactService(session)
    svc.create(aid, source_id=sid, reference_key="version", statement="第1.1版")
    svc.create(aid, source_id=sid, reference_key="version", statement="第1.2版")

    assert session.scalar(select(func.count()).select_from(ArticleReferenceFact)) == 2
    rows = svc.list_for_article(aid)
    assert [r.statement for r in rows] == ["第1.2版"]


def test_unknown_article_or_source_raises(session: Session) -> None:
    aid = _article(session)
    sid = _source(session, aid)
    svc = ArticleReferenceFactService(session)
    with pytest.raises(EntityNotFoundError):
        svc.create(999_999, source_id=sid, reference_key="k", statement="s")
    with pytest.raises(EntityNotFoundError):
        svc.create(aid, source_id=999_999, reference_key="k", statement="s")


def test_article_delete_cascades_reference_facts(session: Session) -> None:
    aid = _article(session)
    sid = _source(session, aid)
    ArticleReferenceFactService(session).create(
        aid, source_id=sid, reference_key="k", statement="s")
    session.delete(session.get(Article, aid))
    session.commit()
    assert session.scalar(select(func.count()).select_from(ArticleReferenceFact)) == 0
