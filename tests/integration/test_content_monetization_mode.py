"""C2.5.8: content monetization mode (affiliate / supporting) の検証 (独立した in-memory DB)。

「この記事を作ってよいか」と「この記事に affiliate の primary があるか」を分ける。
affiliate の要件だけを mode に応じて外し、affiliate 以外の gate
(fact / 内容 / human review / cannibalization) は変えない。
"""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article import monetization
from app.article.cluster_plan import (
    AffiliateCoverage,
    TemplateReadiness,
    content_monetization,
)
from app.article.draft_output_contract import ParsedDraft
from app.article.draft_output_validators import validate_draft_output
from app.article.draft_prompt_package import EditorialOverridesV1, build_prompt_package
from app.article.draft_prompt_render import render_prompt
from app.article.planning import ArticleType
from app.article.schemas import (
    ArticleAffiliateProgramCreate,
    ArticleMonetizationModeRead,
    ArticleMonetizationModeUpdate,
    ArticlePlanApproveRequest,
)
from app.exceptions import (
    DraftGenerationNotReadyError,
    MonetizationModeTransitionError,
    PlanApprovalError,
)
from app.models import Article, ArticleAffiliateProgram, Keyword
from app.models.enums import AffiliateProgramStatus, ArticleStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_affiliate_program_repository import (
    ArticleAffiliateProgramRepository,
)
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.article_affiliate_program_service import (
    ArticleAffiliateProgramService,
)
from app.services.article_monetization_service import ArticleMonetizationService
from app.services.article_plan_service import ArticlePlanService
from app.services.article_service import ArticleService
from app.services.draft_input_snapshot_builder import DraftInputSnapshotBuilder
from app.services.draft_input_snapshot_service import DraftInputSnapshotService
from app.services.fact_pack_service import FactPackService

NOW = datetime(2026, 9, 22, tzinfo=UTC)


# ---------------------------------------------------------------- fixtures
def _catalog(session: Session) -> dict[str, int]:
    repo = AffiliateProgramRepository(session)
    ids = {
        # CRM = HubSpot / Pipedrive の core。業務効率化 は loose
        "HubSpot": repo.create(
            name="HubSpot", provider="Impact", commission_type="percentage",
            commission_value=30.0, match_terms=["HubSpot", "CRM", "業務効率化"],
            status=AffiliateProgramStatus.ACTIVE).id,
        "Pipedrive": repo.create(
            name="Pipedrive", provider="PartnerStack", commission_type="percentage",
            commission_value=20.0, match_terms=["Pipedrive", "CRM"],
            status=AffiliateProgramStatus.ACTIVE).id,
        # fit config に無い program: その generic term は unreviewed
        "Ghost CRM": repo.create(
            name="Ghost CRM", provider="direct", match_terms=["CRM"],
            status=AffiliateProgramStatus.ACTIVE).id,
    }
    session.commit()
    return ids


def _keyword(session: Session, text: str) -> Keyword:
    k = Keyword(keyword=text)
    k.status = "analyzed"
    k.opportunity_score = 50.0
    session.add(k)
    session.commit()
    KeywordSignalRepository(session).create(
        keyword_id=k.id, component="search_demand", normalized_value=30.0,
        provider="test", observed_at=NOW, raw_data={}, source_reference="test",
    )
    session.commit()
    return k


def _req(**over) -> ArticlePlanApproveRequest:
    base = dict(
        title="t", slug=f"s-{over.pop('slug_suffix', 'x')}",
        acknowledge_incomplete_plan=True, acknowledge_cannibalization=True,
    )
    base.update(over)
    return ArticlePlanApproveRequest(**base)


def _links(session: Session, article_id: int) -> list[tuple[int, bool]]:
    return sorted(
        (x.affiliate_program_id, x.is_primary)
        for x in ArticleAffiliateProgramRepository(session).list_by_article(article_id)
    )


def _counts(session: Session) -> tuple[int, int]:
    return (
        session.scalar(select(func.count()).select_from(Article)),
        session.scalar(select(func.count()).select_from(ArticleAffiliateProgram)),
    )


# ================================================================ pure helpers
def test_recommendation_and_mode_resolution_rules() -> None:
    assert monetization.recommend_mode(2)[0] == "affiliate"
    assert "2" in monetization.recommend_mode(2)[1]
    mode, reason = monetization.recommend_mode(0)
    assert mode == "supporting" and "not a blocker" in reason
    # 省略時: primary があれば affiliate、無ければ supporting (affiliate を自動選択しない)
    assert monetization.resolve_requested_mode(None, 5) == "affiliate"
    assert monetization.resolve_requested_mode(None, None) == "supporting"
    assert monetization.resolve_requested_mode("affiliate", None) == "affiliate"

    class _L:
        def __init__(self, primary: bool) -> None:
            self.is_primary = primary

    # legacy fallback (明示値が無い行だけ): primary の link があれば affiliate
    assert monetization.legacy_mode_from_links([_L(False), _L(True)]) == "affiliate"
    assert monetization.legacy_mode_from_links([_L(False)]) == "supporting"
    assert monetization.legacy_mode_from_links([]) == "supporting"
    # 明示値があれば link を見ない
    explicit = monetization.resolve_effective_mode("affiliate", [])
    assert (explicit.mode, explicit.source, explicit.stored) == (
        "affiliate", "explicit", "affiliate",
    )
    legacy = monetization.resolve_effective_mode(None, [_L(True)])
    assert (legacy.mode, legacy.source, legacy.stored) == ("affiliate", "legacy_derived", None)
    with pytest.raises(ValueError, match="unknown monetization_mode"):
        monetization.resolve_effective_mode("sponsored", [])
    # 比較対象の要件は記事タイプで決まる (未確定は fail-closed)
    assert monetization.requires_comparison_subjects(ArticleType.RECOMMENDATION_ROUNDUP)
    assert monetization.requires_comparison_subjects("comparison_listicle")
    assert not monetization.requires_comparison_subjects(ArticleType.HOW_TO)
    assert not monetization.requires_comparison_subjects("category_landing")
    assert monetization.requires_comparison_subjects(None)
    assert monetization.requires_comparison_subjects("unknown_type")


# ================================================================ plan: recommendation / readiness
def test_plan_recommends_affiliate_when_a_primary_eligible_candidate_exists(
    session: Session,
) -> None:
    _catalog(session)
    plan = ArticlePlanService(session).plan_for_keyword(_keyword(session, "crm おすすめ").id)
    assert plan.primary_eligible_candidate_count == 2  # HubSpot / Pipedrive (weak + core)
    assert plan.recommended_monetization_mode == "affiliate"
    assert "primary-eligible" in plan.monetization_recommendation_reason
    assert plan.affiliate_ready is True and plan.supporting_ready is True
    assert plan.monetization_mode is None  # まだ承認されていない
    assert plan.production_blockers == [] and plan.supporting_blockers == []


def test_plan_recommends_supporting_without_treating_it_as_a_blocker(session: Session) -> None:
    _catalog(session)
    # HubSpot は 業務効率化 (loose) でだけ match: primary_eligible 0 件
    k = _keyword(session, "業務効率化 使い方")
    plan = ArticlePlanService(session).plan_for_keyword(k.id)
    assert [(c.name, c.fit) for c in plan.affiliate_candidates] == [("HubSpot", "loose")]
    assert plan.recommended_monetization_mode == "supporting"
    assert "not a blocker" in plan.monetization_recommendation_reason
    assert plan.affiliate_ready is False
    assert plan.supporting_ready is True  # how_to は比較対象を要求しない
    # C3: how_to の prompt template ができたので、これは blocker ではなくなった
    assert plan.production_blockers == []
    assert "incomplete_plan" in plan.acknowledgements_required  # blocker ではない


def test_plan_reports_non_affiliate_blockers_separately(session: Session) -> None:
    _catalog(session)
    # 比較型 (おすすめ) で候補 0 件: supporting の比較対象が無い (affiliate とは別の内容要件)
    roundup = ArticlePlanService(session).plan_for_keyword(_keyword(session, "RPA おすすめ").id)
    assert roundup.affiliate_candidates == []
    assert roundup.recommended_monetization_mode == "supporting"
    assert roundup.supporting_ready is False
    assert roundup.supporting_blockers == ["comparison_subjects_unavailable"]
    assert roundup.production_blockers == []  # roundup には prompt template がある
    # C3: 推論できない keyword は「承認時に明示せよ」という案内であって、記事タイプの
    # 未確定そのものはもう production blocker ではない
    untyped = ArticlePlanService(session).plan_for_keyword(_keyword(session, "crm ツール").id)
    assert untyped.affiliate_ready is True
    assert untyped.recommended_article_type is None
    assert untyped.production_blockers == ["article_type_not_inferred:select_explicitly"]
    # C3: how_to / pricing の template ができたので template blocker は出ない
    howto = ArticlePlanService(session).plan_for_keyword(_keyword(session, "crm 導入").id)
    assert howto.recommended_article_type is ArticleType.HOW_TO
    assert howto.production_blockers == []
    pricing = ArticlePlanService(session).plan_for_keyword(_keyword(session, "crm 料金").id)
    assert pricing.recommended_article_type is ArticleType.PRICING
    assert pricing.production_blockers == []


# ================================================================ approval: affiliate mode
def test_affiliate_mode_with_an_eligible_primary_succeeds(session: Session) -> None:
    ids = _catalog(session)
    k = _keyword(session, "crm おすすめ")
    read = ArticlePlanService(session).approve(
        k.id,
        _req(monetization_mode="affiliate", primary_affiliate_program_id=ids["HubSpot"],
             secondary_affiliate_program_ids=[ids["Ghost CRM"]]),
    )
    assert read.status is ArticleStatus.PLANNED
    assert _links(session, read.id) == sorted([(ids["HubSpot"], True), (ids["Ghost CRM"], False)])
    plan = ArticlePlanService(session).plan_for_keyword(k.id)
    assert plan.monetization_mode == "affiliate"  # link から導出
    assert plan.production_blockers == [f"live_article_exists:{read.id}"]


def test_affiliate_mode_without_a_primary_fails_and_writes_nothing(session: Session) -> None:
    ids = _catalog(session)
    k = _keyword(session, "crm おすすめ")
    with pytest.raises(PlanApprovalError) as exc:
        ArticlePlanService(session).approve(
            k.id,
            _req(monetization_mode="affiliate",
                 secondary_affiliate_program_ids=[ids["Pipedrive"]]),
        )
    msg = str(exc.value)
    assert "requires primary_affiliate_program_id" in msg and "supporting" in msg
    assert _counts(session) == (0, 0)


@pytest.mark.parametrize(
    ("keyword", "program", "fit"),
    [("業務効率化 おすすめ", "HubSpot", "loose"), ("crm おすすめ", "Ghost CRM", "unreviewed")],
)
def test_affiliate_mode_rejects_loose_and_unreviewed_primaries(
    session: Session, keyword: str, program: str, fit: str
) -> None:
    ids = _catalog(session)
    k = _keyword(session, keyword)
    with pytest.raises(PlanApprovalError, match=f"{fit} fit"):
        ArticlePlanService(session).approve(
            k.id, _req(monetization_mode="affiliate", primary_affiliate_program_id=ids[program])
        )
    assert _counts(session) == (0, 0)


# ================================================================ approval: supporting mode
def test_supporting_mode_with_zero_affiliates_succeeds(session: Session) -> None:
    _catalog(session)
    k = _keyword(session, "業務効率化 使い方")
    read = ArticlePlanService(session).approve(k.id, _req(monetization_mode="supporting"))
    assert read.status is ArticleStatus.PLANNED
    assert _links(session, read.id) == []  # affiliate を自動で選ばない
    assert ArticlePlanService(session).plan_for_keyword(k.id).monetization_mode == "supporting"


def test_supporting_mode_does_not_auto_select_even_when_affiliate_is_recommended(
    session: Session,
) -> None:
    _catalog(session)
    k = _keyword(session, "crm おすすめ")
    assert ArticlePlanService(session).plan_for_keyword(k.id).recommended_monetization_mode == (
        "affiliate"
    )
    read = ArticlePlanService(session).approve(k.id, _req(monetization_mode="supporting"))
    assert _links(session, read.id) == []


def test_supporting_mode_with_a_primary_fails_clearly(session: Session) -> None:
    ids = _catalog(session)
    k = _keyword(session, "crm おすすめ")
    with pytest.raises(PlanApprovalError) as exc:
        ArticlePlanService(session).approve(
            k.id, _req(monetization_mode="supporting", primary_affiliate_program_id=ids["HubSpot"])
        )
    msg = str(exc.value)
    assert "supporting does not take a primary" in msg
    assert "monetization_mode=affiliate" in msg and "remove the primary" in msg
    assert _counts(session) == (0, 0)


def test_supporting_mode_allows_valid_secondaries_only(session: Session) -> None:
    ids = _catalog(session)
    k = _keyword(session, "業務効率化 おすすめ")  # HubSpot (loose) だけが candidate
    with pytest.raises(PlanApprovalError, match="not active matched candidates"):
        ArticlePlanService(session).approve(
            k.id,
            _req(monetization_mode="supporting",
                 secondary_affiliate_program_ids=[ids["Pipedrive"]]),
        )
    assert _counts(session) == (0, 0)
    read = ArticlePlanService(session).approve(
        k.id,
        _req(monetization_mode="supporting", secondary_affiliate_program_ids=[ids["HubSpot"]]),
    )
    assert _links(session, read.id) == [(ids["HubSpot"], False)]  # 比較対象 (contextual)
    assert ArticlePlanService(session).plan_for_keyword(k.id).monetization_mode == "supporting"


def test_omitted_mode_keeps_existing_callers_working(session: Session) -> None:
    ids = _catalog(session)
    svc = ArticlePlanService(session)
    # primary あり -> affiliate として検証される (C2.5.7 の primary 検証もそのまま)
    a = svc.approve(
        _keyword(session, "crm おすすめ").id,
        _req(primary_affiliate_program_id=ids["Pipedrive"], slug_suffix="a"),
    )
    assert _links(session, a.id) == [(ids["Pipedrive"], True)]
    # primary なし (secondary だけ) -> supporting として従来どおり承認できる
    b = svc.approve(
        _keyword(session, "crm 比較").id,
        _req(secondary_affiliate_program_ids=[ids["HubSpot"]], slug_suffix="b"),
    )
    assert _links(session, b.id) == [(ids["HubSpot"], False)]
    # 何も指定しない -> supporting (affiliate 0 件)
    c = svc.approve(_keyword(session, "crm 導入").id, _req(slug_suffix="c"))
    assert _links(session, c.id) == []
    # loose の primary は mode 省略でも拒否 (affiliate として扱われる)
    with pytest.raises(PlanApprovalError, match="loose fit"):
        svc.approve(
            _keyword(session, "業務効率化 おすすめ").id,
            _req(primary_affiliate_program_id=ids["HubSpot"], slug_suffix="d"),
        )


def test_request_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError):
        ArticlePlanApproveRequest(title="t", slug="s", monetization_mode="sponsored")


# ================================================================ drafting readiness
def _approved(session: Session, text: str, **req) -> tuple[int, dict[str, int]]:
    ids = _catalog(session)
    read = ArticlePlanService(session).approve(_keyword(session, text).id, _req(**req))
    return read.id, ids


def test_supporting_how_to_with_zero_links_may_draft(session: Session) -> None:
    article_id, _ = _approved(session, "業務効率化 使い方", monetization_mode="supporting")
    pack = FactPackService(session).build(article_id, now=NOW)
    r = pack.readiness
    assert r.monetization_mode == "supporting"
    assert r.affiliate_monetization == "absent_by_design"
    assert r.comparison_subjects_required is False and r.comparison_subject_count == 0
    assert r.drafting_allowed is True and r.blocking_reasons == []
    assert any(w.startswith("affiliate_monetization_absent_by_design") for w in pack.warnings)


def test_supporting_roundup_still_needs_a_researched_comparison_subject(session: Session) -> None:
    # affiliate ではなく内容の要件: 比較型は比較対象が要る (dummy を作らない)
    article_id, _ = _approved(session, "業務効率化 おすすめ", monetization_mode="supporting")
    r = FactPackService(session).build(article_id, now=NOW).readiness
    assert r.drafting_allowed is False and r.comparison_subjects_required is True
    assert any("least one selected content subject" in b for b in r.blocking_reasons)
    assert not any("affiliate link" in b for b in r.blocking_reasons)


def test_supporting_secondary_subject_still_needs_its_facts(session: Session) -> None:
    ids = _catalog(session)
    read = ArticlePlanService(session).approve(
        _keyword(session, "業務効率化 おすすめ").id,
        _req(monetization_mode="supporting", secondary_affiliate_program_ids=[ids["HubSpot"]]),
    )
    r = FactPackService(session).build(read.id, now=NOW).readiness
    # fact の検証は affiliate と無関係なので supporting でも弱めない (未調査なら止まる)
    assert r.drafting_allowed is False
    assert any(b.startswith("HubSpot: missing required") for b in r.blocking_reasons)
    assert r.affiliate_monetization == "absent_by_design"


def test_affiliate_mode_readiness_is_unchanged(session: Session) -> None:
    ids = _catalog(session)
    read = ArticlePlanService(session).approve(
        _keyword(session, "crm おすすめ").id,
        _req(monetization_mode="affiliate", primary_affiliate_program_id=ids["HubSpot"]),
    )
    r = FactPackService(session).build(read.id, now=NOW).readiness
    assert r.monetization_mode == "affiliate" and r.affiliate_monetization == "present"
    assert r.drafting_allowed is False  # facts 未調査: 従来どおり止まる
    assert any(b.startswith("HubSpot: missing required") for b in r.blocking_reasons)


def test_supporting_freeze_gates_do_not_require_links_or_a_primary(session: Session) -> None:
    article_id, _ = _approved(session, "業務効率化 使い方", monetization_mode="supporting")
    result = DraftInputSnapshotBuilder(session).build(article_id, now=NOW)
    assert result.monetization_mode == "supporting"
    assert result.primary_affiliate_program_id is None and result.comparison_program_ids == []
    assert result.payload["selection"]["primary_affiliate_program_id"] is None
    # 明示 mode は canonical payload に入る (content_hash の入力)
    assert result.payload["monetization"] == {"mode": "supporting", "source": "explicit"}
    failed = result.gate_status["failed_gates"]
    assert "no_comparison_links" not in failed and "primary_not_exactly_one" not in failed
    assert "factpack_drafting_not_allowed" not in failed
    assert result.can_freeze is True  # 他の gate (status / body / freshness) は通常どおり
    assert result.drafting_allowed_at_freeze is True


def test_supporting_freeze_keeps_every_non_affiliate_gate(session: Session) -> None:
    article_id, _ = _approved(session, "業務効率化 使い方", monetization_mode="supporting")
    article = session.get(Article, article_id)
    article.body = "already drafted"
    session.commit()
    failed = DraftInputSnapshotBuilder(session).build(article_id, now=NOW).gate_status[
        "failed_gates"
    ]
    assert failed == ["article_body_present"]
    roundup_id = ArticlePlanService(session).approve(
        _keyword(session, "業務効率化 おすすめ").id,
        _req(monetization_mode="supporting", slug_suffix="r"),
    ).id
    failed = DraftInputSnapshotBuilder(session).build(roundup_id, now=NOW).gate_status[
        "failed_gates"
    ]
    assert {"factpack_drafting_not_allowed", "factpack_blocking_reasons"} <= set(failed)


# ================================================================ prompt stage
def _supporting_payload(session: Session) -> dict:
    article_id, _ = _approved(session, "業務効率化 使い方", monetization_mode="supporting")
    return DraftInputSnapshotBuilder(session).build(article_id, now=NOW).payload


def test_supporting_prompt_package_has_no_primary_and_renders_it_as_absent(
    session: Session,
) -> None:
    payload = _supporting_payload(session)
    package = build_prompt_package(
        snapshot_payload=payload, snapshot_id=1, snapshot_content_hash="0" * 64,
        overrides=EditorialOverridesV1(comparison_set_size=0), now=NOW,
    )
    assert package["primary"] is None and package["comparison_tools"] == []
    prompt = render_prompt(package)
    assert "primary: なし（supporting content" in prompt
    report = validate_draft_output(
        parsed=ParsedDraft(meta_description="m", body_markdown="# 業務効率化の進め方\n本文"),
        package=package,
    )
    fairness = [c for c in report["checks"] if c["id"].startswith("fairness_primary")]
    assert fairness == [
        {"id": "fairness_primary_superlative", "level": "pass",
         "detail": "supporting content: primary なし (対象外)"}
    ]


def test_prompt_package_primary_must_match_the_snapshot_mode(session: Session) -> None:
    payload = _supporting_payload(session)
    with pytest.raises(DraftGenerationNotReadyError, match="omit overrides.primary"):
        build_prompt_package(
            snapshot_payload=payload, snapshot_id=1, snapshot_content_hash="0" * 64,
            overrides=EditorialOverridesV1(primary="HubSpot", comparison_set_size=0), now=NOW,
        )
    affiliate_like = {**payload, "selection": {**payload["selection"],
                                               "primary_affiliate_program_id": 9}}
    with pytest.raises(DraftGenerationNotReadyError, match="must name the primary"):
        build_prompt_package(
            snapshot_payload=affiliate_like, snapshot_id=1, snapshot_content_hash="0" * 64,
            overrides=EditorialOverridesV1(comparison_set_size=0), now=NOW,
        )


# ================================================================ queue classification
def _coverage(**over) -> AffiliateCoverage:
    base = dict(level="none", program_count=0, providers=(), program_names=(),
                strong_program_count=0, weak_program_count=0, no_strong_affiliate_match=True,
                no_eligible_affiliate_match=True)
    base.update(over)
    return AffiliateCoverage(**base)


_READY = TemplateReadiness(True, "article_roundup_v1", "template available")


def test_queue_classifies_affiliate_ready_supporting_only_and_blocked() -> None:
    eligible = _coverage(weak_program_count=1, core_weak_program_names=("HubSpot",),
                         eligible_program_names=("HubSpot",), no_eligible_affiliate_match=False)
    m = content_monetization(decision="ok", article_type=ArticleType.RECOMMENDATION_ROUNDUP,
                             template=_READY, coverage=eligible)
    assert (m.recommended_mode, m.production_readiness) == ("affiliate", "affiliate_ready")
    loose = _coverage(weak_program_count=1, loose_weak_program_names=("ClickUp",))
    m = content_monetization(decision="ok", article_type=ArticleType.RECOMMENDATION_ROUNDUP,
                             template=_READY, coverage=loose)
    assert (m.recommended_mode, m.production_readiness) == ("supporting", "supporting_only")
    assert "not a blocker" in m.reason
    empty_roundup = content_monetization(
        decision="ok", article_type=ArticleType.RECOMMENDATION_ROUNDUP, template=_READY,
        coverage=_coverage(),
    )
    assert empty_roundup.production_readiness == "blocked"
    assert empty_roundup.blockers == ("comparison_subjects_unavailable",)
    untyped = content_monetization(
        decision="ok", article_type=None,
        template=TemplateReadiness(False, None, "article_type_unclassified"), coverage=eligible,
    )
    assert untyped.production_readiness == "blocked"
    assert untyped.blockers == ("article_type_undetermined",)
    assert untyped.recommended_mode == "affiliate"  # 他の理由で止まっても推奨 mode は残す
    merged = content_monetization(decision="merge", article_type=None, template=_READY,
                                  coverage=_coverage())
    assert merged.production_readiness == "not_a_slot"
    legacy = content_monetization(
        decision="ok", article_type=ArticleType.HOW_TO, template=_READY,
        coverage=AffiliateCoverage("none", 0, (), ()),
    )
    assert legacy.recommended_mode is None  # tier 未算出


# ================================================================ explicit mode (C2.5.8 revision)
# mode は編集上の意図であって link の状態ではない: articles.monetization_mode に明示で保存し、
# link を変えても mode は変わらない。NULL は C2.5.8 以前の legacy 行だけ (backfill しない)。
def _legacy_article(
    session: Session,
    keyword_text: str,
    *,
    primary_program_id: int | None = None,
    secondary_program_ids: tuple[int, ...] = (),
    slug: str = "legacy",
    status: ArticleStatus = ArticleStatus.PLANNED,
) -> Article:
    """C2.5.8 以前に承認された article (monetization_mode が NULL) を再現する。"""

    keyword = _keyword(session, keyword_text)
    article = Article(title="legacy", slug=slug, keyword_id=keyword.id)
    article.status = status
    session.add(article)
    session.commit()
    assert article.monetization_mode is None
    links = ArticleAffiliateProgramRepository(session)
    if primary_program_id is not None:
        links.create(
            article_id=article.id, affiliate_program_id=primary_program_id, is_primary=True
        )
    for program_id in secondary_program_ids:
        links.create(article_id=article.id, affiliate_program_id=program_id, is_primary=False)
    session.commit()
    return article


def _mode(session: Session, article_id: int) -> ArticleMonetizationModeRead:
    return ArticleMonetizationService(session).get_mode(article_id)


def _detach_primary(session: Session, article_id: int) -> None:
    links = ArticleAffiliateProgramService(session)
    primary = next(link for link in links.list_by_article(article_id) if link.is_primary)
    links.detach(article_id, primary.id)


def test_legacy_null_mode_with_a_primary_derives_affiliate(session: Session) -> None:
    ids = _catalog(session)
    article = _legacy_article(session, "crm おすすめ", primary_program_id=ids["HubSpot"])

    read = _mode(session, article.id)
    assert read.monetization_mode is None  # 保存値は NULL のまま (backfill しない)
    assert read.effective_monetization_mode == "affiliate"
    assert read.monetization_mode_source == "legacy_derived"
    assert read.mode_requirement_violations == []

    readiness = FactPackService(session).build(article.id, now=NOW).readiness
    assert readiness.monetization_mode == "affiliate"
    assert readiness.monetization_mode_source == "legacy_derived"
    result = DraftInputSnapshotBuilder(session).build(article.id, now=NOW)
    assert (result.monetization_mode, result.monetization_mode_source) == (
        "affiliate",
        "legacy_derived",
    )
    # legacy 行の payload / content_hash は C2.5.8 以前と同一 (キーを足さない)
    assert "monetization" not in result.payload


def test_legacy_null_mode_without_a_primary_derives_supporting(session: Session) -> None:
    ids = _catalog(session)
    article = _legacy_article(
        session, "業務効率化 使い方", secondary_program_ids=(ids["HubSpot"],)
    )
    read = _mode(session, article.id)
    assert (read.monetization_mode, read.effective_monetization_mode) == (None, "supporting")
    assert read.monetization_mode_source == "legacy_derived"
    payload = DraftInputSnapshotBuilder(session).build(article.id, now=NOW).payload
    assert "monetization" not in payload


def test_legacy_null_mode_is_reported_by_the_plan(session: Session) -> None:
    ids = _catalog(session)
    article = _legacy_article(session, "crm おすすめ", primary_program_id=ids["HubSpot"])
    plan = ArticlePlanService(session).plan_for_keyword(article.keyword_id)
    assert plan.monetization_mode == "affiliate"
    assert plan.monetization_mode_source == "legacy_derived"


def test_approval_persists_the_mode_explicitly(session: Session) -> None:
    ids = _catalog(session)
    svc = ArticlePlanService(session)
    affiliate = svc.approve(
        _keyword(session, "crm おすすめ").id,
        _req(
            monetization_mode="affiliate",
            primary_affiliate_program_id=ids["HubSpot"],
            slug_suffix="a",
        ),
    )
    supporting = svc.approve(
        _keyword(session, "業務効率化 使い方").id,
        _req(monetization_mode="supporting", slug_suffix="s"),
    )
    assert session.get(Article, affiliate.id).monetization_mode == "affiliate"
    assert session.get(Article, supporting.id).monetization_mode == "supporting"
    for article_id, mode in ((affiliate.id, "affiliate"), (supporting.id, "supporting")):
        read = _mode(session, article_id)
        assert (read.monetization_mode, read.effective_monetization_mode) == (mode, mode)
        assert read.monetization_mode_source == "explicit"


def test_omitted_mode_is_still_persisted_explicitly(session: Session) -> None:
    ids = _catalog(session)
    svc = ArticlePlanService(session)
    # primary あり -> affiliate、primary なし -> supporting (後方互換の既定値を承認時に確定する)
    with_primary = svc.approve(
        _keyword(session, "crm おすすめ").id,
        _req(primary_affiliate_program_id=ids["Pipedrive"], slug_suffix="p"),
    )
    without_primary = svc.approve(
        _keyword(session, "crm 比較").id,
        _req(secondary_affiliate_program_ids=[ids["HubSpot"]], slug_suffix="n"),
    )
    assert session.get(Article, with_primary.id).monetization_mode == "affiliate"
    assert session.get(Article, without_primary.id).monetization_mode == "supporting"
    assert _mode(session, with_primary.id).monetization_mode_source == "explicit"


def _approved_affiliate(session: Session) -> tuple[int, dict[str, int]]:
    ids = _catalog(session)
    read = ArticlePlanService(session).approve(
        _keyword(session, "crm おすすめ").id,
        _req(monetization_mode="affiliate", primary_affiliate_program_id=ids["HubSpot"]),
    )
    return read.id, ids


def test_removing_the_primary_does_not_change_an_explicit_affiliate_mode(
    session: Session,
) -> None:
    article_id, _ = _approved_affiliate(session)
    _detach_primary(session, article_id)

    read = _mode(session, article_id)
    assert read.monetization_mode == "affiliate"  # supporting に落ちない
    assert read.effective_monetization_mode == "affiliate"
    assert read.monetization_mode_source == "explicit"
    assert read.primary_affiliate_program_id is None
    # 代わりに「primary が無い」と報告される
    assert read.mode_requirement_violations == ["no_comparison_links", "primary_not_exactly_one"]


def test_explicit_affiliate_without_a_primary_is_not_ready_and_freeze_blocked(
    session: Session,
) -> None:
    article_id, _ = _approved_affiliate(session)
    _detach_primary(session, article_id)

    pack = FactPackService(session).build(article_id, now=NOW).readiness
    assert pack.monetization_mode == "affiliate" and pack.monetization_mode_source == "explicit"
    assert pack.drafting_allowed is False
    # C3: 比較対象は承認時に固定済みなので link を外しても消えない。
    # 「affiliate の primary が無い」は freeze gate の担当になった。
    assert pack.comparison_subject_source == "persisted"
    assert pack.comparison_subject_count == 1

    result = DraftInputSnapshotBuilder(session).build(article_id, now=NOW)
    assert result.can_freeze is False
    assert {"no_comparison_links", "primary_not_exactly_one"} <= set(
        result.gate_status["failed_gates"]
    )
    assert result.payload["monetization"] == {"mode": "affiliate", "source": "explicit"}


def test_affiliate_to_supporting_needs_an_explicit_transition(session: Session) -> None:
    article_id, _ = _approved_affiliate(session)
    svc = ArticleMonetizationService(session)
    # primary が残っている間は supporting にできない
    with pytest.raises(MonetizationModeTransitionError, match="still has a primary affiliate"):
        svc.set_mode(article_id, ArticleMonetizationModeUpdate(monetization_mode="supporting"))
    assert session.get(Article, article_id).monetization_mode == "affiliate"

    _detach_primary(session, article_id)
    assert session.get(Article, article_id).monetization_mode == "affiliate"  # link 操作では不変

    read = svc.set_mode(
        article_id, ArticleMonetizationModeUpdate(monetization_mode="supporting")
    )
    assert read.monetization_mode == "supporting"
    assert read.monetization_mode_source == "explicit"
    assert session.get(Article, article_id).monetization_mode == "supporting"


def test_supporting_to_affiliate_requires_an_eligible_primary(session: Session) -> None:
    ids = _catalog(session)
    article_id = (
        ArticlePlanService(session)
        .approve(_keyword(session, "crm おすすめ").id, _req(monetization_mode="supporting"))
        .id
    )
    svc = ArticleMonetizationService(session)
    update = ArticleMonetizationModeUpdate(monetization_mode="affiliate")

    with pytest.raises(MonetizationModeTransitionError, match="requires exactly one primary"):
        svc.set_mode(article_id, update)
    assert session.get(Article, article_id).monetization_mode == "supporting"

    links = ArticleAffiliateProgramService(session)
    ghost = links.attach(
        article_id,
        ArticleAffiliateProgramCreate(affiliate_program_id=ids["Ghost CRM"], is_primary=True),
    )
    # weak + unreviewed は primary になれない (fail-closed)
    with pytest.raises(MonetizationModeTransitionError, match="unreviewed fit"):
        svc.set_mode(article_id, update)
    links.detach(article_id, ghost.id)

    links.attach(
        article_id,
        ArticleAffiliateProgramCreate(affiliate_program_id=ids["HubSpot"], is_primary=True),
    )
    read = svc.set_mode(article_id, update)  # weak + core は可
    assert (read.monetization_mode, read.monetization_mode_source) == ("affiliate", "explicit")
    assert read.mode_requirement_violations == []


def test_transition_to_affiliate_rejects_a_loose_primary(session: Session) -> None:
    ids = _catalog(session)
    article_id = (
        ArticlePlanService(session)
        .approve(
            _keyword(session, "業務効率化 おすすめ").id, _req(monetization_mode="supporting")
        )
        .id
    )
    ArticleAffiliateProgramService(session).attach(
        article_id,
        ArticleAffiliateProgramCreate(affiliate_program_id=ids["HubSpot"], is_primary=True),
    )
    with pytest.raises(MonetizationModeTransitionError, match="loose fit"):
        ArticleMonetizationService(session).set_mode(
            article_id, ArticleMonetizationModeUpdate(monetization_mode="affiliate")
        )
    assert session.get(Article, article_id).monetization_mode == "supporting"


def test_transition_promotes_a_legacy_row_to_an_explicit_value(session: Session) -> None:
    ids = _catalog(session)
    article = _legacy_article(session, "crm おすすめ", primary_program_id=ids["HubSpot"])
    read = ArticleMonetizationService(session).set_mode(
        article.id, ArticleMonetizationModeUpdate(monetization_mode="affiliate")
    )
    assert read.monetization_mode == "affiliate"
    assert read.monetization_mode_source == "explicit"
    assert session.get(Article, article.id).monetization_mode == "affiliate"


def test_transition_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError):
        ArticleMonetizationModeUpdate(monetization_mode="sponsored")


# ================================================================ snapshot / hash semantics
def test_explicit_mode_changes_new_snapshot_hashes_only(session: Session) -> None:
    """明示 mode は content_hash の入力。NULL (legacy) の hash は C2.5.8 以前と同じ。"""

    _catalog(session)
    article_id = (
        ArticlePlanService(session)
        .approve(
            _keyword(session, "業務効率化 使い方").id, _req(monetization_mode="supporting")
        )
        .id
    )
    builder = DraftInputSnapshotBuilder(session)
    supporting_hash = builder.build(article_id, now=NOW).content_hash

    article = session.get(Article, article_id)
    # 列を直接書き換えるのは「明示的な mode 変更」の短縮 (link は一切変えない)
    article.monetization_mode = None
    session.commit()
    legacy_result = builder.build(article_id, now=NOW)
    assert "monetization" not in legacy_result.payload

    article.monetization_mode = "affiliate"
    session.commit()
    affiliate_hash = builder.build(article_id, now=NOW).content_hash

    assert len({supporting_hash, legacy_result.content_hash, affiliate_hash}) == 3


def test_frozen_snapshots_are_not_rewritten_when_the_mode_changes(session: Session) -> None:
    _catalog(session)
    article_id = (
        ArticlePlanService(session)
        .approve(
            _keyword(session, "業務効率化 使い方").id, _req(monetization_mode="supporting")
        )
        .id
    )
    svc = DraftInputSnapshotService(session)
    frozen = svc.freeze(article_id, svc.preview(article_id, now=NOW).content_hash, now=NOW)
    stored_hash = frozen.snapshot.content_hash
    stored_payload = deepcopy(frozen.snapshot.payload)
    assert stored_payload["monetization"] == {"mode": "supporting", "source": "explicit"}

    article = session.get(Article, article_id)
    article.monetization_mode = "affiliate"
    session.commit()

    # 既存行は不変。新しい build だけ hash が変わる
    reread = svc.get(article_id, frozen.snapshot.id)
    assert reread.content_hash == stored_hash and reread.payload == stored_payload
    assert svc.preview(article_id, now=NOW).content_hash != stored_hash
    assert [row.content_hash for row in svc.list_for_article(article_id)] == [stored_hash]


def test_legacy_snapshots_without_the_mode_key_stay_readable(session: Session) -> None:
    _catalog(session)
    article = _legacy_article(session, "業務効率化 使い方", slug="legacy-snap")
    # C2.5.8 以前に凍結された snapshot の形 (monetization キーなし)
    svc = DraftInputSnapshotService(session)
    preview = svc.preview(article.id, now=NOW)
    assert "monetization" not in preview.payload
    frozen = svc.freeze(article.id, preview.content_hash, now=NOW).snapshot
    assert "monetization" not in frozen.payload

    read = svc.get(article.id, frozen.id)
    assert read.payload == frozen.payload and read.content_hash == frozen.content_hash
    # 明示値を与えたあとも、古い行はそのまま読める
    ArticleMonetizationService(session).set_mode(
        article.id, ArticleMonetizationModeUpdate(monetization_mode="supporting")
    )
    assert "monetization" not in svc.get(article.id, frozen.id).payload


def test_article_one_shape_stays_legacy_null_and_effectively_affiliate(
    session: Session,
) -> None:
    """article #1 (published / primary=Make / 7 links) の形: NULL のまま affiliate に解決される。"""

    repo = AffiliateProgramRepository(session)
    make_id = repo.create(
        name="Make",
        provider="direct",
        match_terms=["Make", "業務効率化"],
        status=AffiliateProgramStatus.ACTIVE,
    ).id
    others = tuple(
        repo.create(
            name=f"Tool {n}",
            provider="direct",
            match_terms=[f"Tool {n}", "業務効率化"],
            status=AffiliateProgramStatus.ACTIVE,
        ).id
        for n in range(6)
    )
    session.commit()
    article = _legacy_article(
        session,
        "業務効率化 ツール おすすめ",
        primary_program_id=make_id,
        secondary_program_ids=others,
        slug="article-1",
        status=ArticleStatus.PUBLISHED,
    )

    read = _mode(session, article.id)
    assert read.monetization_mode is None
    assert read.effective_monetization_mode == "affiliate"
    assert read.monetization_mode_source == "legacy_derived"
    assert read.primary_affiliate_program_id == make_id
    assert read.affiliate_program_link_count == 7
    assert read.mode_requirement_violations == []
    assert session.get(Article, article.id).monetization_mode is None


# ================================================================ published: mode is frozen
def _article_state(session: Session, article_id: int) -> tuple:
    article = session.get(Article, article_id)
    session.refresh(article)
    return (
        article.monetization_mode,
        article.status,
        article.updated_at,
        tuple(_links(session, article_id)),
    )


@pytest.mark.parametrize("target", ["supporting", "affiliate"])
def test_published_articles_cannot_change_their_monetization_mode(
    session: Session, target: str
) -> None:
    ids = _catalog(session)
    article = _legacy_article(
        session,
        "crm おすすめ",
        primary_program_id=ids["HubSpot"],
        slug="published-legacy",
        status=ArticleStatus.PUBLISHED,
    )
    before = _article_state(session, article.id)

    with pytest.raises(MonetizationModeTransitionError, match="is published"):
        ArticleMonetizationService(session).set_mode(
            article.id, ArticleMonetizationModeUpdate(monetization_mode=target)
        )

    # 拒否時は DB へ一切書かない: legacy の NULL も link もそのまま
    assert _article_state(session, article.id) == before
    assert session.get(Article, article.id).monetization_mode is None
    read = _mode(session, article.id)
    assert read.effective_monetization_mode == "affiliate"
    assert read.monetization_mode_source == "legacy_derived"


def test_published_articles_reject_even_a_same_value_explicit_request(session: Session) -> None:
    ids = _catalog(session)
    article_id = (
        ArticlePlanService(session)
        .approve(
            _keyword(session, "crm おすすめ").id,
            _req(monetization_mode="affiliate", primary_affiliate_program_id=ids["HubSpot"]),
        )
        .id
    )
    article = session.get(Article, article_id)
    article.status = ArticleStatus.PUBLISHED
    session.commit()
    before = _article_state(session, article_id)

    with pytest.raises(MonetizationModeTransitionError, match="revision"):
        ArticleMonetizationService(session).set_mode(
            article_id, ArticleMonetizationModeUpdate(monetization_mode="affiliate")
        )

    assert _article_state(session, article_id) == before
    assert _mode(session, article_id).monetization_mode == "affiliate"


def test_non_published_statuses_can_still_change_their_mode(session: Session) -> None:
    _catalog(session)
    article_id = (
        ArticlePlanService(session)
        .approve(
            _keyword(session, "業務効率化 使い方").id, _req(monetization_mode="supporting")
        )
        .id
    )
    svc = ArticleMonetizationService(session)
    for status in (ArticleStatus.DRAFTING, ArticleStatus.REVIEW, ArticleStatus.APPROVED):
        article = session.get(Article, article_id)
        article.status = status
        session.commit()
        # 同じ値の明示は no-op (書き込まない) で通る
        assert svc.set_mode(
            article_id, ArticleMonetizationModeUpdate(monetization_mode="supporting")
        ).monetization_mode == "supporting"


# ============================================================ ArticleRead exposes the stored value
def test_article_read_exposes_the_stored_monetization_mode(session: Session) -> None:
    ids = _catalog(session)
    svc = ArticlePlanService(session)
    affiliate = svc.approve(
        _keyword(session, "crm おすすめ").id,
        _req(
            monetization_mode="affiliate",
            primary_affiliate_program_id=ids["HubSpot"],
            slug_suffix="ar-a",
        ),
    )
    supporting = svc.approve(
        _keyword(session, "業務効率化 使い方").id,
        _req(monetization_mode="supporting", slug_suffix="ar-s"),
    )
    assert affiliate.monetization_mode == "affiliate"
    assert supporting.monetization_mode == "supporting"
    # legacy 行は None のまま (ArticleRead は保存値だけを出し、実効 mode を導出しない)
    legacy = _legacy_article(
        session, "crm 比較", primary_program_id=ids["HubSpot"], slug="ar-legacy"
    )
    assert ArticleService(session).get_article(legacy.id).monetization_mode is None
    assert _mode(session, legacy.id).effective_monetization_mode == "affiliate"
    assert ArticleService(session).get_article(affiliate.id).monetization_mode == "affiliate"
