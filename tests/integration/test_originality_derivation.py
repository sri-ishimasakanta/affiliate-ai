"""KeywordSignalService.derive_originality の検証 (独立した in-memory DB)。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.exceptions import EntityNotFoundError
from app.models import Article, Keyword
from app.models.enums import KeywordSignalComponent
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_signal_service import KeywordSignalService

_FORBIDDEN_BODY = "本文には SUPER_SECRET_BODY と https://wp.example.test/secret が含まれる"


def _kw(session: Session, text: str, status: str = "discovered") -> Keyword:
    entity = Keyword(keyword=text)
    entity.status = status
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _art(
    session: Session,
    *,
    slug: str,
    title: str,
    status: str,
    keyword_id: int | None,
    body: str | None = None,
    meta: str | None = None,
    url: str | None = None,
) -> Article:
    entity = Article(title=title, slug=slug, keyword_id=keyword_id)
    entity.status = status
    entity.body = body
    entity.meta_description = meta
    entity.published_url = url
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def test_empty_corpus(session: Session) -> None:
    keyword = _kw(session, "AI 議事録 おすすめ")
    before = datetime.now(UTC).replace(tzinfo=None)
    read = KeywordSignalService(session).derive_originality(keyword.id)
    after = datetime.now(UTC).replace(tzinfo=None)

    assert read.component == KeywordSignalComponent.ORIGINALITY
    assert read.provider == "internal_corpus"
    assert read.source_reference == "internal-corpus:v1"
    assert read.period_start is None and read.period_end is None
    assert before <= _naive(read.observed_at) <= after
    assert read.normalized_value == 100.0

    raw = read.raw_data
    assert raw["corpus_available"] is False
    assert raw["evidence_coverage"] == 0.0
    assert raw["candidates_count"] == 0
    assert raw["max_similarity"] == 0.0
    assert raw["self_article_exists"] is False
    assert raw["self_excluded_keyword_id"] == keyword.id
    assert raw["similarity_method"] == "char_bigram_dice|sequencematcher_max"
    assert raw["ngram_size"] == 2
    assert raw["title_evidence_weight"] == 0.80
    assert raw["intent_adjustment_applied"] is False
    assert raw["normalizer"] == {"name": "originality", "version": "v1"}


def test_exact_duplicate_against_other_keyword(session: Session) -> None:
    _kw(session, "AI 議事録 おすすめ", status="selected")
    current = _kw(session, "AI 議事録 おすすめ dup", status="discovered")
    # normalize 後に完全一致させるため、比較対象を current と同一 normalize 結果へ
    twin = Keyword(keyword="  AI  議事録  おすすめ dup ")
    twin.status = "analyzed"
    session.add(twin)
    session.flush()
    session.commit()

    read = KeywordSignalService(session).derive_originality(current.id)
    assert read.normalized_value == 0.0
    assert read.raw_data["max_similarity"] == 1.0
    assert read.raw_data["most_similar_kind"] == "keyword"
    assert read.raw_data["most_similar_keyword_id"] == twin.id
    assert read.raw_data["most_similar_keyword_text"] == twin.keyword
    assert read.raw_data["most_similar_article_id"] is None


def test_status_filtering_excludes_discovered_and_rejected(session: Session) -> None:
    current = _kw(session, "AI 議事録 おすすめ")
    _kw(session, "AI 議事録 おすすめ 近い", status="discovered")  # 除外
    _kw(session, "AI 議事録 おすすめ 却下", status="rejected")   # 除外
    session.commit()

    read = KeywordSignalService(session).derive_originality(current.id)
    assert read.raw_data["corpus_available"] is False
    assert read.raw_data["keyword_candidates_count"] == 0


def test_article_title_candidate_weighting(session: Session) -> None:
    current = _kw(session, "AI 議事録 完全ガイド")
    _art(
        session,
        slug="a1",
        title="AI 議事録 完全ガイド",
        status="published",
        keyword_id=None,  # linked keyword 無し -> title candidate のみ
    )
    session.commit()

    read = KeywordSignalService(session).derive_originality(current.id)
    raw = read.raw_data
    assert raw["article_title_candidates_count"] == 1
    assert raw["article_keyword_candidates_count"] == 0
    assert raw["most_similar_kind"] == "article_title"
    assert raw["most_similar_article_title"] == "AI 議事録 完全ガイド"
    assert raw["most_similar_keyword_text"] is None
    # title 完全一致 -> effective 0.8 -> originality 20.0
    assert read.normalized_value == 20.0


def test_article_keyword_candidate(session: Session) -> None:
    current = _kw(session, "AI 議事録 おすすめ dup")
    # linked keyword は discovered (keyword candidate からは除外される) だが、
    # それに紐づく published 記事があるので article_keyword candidate としては入る。
    linked = _kw(session, "  AI 議事録  おすすめ dup ", status="discovered")
    _art(session, slug="a1", title="別物タイトル xyz", status="approved", keyword_id=linked.id)
    session.commit()

    read = KeywordSignalService(session).derive_originality(current.id)
    raw = read.raw_data
    assert raw["keyword_candidates_count"] == 0  # discovered は keyword candidate 外
    assert raw["article_keyword_candidates_count"] == 1
    assert raw["most_similar_kind"] == "article_keyword"
    assert raw["most_similar_keyword_id"] == linked.id
    assert read.normalized_value == 0.0  # linked keyword text が正規化後完全一致


def test_duplicate_text_prefers_keyword_kind_over_article_keyword(session: Session) -> None:
    current = _kw(session, "AI 議事録 おすすめ dup")
    linked = _kw(session, "  AI 議事録  おすすめ dup ", status="analyzed")  # keyword candidate
    _art(session, slug="a1", title="別物 xyz", status="published", keyword_id=linked.id)
    session.commit()

    raw = KeywordSignalService(session).derive_originality(current.id).raw_data
    # 同じ text が keyword / article_keyword 両方に現れる -> keyword kind を優先
    assert raw["keyword_candidates_count"] == 1
    assert raw["article_keyword_candidates_count"] == 1
    assert raw["most_similar_kind"] == "keyword"
    assert raw["most_similar_keyword_id"] == linked.id


def test_self_article_excluded_but_flagged(session: Session) -> None:
    current = _kw(session, "AI 議事録 おすすめ")
    _art(
        session,
        slug="self",
        title="AI 議事録 おすすめ",  # 完全一致だが self なので除外
        status="published",
        keyword_id=current.id,
        body=_FORBIDDEN_BODY,
    )
    session.commit()

    read = KeywordSignalService(session).derive_originality(current.id)
    assert read.raw_data["self_article_exists"] is True
    assert read.raw_data["candidates_count"] == 0  # self article は候補にしない
    assert read.normalized_value == 100.0


def test_raw_data_has_no_body_meta_or_url(session: Session) -> None:
    current = _kw(session, "AI 議事録 おすすめ")
    other = _kw(session, "AI 文字起こし ツール", status="selected")
    _art(
        session,
        slug="a1",
        title="AI 議事録 おすすめ 記事",
        status="published",
        keyword_id=other.id,
        body=_FORBIDDEN_BODY,
        meta="META_SECRET_DESCRIPTION",
        url="https://wp.example.test/secret-post",
    )
    session.commit()

    read = KeywordSignalService(session).derive_originality(current.id)
    blob = repr(read.raw_data)
    assert "SUPER_SECRET_BODY" not in blob
    assert "META_SECRET_DESCRIPTION" not in blob
    assert "wp.example.test" not in blob
    assert "published_url" not in blob
    assert "meta_description" not in blob
    assert "body" not in blob


def test_persist_and_immutable_history(session: Session) -> None:
    keyword = _kw(session, "AI 議事録 おすすめ")
    service = KeywordSignalService(session)

    first = service.derive_originality(keyword.id)
    session.rollback()
    assert KeywordSignalRepository(session).get_by_id(first.id) is not None

    second = service.derive_originality(keyword.id)
    assert first.id != second.id
    history = KeywordSignalRepository(session).list_by_component(
        keyword.id, "originality"
    )
    assert len(history) == 2
    latest = KeywordSignalRepository(session).get_latest(keyword.id, "originality")
    assert latest.id == second.id


def test_keyword_not_found(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        KeywordSignalService(session).derive_originality(999999)


def test_commit_failure_rolls_back(session: Session, monkeypatch) -> None:
    keyword = _kw(session, "AI 議事録 おすすめ")
    service = KeywordSignalService(session)

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        service.derive_originality(keyword.id)

    monkeypatch.undo()
    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_corpus_totals_in_raw_data(session: Session) -> None:
    current = _kw(session, "AI 議事録 おすすめ")
    _kw(session, "他 kw 1", status="analyzed")
    _kw(session, "他 kw 2", status="discovered")  # total には数えるが candidate 外
    other = _kw(session, "他 kw 3", status="selected")
    _art(session, slug="a1", title="記事 1", status="published", keyword_id=other.id)
    _art(session, slug="a2", title="記事 2", status="idea", keyword_id=None)
    session.commit()

    raw = KeywordSignalService(session).derive_originality(current.id).raw_data
    assert raw["keyword_total"] == 4
    assert raw["article_total"] == 2
    assert raw["corpus_size_total"] == 6
    assert raw["keyword_candidates_count"] == 2  # analyzed + selected (discovered 除外)
