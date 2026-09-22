"""ArticlePlanService.plan_for_keyword の検証 (独立した in-memory DB、read-only)。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.planning import ArticleType
from app.exceptions import EntityNotFoundError
from app.models import AffiliateProgram, Article, Keyword, KeywordScore, KeywordSignal
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.article_plan_service import ArticlePlanService

_ALL7 = (
    "search_demand",
    "commercial_intent",
    "affiliate_opportunity",
    "competition_ease",
    "trend",
    "originality",
    "site_relevance",
)


def _keyword(session: Session, text: str, *, status: str = "analyzed",
             opp: float | None = 68.81) -> Keyword:
    k = Keyword(keyword=text)
    k.status = status
    k.opportunity_score = opp
    session.add(k)
    session.flush()
    session.commit()
    return k


def _signal(session: Session, keyword_id: int, component: str, value: float,
            *, provider: str = "test", raw: dict | None = None) -> KeywordSignal:
    repo = KeywordSignalRepository(session)
    entity = repo.create(
        keyword_id=keyword_id,
        component=component,
        normalized_value=value,
        provider=provider,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw_data=raw or {},
        source_reference="test",
    )
    session.commit()
    return entity


def _seed_catalog(session: Session) -> dict[str, int]:
    repo = AffiliateProgramRepository(session)
    ids: dict[str, int] = {}
    ids["Make"] = repo.create(
        name="Make", provider="direct", category="automation",
        commission_type="percentage", commission_value=35.0,
        match_terms=["Make", "業務効率化", "自動化"],
        status=AffiliateProgramStatus.ACTIVE,
    ).id
    ids["HubSpot"] = repo.create(
        name="HubSpot", provider="Impact", category="crm",
        commission_type="percentage", commission_value=30.0,
        match_terms=["HubSpot", "業務効率化", "CRM"],
        status=AffiliateProgramStatus.ACTIVE,
    ).id
    ids["ClickUp"] = repo.create(
        name="ClickUp", provider="direct", category="task",
        commission_type=None, commission_value=None,
        match_terms=["ClickUp", "業務効率化", "タスク管理"],
        status=AffiliateProgramStatus.ACTIVE,
    ).id
    ids["Paused"] = repo.create(
        name="PausedTool", provider="direct", category="task",
        commission_type="percentage", commission_value=99.0,
        match_terms=["業務効率化"],
        status=AffiliateProgramStatus.PAUSED,
    ).id
    ids["Unrelated"] = repo.create(
        name="Unrelated", provider="direct",
        match_terms=["料理", "レシピ"],
        status=AffiliateProgramStatus.ACTIVE,
    ).id
    session.commit()
    return ids


def _complete_target(session: Session) -> tuple[Keyword, dict[str, int]]:
    ids = _seed_catalog(session)
    k = _keyword(session, "業務効率化 ツール おすすめ")
    _signal(session, k.id, "search_demand", 29.83)
    _signal(session, k.id, "commercial_intent", 80.88)
    _signal(session, k.id, "trend", 77.27)
    _signal(session, k.id, "site_relevance", 80.0)
    _signal(
        session, k.id, "affiliate_opportunity", 86.07, provider="affiliate_catalog",
        raw={"matched_program_ids": [ids["Make"], ids["HubSpot"], ids["ClickUp"]]},
    )
    _signal(
        session, k.id, "originality", 27.27, provider="internal_corpus",
        raw={
            "corpus_available": True,
            "max_similarity": 0.7273,
            "most_similar_kind": "keyword",
            "most_similar_keyword_id": 999,
            "most_similar_keyword_text": "業務効率化 ツール 無料",
        },
    )
    _signal(session, k.id, "competition_ease", 100.0, provider="manual_keyword_difficulty")
    return k, ids


# -- plan for the target keyword ----------------------------------------
def test_plan_target_keyword(session: Session) -> None:
    k, ids = _complete_target(session)
    dto = ArticlePlanService(session).plan_for_keyword(k.id)

    assert dto.keyword_id == k.id
    assert dto.article_type is ArticleType.RECOMMENDATION_ROUNDUP
    assert dto.readiness.complete is True
    assert dto.readiness.opportunity_score == 68.81
    assert dto.proposed_slug == "業務効率化-ツール-おすすめ-roundup"
    assert dto.slug_available is True
    assert [s.level for s in dto.outline][0] == "H1"
    # affiliate: active + matched のみ。paused / unrelated は除外
    names = [c.name for c in dto.affiliate_candidates]
    assert names == ["Make", "HubSpot", "ClickUp"]
    assert "PausedTool" not in names and "Unrelated" not in names
    # 順序: strong が先、その中で percentage DESC、その後 commission 無しは後ろ。
    # generic term だけの match (weak) は role が全て comparison_candidate (C2.5.6)
    assert [c.recommended_role for c in dto.affiliate_candidates] == [
        "comparison_candidate",
        "comparison_candidate",
        "comparison_candidate",
    ]
    assert all(c.match_tier == "weak" for c in dto.affiliate_candidates)
    assert dto.affiliate_candidates[0].commission_value == 35.0
    # cannibalization gate
    assert dto.cannibalization.originality == 27.27
    assert dto.cannibalization.acknowledgment_required is True
    assert dto.cannibalization.most_similar_keyword_text == "業務効率化 ツール 無料"
    assert "業務効率化 ツール 無料" in dto.cannibalization.guidance
    assert any("cannibalization" in w for w in dto.warnings)
    # no tracking url anywhere
    assert "tracking" not in dto.model_dump_json().lower()


def test_plan_is_read_only(session: Session) -> None:
    k, _ids = _complete_target(session)
    before = {
        m.__name__: session.scalar(select(func.count()).select_from(m))
        for m in (Article, KeywordSignal, KeywordScore, AffiliateProgram)
    }
    ArticlePlanService(session).plan_for_keyword(k.id)
    ArticlePlanService(session).plan_for_keyword(k.id)
    after = {
        m.__name__: session.scalar(select(func.count()).select_from(m))
        for m in (Article, KeywordSignal, KeywordScore, AffiliateProgram)
    }
    assert before == after


def test_plan_deterministic(session: Session) -> None:
    k, _ids = _complete_target(session)
    a = ArticlePlanService(session).plan_for_keyword(k.id).model_dump_json()
    b = ArticlePlanService(session).plan_for_keyword(k.id).model_dump_json()
    assert a == b


# -- incomplete keyword -----------------------------------------------
def test_plan_incomplete_keyword_returns_partial(session: Session) -> None:
    _seed_catalog(session)
    k = _keyword(session, "業務効率化 ツール おすすめ", opp=None)
    _signal(session, k.id, "search_demand", 30.0)
    _signal(session, k.id, "site_relevance", 80.0)

    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.readiness.complete is False
    assert set(dto.readiness.missing_components) == set(_ALL7) - {
        "search_demand",
        "site_relevance",
    }
    assert dto.article_type is ArticleType.RECOMMENDATION_ROUNDUP
    assert any("incomplete_plan" in w for w in dto.warnings)
    # originality signal 無し -> ack 不要
    assert dto.cannibalization.acknowledgment_required is False
    assert dto.cannibalization.originality is None


def test_plan_undetermined_article_type_warns(session: Session) -> None:
    _seed_catalog(session)
    k = _keyword(session, "業務効率化 ツール")  # marker 無し
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.article_type is None
    assert any("article_type_undetermined" in w for w in dto.warnings)


def test_plan_keyword_not_found(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        ArticlePlanService(session).plan_for_keyword(123456)


# -- catalog drift semantics ---------------------------------------
def _bare_keyword_with_catalog(session: Session) -> tuple[Keyword, dict[str, int]]:
    """affiliate_opportunity Signal を持たない keyword + マッチする catalog。"""

    ids = _seed_catalog(session)  # Make/HubSpot/ClickUp が "業務効率化" で match
    k = _keyword(session, "業務効率化 ツール おすすめ")
    return k, ids


def _set_ao_signal(session: Session, kid: int, raw: dict) -> None:
    _signal(session, kid, "affiliate_opportunity", 50.0,
            provider="affiliate_catalog", raw=raw)


def test_drift_available_true_no_drift_when_equal(session: Session) -> None:
    k, ids = _bare_keyword_with_catalog(session)
    _set_ao_signal(session, k.id, {
        "matched_program_ids": [ids["Make"], ids["HubSpot"], ids["ClickUp"]]
    })
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.catalog_snapshot_available is True
    assert dto.catalog_drift is False
    assert not any("catalog_snapshot_unavailable" in w for w in dto.warnings)


def test_drift_available_true_drift_when_subset(session: Session) -> None:
    k, ids = _bare_keyword_with_catalog(session)
    _set_ao_signal(session, k.id, {"matched_program_ids": [ids["Make"]]})
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.catalog_snapshot_available is True
    assert dto.catalog_drift is True


def test_drift_explicit_empty_snapshot_with_live_is_drift(session: Session) -> None:
    k, _ids = _bare_keyword_with_catalog(session)
    _set_ao_signal(session, k.id, {"matched_program_ids": []})
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.catalog_snapshot_available is True
    assert dto.snapshot_program_ids == []
    assert len(dto.live_program_ids) == 3
    assert dto.catalog_drift is True


def test_drift_explicit_empty_snapshot_and_empty_live_no_drift(session: Session) -> None:
    # どの program の match_terms にも当たらない keyword を使う (live=[])
    _seed_catalog(session)
    k = _keyword(session, "旅行 ガイド まとめ")
    _set_ao_signal(session, k.id, {"matched_program_ids": []})
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.catalog_snapshot_available is True
    assert dto.snapshot_program_ids == []
    assert dto.live_program_ids == []
    assert dto.catalog_drift is False


def test_drift_signal_missing_is_unavailable_not_drift(session: Session) -> None:
    k, _ids = _bare_keyword_with_catalog(session)  # affiliate_opportunity Signal なし
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.catalog_snapshot_available is False
    assert dto.snapshot_program_ids == []
    assert len(dto.live_program_ids) == 3
    assert dto.catalog_drift is False
    assert any("catalog_snapshot_unavailable" in w for w in dto.warnings)


def test_drift_key_missing_is_unavailable_not_drift(session: Session) -> None:
    k, _ids = _bare_keyword_with_catalog(session)
    _set_ao_signal(session, k.id, {"program_match_score": 12.3})  # matched_program_ids なし
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.catalog_snapshot_available is False
    assert dto.catalog_drift is False
    assert any("catalog_snapshot_unavailable" in w for w in dto.warnings)


def test_drift_ignores_order(session: Session) -> None:
    k, ids = _bare_keyword_with_catalog(session)
    _set_ao_signal(session, k.id, {
        "matched_program_ids": [ids["ClickUp"], ids["Make"], ids["HubSpot"]]  # 逆順
    })
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.catalog_drift is False


def test_catalog_drift_false_when_snapshot_matches_live(session: Session) -> None:
    k, _ids = _complete_target(session)
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.catalog_snapshot_available is True
    assert dto.catalog_drift is False
    assert sorted(dto.snapshot_program_ids) == sorted(dto.live_program_ids)


def test_catalog_drift_true_when_snapshot_differs(session: Session) -> None:
    k, ids = _complete_target(session)
    # snapshot を live と違う id 集合へ差し替えた新しい affiliate_opportunity Signal
    _signal(
        session, k.id, "affiliate_opportunity", 50.0, provider="affiliate_catalog",
        raw={"matched_program_ids": [ids["Make"]]},  # live は Make/HubSpot/ClickUp
    )
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.catalog_drift is True
    assert dto.snapshot_program_ids == [ids["Make"]]
    assert sorted(dto.live_program_ids) == sorted(
        [ids["Make"], ids["HubSpot"], ids["ClickUp"]]
    )
    assert any("catalog_drift" in w for w in dto.warnings)


def test_slug_collision_proposes_alternative(session: Session) -> None:
    k, _ids = _complete_target(session)
    session.add(
        Article(title="x", slug="業務効率化-ツール-おすすめ-roundup", keyword_id=None)
    )
    session.commit()
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.proposed_slug == "業務効率化-ツール-おすすめ-roundup-2"
    assert dto.slug_available is True
    assert any("slug_collision" in w for w in dto.warnings)
# ================================================================ C2.5.4 / C2.5.6 / C2.5.7
def test_candidates_keep_strong_and_weak_with_tier_aware_roles(session: Session) -> None:
    k, _ids = _complete_target(session)
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert [c.name for c in dto.affiliate_candidates] == ["Make", "HubSpot", "ClickUp"]
    # 「業務効率化」だけの generic match は weak + loose: candidate のままだが文脈用のみ
    assert all(c.match_tier == "weak" for c in dto.affiliate_candidates)
    assert all(c.fit == "loose" for c in dto.affiliate_candidates)
    assert all(c.primary_eligible is False for c in dto.affiliate_candidates)
    assert all(c.recommended_role == "comparison_candidate" for c in dto.affiliate_candidates)
    assert all(c.strong_terms == [] and c.weak_terms for c in dto.affiliate_candidates)
    assert all(
        c.tier_reason.startswith("weak: generic term(s) only") for c in dto.affiliate_candidates
    )
    assert (dto.strong_candidate_count, dto.weak_candidate_count) == (0, 3)
    assert (dto.core_weak_candidate_count, dto.loose_weak_candidate_count) == (0, 3)
    assert dto.no_strong_affiliate_candidate is True and dto.no_primary_eligible_candidate is True
    assert dto.alias_only_strong_programs == []
    assert dto.live_program_ids == [c.program_id for c in dto.affiliate_candidates]
    assert dto.catalog_drift is False


def test_brand_keyword_makes_its_candidate_strong_and_orders_it_first(session: Session) -> None:
    _seed_catalog(session)
    k = _keyword(session, "Make 業務効率化 ツール")
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert [c.name for c in dto.affiliate_candidates][0] == "Make"
    make = dto.affiliate_candidates[0]
    assert make.match_tier == "strong" and make.strong_terms == ["Make"]
    assert make.recommended_role == "primary_candidate"  # strong は commission に応じた role
    assert {c.match_tier for c in dto.affiliate_candidates[1:]} == {"weak"}
    assert all(c.recommended_role == "comparison_candidate" for c in dto.affiliate_candidates[1:])
    assert dto.strong_candidate_count == 1 and dto.no_strong_affiliate_candidate is False


def test_english_make_idioms_are_weak_candidates_never_primary(session: Session) -> None:
    _seed_catalog(session)
    for text in ("make sure", "make"):
        dto = ArticlePlanService(session).plan_for_keyword(_keyword(session, text).id)
        make = next(c for c in dto.affiliate_candidates if c.name == "Make")
        assert make.match_tier == "weak" and make.tier_ambiguity is not None, text
        assert make.recommended_role == "comparison_candidate"
        assert dto.no_strong_affiliate_candidate is True


def test_alias_only_strong_program_is_a_strong_candidate(session: Session) -> None:
    _seed_catalog(session)
    dto = ArticlePlanService(session).plan_for_keyword(_keyword(session, "ハブスポット とは").id)
    assert [(c.name, c.match_tier) for c in dto.affiliate_candidates] == [("HubSpot", "strong")]
    assert dto.affiliate_candidates[0].recommended_role == "primary_candidate"
    assert dto.live_program_ids == [dto.affiliate_candidates[0].program_id]
    assert dto.alias_only_strong_programs == ["HubSpot"]  # alias だけで strong だったことを明示
    assert dto.no_strong_affiliate_candidate is False


def test_split_japanese_keyword_gets_the_same_candidates_as_unsplit(session: Session) -> None:
    _seed_catalog(session)
    service = ArticlePlanService(session)
    unsplit = service.plan_for_keyword(_keyword(session, "タスク管理 ツール").id)
    split = service.plan_for_keyword(_keyword(session, "タスク 管理 ツール").id)
    names = [c.name for c in split.affiliate_candidates]
    assert names == [c.name for c in unsplit.affiliate_candidates]
    assert [c.name for c in split.affiliate_candidates] == ["ClickUp"]


def test_tiering_alone_never_creates_catalog_drift(session: Session) -> None:
    """signal の matched_program_ids は candidate と同じ集合 (strong + weak): drift しない。"""

    from app.services.keyword_signal_service import KeywordSignalService

    _seed_catalog(session)
    for text in ("業務効率化 ツール おすすめ", "Make 業務効率化 ツール", "ChatGPT 料金"):
        k = _keyword(session, text)
        KeywordSignalService(session).derive_affiliate_opportunity(k.id)
        dto = ArticlePlanService(session).plan_for_keyword(k.id)
        assert dto.catalog_snapshot_available is True and dto.catalog_drift is False, text
        assert sorted(dto.snapshot_program_ids) == sorted(dto.live_program_ids), text


def test_real_catalog_change_after_derivation_is_still_reported_as_drift(session: Session) -> None:
    from app.services.keyword_signal_service import KeywordSignalService

    ids = _seed_catalog(session)
    k = _keyword(session, "Make 業務効率化 ツール")
    KeywordSignalService(session).derive_affiliate_opportunity(k.id)
    assert ArticlePlanService(session).plan_for_keyword(k.id).catalog_drift is False
    make = session.get(AffiliateProgram, ids["Make"])
    # 業務効率化 を外す (hygiene 相当): その term で match していた program が減る
    make.match_terms = ["Make"]
    click = session.get(AffiliateProgram, ids["ClickUp"])
    click.match_terms = ["ClickUp"]
    session.commit()
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    assert dto.catalog_drift is True and any(w.startswith("catalog_drift") for w in dto.warnings)
    assert set(dto.snapshot_program_ids) > set(dto.live_program_ids)  # 減った分が正確に見える


def test_plan_json_serialization_has_tier_fields_and_no_tracking_urls(session: Session) -> None:
    k, _ids = _complete_target(session)
    payload = ArticlePlanService(session).plan_for_keyword(k.id).model_dump(mode="json")
    cand = payload["affiliate_candidates"][0]
    tier_keys = {"match_tier", "strong_terms", "weak_terms", "tier_reason", "tier_ambiguity"}
    assert tier_keys <= set(cand)
    count_keys = {"strong_candidate_count", "weak_candidate_count", "no_strong_affiliate_candidate"}
    assert count_keys <= set(payload)
    assert "tracking" not in str(payload).lower()


def test_candidates_order_strong_then_core_then_loose_and_report_fit(session: Session) -> None:
    _seed_catalog(session)
    # Make = brand (strong)、HubSpot = CRM (weak + core)、ClickUp = 業務効率化 (weak + loose)
    k = _keyword(session, "Make crm 業務効率化 ツール")
    dto = ArticlePlanService(session).plan_for_keyword(k.id)
    got = [(c.name, c.match_tier, c.fit, c.primary_eligible) for c in dto.affiliate_candidates]
    assert got == [
        ("Make", "strong", "loose", True),  # strong は fit に依存しない (業務効率化 を同時に踏む)
        ("HubSpot", "weak", "core", True),
        ("ClickUp", "weak", "loose", False),
    ]
    by = {c.name: c for c in dto.affiliate_candidates}
    assert by["HubSpot"].core_terms == ["CRM"] and by["HubSpot"].loose_terms == ["業務効率化"]
    assert by["ClickUp"].core_terms == [] and by["ClickUp"].loose_terms == ["業務効率化"]
    assert by["Make"].recommended_role == "primary_candidate"
    assert by["HubSpot"].recommended_role == "comparison_candidate"
    assert (dto.strong_candidate_count, dto.core_weak_candidate_count) == (1, 1)
    assert dto.loose_weak_candidate_count == 1 and dto.primary_eligible_candidate_count == 2


def test_plan_json_carries_the_fit_fields_and_no_tracking_urls(session: Session) -> None:
    _seed_catalog(session)
    k = _keyword(session, "crm ツール")
    payload = ArticlePlanService(session).plan_for_keyword(k.id).model_dump(mode="json")
    cand = payload["affiliate_candidates"][0]
    assert {"fit", "fit_reason", "core_terms", "loose_terms", "unreviewed_terms"} <= set(cand)
    assert {"scoring_eligible", "primary_eligible"} <= set(cand)
    assert {"core_weak_candidate_count", "loose_weak_candidate_count"} <= set(payload)
    assert {"unreviewed_weak_candidate_count", "primary_eligible_candidate_count"} <= set(payload)
    assert "no_primary_eligible_candidate" in payload
    assert "tracking" not in str(payload).lower()
