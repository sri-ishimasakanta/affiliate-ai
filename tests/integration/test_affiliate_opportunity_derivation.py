"""KeywordSignalService.derive_affiliate_opportunity の検証 (独立した in-memory DB)。

実 catalog (dev DB の 19 件) には一切触れない。fixture の in-memory session に
テスト用 program を投入する。
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.exceptions import EntityNotFoundError
from app.models import Keyword
from app.models.enums import AffiliateProgramStatus, KeywordSignalComponent
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_signal_service import KeywordSignalService

_TRACKING = "https://aff.example.test/redirect?token=SUPER_SECRET_TRACK_ID"
_LANDING = "https://lp.example.test/secret-landing"


def _make_keyword(session: Session, text: str) -> Keyword:
    entity = Keyword(keyword=text)
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _seed_catalog(session: Session) -> None:
    repo = AffiliateProgramRepository(session)
    repo.create(
        name="Meeting AI A",
        provider="PartnerStack",
        category="ai_meeting",
        commission_type="fixed",
        commission_value=25,
        currency="USD",
        match_terms=["Meeting", "議事録", "AI 議事録", "文字起こし"],
        tracking_url=_TRACKING,
        landing_page_url=_LANDING,
        status=AffiliateProgramStatus.ACTIVE,
    )
    repo.create(
        name="Meeting AI B",
        provider="direct",
        category="ai_meeting",
        commission_type="percentage",
        commission_value=30,
        match_terms=["Meeting", "議事録", "AI 議事録"],
        status=AffiliateProgramStatus.ACTIVE,
    )
    repo.create(
        name="Meeting AI C",
        provider="Impact",
        category="ai_meeting",
        commission_type="percentage",
        commission_value=10,
        match_terms=["Meeting", "議事録"],
        status=AffiliateProgramStatus.ACTIVE,
    )
    repo.create(
        name="Paused Notion",
        provider="PartnerStack",
        commission_type="percentage",
        commission_value=20,
        match_terms=["Notion", "Notion AI", "議事録"],
        status=AffiliateProgramStatus.PAUSED,
    )
    repo.create(
        name="Unknown Jasper",
        provider="FirstPromoter",
        commission_type="percentage",
        commission_value=25,
        match_terms=["生成AI", "議事録"],
        status=AffiliateProgramStatus.UNKNOWN,
    )
    session.commit()


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def test_derive_creates_signal_from_active_catalog(session: Session) -> None:
    _seed_catalog(session)
    # 3 program の own-name (Meeting) を含む keyword = strong
    keyword = _make_keyword(session, "Meeting 議事録 おすすめ")
    service = KeywordSignalService(session)

    before = datetime.now(UTC).replace(tzinfo=None)
    read = service.derive_affiliate_opportunity(keyword.id)
    after = datetime.now(UTC).replace(tzinfo=None)

    assert read.component == KeywordSignalComponent.AFFILIATE_OPPORTUNITY
    assert read.provider == "affiliate_catalog"
    assert read.source_reference == "affiliate-catalog:local:v2"  # C2.5.7: strong-or-core
    assert read.period_start is None and read.period_end is None
    assert before <= _naive(read.observed_at) <= after

    raw = read.raw_data
    # active のみ: A(fixed) / B(pct30) / C(pct10) の 3 件。paused/unknown は除外。
    assert raw["matched_program_count"] == 3 and raw["scored_program_count"] == 3
    assert sorted(raw["matched_program_names"]) == ["Meeting AI A", "Meeting AI B", "Meeting AI C"]
    assert sorted(raw["strong_program_names"]) == ["Meeting AI A", "Meeting AI B", "Meeting AI C"]
    assert raw["weak_program_ids"] == [] and raw["scoring_policy"] == "strong_or_core_v1"
    assert raw["distinct_provider_count"] == 3
    assert raw["active_providers"] == ["Impact", "PartnerStack", "direct"]
    assert raw["program_match_score"] == 52.76
    assert raw["commission_score"] == 75.0  # max percentage 30% * 2.5
    assert raw["provider_spread_score"] == 100.0
    assert raw["program_match_weight"] == 0.55
    assert raw["commission_weight"] == 0.35
    assert raw["provider_spread_weight"] == 0.10
    assert raw["available_weight"] == 1.0
    assert raw["evidence_coverage"] == 1.0
    assert raw["market_evidence_available"] is True
    assert raw["normalizer"] == {"name": "affiliate_opportunity", "version": "v1"}
    assert raw["normalizer_version"] == "v1"
    assert raw["catalog_size"] == 5
    assert raw["active_catalog_size"] == 3

    # (52.76*0.55 + 75.0*0.35 + 100.0*0.10)
    assert read.normalized_value == pytest.approx(65.27, abs=0.01)


def test_percentage_and_fixed_provenance(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "Meeting 議事録 おすすめ")
    read = KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)

    raw = read.raw_data
    pct = {p["name"]: p["value"] for p in raw["percentage_commissions"]}
    assert pct == {"Meeting AI B": 30.0, "Meeting AI C": 10.0}
    fixed = raw["fixed_commissions"]
    assert len(fixed) == 1
    assert fixed[0]["name"] == "Meeting AI A"
    assert fixed[0]["value"] == 25.0
    assert fixed[0]["currency"] == "USD"


def test_secret_urls_never_in_raw_data(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    read = KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)

    blob = repr(read.raw_data)
    assert "tracking_url" not in blob
    assert "landing_page_url" not in blob
    assert "SUPER_SECRET_TRACK_ID" not in blob
    assert "secret-landing" not in blob


def test_zero_match_creates_signal_with_value_zero(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "ChatGPT 料金")
    read = KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)

    assert read.normalized_value == 0.0
    assert read.raw_data["matched_program_count"] == 0
    assert read.raw_data["market_evidence_available"] is False
    assert read.raw_data["percentage_commissions"] == []
    assert read.raw_data["active_catalog_size"] == 3


def test_paused_and_unknown_excluded(session: Session) -> None:
    _seed_catalog(session)
    # "Notion AI" は paused の Notion 案件にしか term が無い -> 0 match
    keyword = _make_keyword(session, "Notion AI 料金")
    read = KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)
    assert read.normalized_value == 0.0
    assert read.raw_data["matched_program_count"] == 0


def test_derive_persists_and_immutable_history(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    service = KeywordSignalService(session)

    first = service.derive_affiliate_opportunity(keyword.id)
    session.rollback()  # commit 済みなら残る
    assert KeywordSignalRepository(session).get_by_id(first.id) is not None

    second = service.derive_affiliate_opportunity(keyword.id)
    assert first.id != second.id
    history = KeywordSignalRepository(session).list_by_component(
        keyword.id, "affiliate_opportunity"
    )
    assert len(history) == 2
    latest = KeywordSignalRepository(session).get_latest(
        keyword.id, "affiliate_opportunity"
    )
    assert latest.id == second.id


def test_derive_does_not_mutate_catalog(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    before = AffiliateProgramRepository(session).count()

    KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)

    assert AffiliateProgramRepository(session).count() == before


def test_derive_keyword_not_found(session: Session) -> None:
    _seed_catalog(session)
    with pytest.raises(EntityNotFoundError):
        KeywordSignalService(session).derive_affiliate_opportunity(999999)


def test_derive_commit_failure_rolls_back(session: Session, monkeypatch) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    service = KeywordSignalService(session)

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        service.derive_affiliate_opportunity(keyword.id)

    monkeypatch.undo()
    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


# ================================================================ C2.5.6 / C2.5.7: strong-or-core
def _derive(session: Session, text: str):
    keyword = _make_keyword(session, text)
    return KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)


def test_weak_only_matches_score_zero_but_are_retained_in_raw_data(session: Session) -> None:
    _seed_catalog(session)
    # generic term (議事録) だけで 3 program に match。fit config に無い program は unreviewed
    read = _derive(session, "AI 議事録 おすすめ")

    assert read.normalized_value == 0.0  # weak + loose / unreviewed は score 0 (重み無し)
    raw = read.raw_data
    assert raw["market_evidence_available"] is False
    assert raw["program_match_score"] == 0.0 and raw["commission_score"] is None
    assert raw["scored_program_count"] == 0 and raw["scored_program_ids"] == []
    assert raw["percentage_commissions"] == [] and raw["fixed_commissions"] == []
    assert raw["active_providers"] == [] and raw["distinct_provider_count"] == 0
    # weak の match は raw_data / 報告に残る
    assert raw["matched_program_count"] == 3
    assert sorted(raw["weak_program_names"]) == ["Meeting AI A", "Meeting AI B", "Meeting AI C"]
    assert raw["strong_program_ids"] == [] and raw["strong_program_names"] == []
    assert "議事録" in raw["weak_terms"] and raw["strong_terms"] == []
    # matched_program_ids は現在の candidate 集合 (strong + weak)
    assert sorted(raw["matched_program_ids"]) == sorted(raw["weak_program_ids"])
    assert raw["match_semantics"]["japanese_spacing"] is True


def test_scoring_uses_exactly_the_eligible_programs_and_no_weak_weight(session: Session) -> None:
    from app.keyword.affiliate_matching import ProgramFacts
    from app.keyword.affiliate_tiers import match_catalog, scoring_programs
    from app.keyword.normalizers.affiliate_opportunity import (
        calculate_affiliate_opportunity,
    )

    _seed_catalog(session)
    facts = [
        ProgramFacts(p.id, p.name, p.provider, p.category, p.commission_type, p.commission_value,
                     p.currency, tuple(p.match_terms or ()))
        for p in AffiliateProgramRepository(session).list_active(limit=100)
    ]
    for text in ("Meeting 議事録 おすすめ", "AI 議事録 おすすめ", "meeting ai", "ChatGPT 料金"):
        tiered = match_catalog(text, facts)
        expected = calculate_affiliate_opportunity(scoring_programs(tiered))
        read = _derive(session, text)
        assert read.normalized_value == expected.normalized_value, text
        assert read.raw_data["scored_program_count"] == expected.matched_program_count, text
    # 同じ strong 集合なら weak の有無は score を変えない
    strong_only = _derive(session, "Meeting 議事録")
    with_extra_weak = _derive(session, "Meeting 議事録 文字起こし 議事録 AI")
    assert with_extra_weak.normalized_value == strong_only.normalized_value


def test_split_japanese_spelling_scores_like_the_unsplit_spelling(session: Session) -> None:
    """planner / scoring の照合の食い違いを作らない (Google Ads の分かち書きも同じ扱い)。"""

    _seed_catalog(session)
    unsplit = _derive(session, "Meeting 議事録 おすすめ")
    split = _derive(session, "Meeting 議事 録 おすすめ")
    assert split.normalized_value == unsplit.normalized_value > 0
    assert split.raw_data["strong_program_ids"] == unsplit.raw_data["strong_program_ids"]
    weak_unsplit = _derive(session, "議事録 作成")
    weak_split = _derive(session, "議事 録 作成")
    assert weak_unsplit.raw_data["weak_program_ids"] != []
    assert weak_split.raw_data["weak_program_ids"] == weak_unsplit.raw_data["weak_program_ids"]
    # 英語の空白は広げない
    assert _derive(session, "mee ting 議事録").raw_data["strong_program_ids"] == []


def test_alias_only_strong_program_contributes_to_the_score(session: Session) -> None:
    AffiliateProgramRepository(session).create(
        name="HubSpot", provider="Impact", commission_type="percentage", commission_value=30,
        match_terms=["HubSpot", "CRM"], status=AffiliateProgramStatus.ACTIVE,
    )
    session.commit()
    read = _derive(session, "ハブスポット とは")  # 明示 alias だけ (catalog の term には無い)
    assert read.normalized_value > 0 and read.raw_data["scored_program_count"] == 1
    assert read.raw_data["alias_only_strong_program_ids"] == read.raw_data["strong_program_ids"]
    assert read.raw_data["strong_program_names"] == ["HubSpot"]
    # generic だけ (weak) でも CRM は HubSpot の core term なので適格 (brand 名は不要)
    weak_core = _derive(session, "crm おすすめ")
    assert weak_core.raw_data["strong_program_ids"] == []
    assert weak_core.raw_data["weak_core_program_names"] == ["HubSpot"]
    assert weak_core.normalized_value == read.normalized_value


def test_ambiguous_brand_idiom_scores_zero_but_stays_reported(session: Session) -> None:
    AffiliateProgramRepository(session).create(
        name="Make", provider="make", commission_type="percentage", commission_value=35,
        match_terms=["Make"], status=AffiliateProgramStatus.ACTIVE,
    )
    session.commit()
    idiom = _derive(session, "make sure")
    assert idiom.normalized_value == 0.0
    assert idiom.raw_data["weak_program_names"] == ["Make"]
    # 一般語の brand term が weak になった場合の fit は loose (category intent ではない)
    assert idiom.raw_data["weak_loose_program_names"] == ["Make"]
    assert idiom.raw_data["matches"][0]["fit"] == "loose"
    assert idiom.raw_data["matches"][0]["scoring_eligible"] is False
    assert _derive(session, "Make 料金").normalized_value > 0


def test_derivation_writes_only_an_affiliate_opportunity_signal(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "Meeting 議事録 おすすめ")
    KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)
    rows = KeywordSignalRepository(session).list_by_keyword(keyword.id)
    assert {r.component for r in rows} == {"affiliate_opportunity"}
    assert not [k for k in rows[0].raw_data if "url" in k.lower()]


# ================================================================ C2.5.7: brand tier x fit
def _program(session: Session, name: str, terms: list[str], *, pct: float = 20, provider="direct"):
    AffiliateProgramRepository(session).create(
        name=name,
        provider=provider,
        commission_type="percentage",
        commission_value=pct,
        match_terms=terms,
        status=AffiliateProgramStatus.ACTIVE,
    )
    session.commit()


def _expected_value(program_pcts: list[float], providers: list[str]) -> float:
    from app.keyword.affiliate_matching import MatchedProgram
    from app.keyword.normalizers.affiliate_opportunity import calculate_affiliate_opportunity

    programs = [
        MatchedProgram(i, f"p{i}", providers[i - 1], None, ("x",), "percentage", pct, None)
        for i, pct in enumerate(program_pcts, start=1)
    ]
    return calculate_affiliate_opportunity(programs).normalized_value


def test_weak_core_scores_and_weak_loose_does_not_and_both_are_retained(session: Session) -> None:
    _program(session, "HubSpot", ["HubSpot", "CRM", "業務効率化"], provider="Impact", pct=30)
    _program(session, "Todoist", ["Todoist", "業務効率化"], provider="direct", pct=10)
    core = _derive(session, "crm おすすめ")  # HubSpot: CRM (core)。brand 名は無い
    assert core.raw_data["strong_program_ids"] == []
    assert core.raw_data["weak_core_program_names"] == ["HubSpot"]
    assert core.raw_data["scored_program_count"] == 1 and core.normalized_value > 0
    match = core.raw_data["matches"][0]
    assert (match["brand_tier"], match["fit"]) == ("weak", "core")
    assert (match["scoring_eligible"], match["primary_eligible"]) == (True, True)
    assert match["core_terms"] == ["CRM"] and match["loose_terms"] == []

    loose = _derive(session, "業務効率化 ツール")  # 両 program とも loose の term だけ
    assert loose.normalized_value == 0.0 and loose.raw_data["scored_program_count"] == 0
    assert sorted(loose.raw_data["weak_loose_program_names"]) == ["HubSpot", "Todoist"]
    assert loose.raw_data["matched_program_count"] == 2  # candidate 集合には残る (報告 / drift)
    assert all(m["scoring_eligible"] is False for m in loose.raw_data["matches"])

    # 同じ keyword に core と loose が混ざるとき: 数えるのは core の program だけ
    mixed = _derive(session, "crm 業務効率化")
    assert mixed.raw_data["scored_program_ids"] == core.raw_data["scored_program_ids"]
    assert mixed.raw_data["weak_loose_program_names"] == ["Todoist"]
    assert mixed.normalized_value == core.normalized_value  # loose の Todoist は寄与しない


def test_a_program_with_a_core_and_a_loose_term_is_core(session: Session) -> None:
    _program(session, "HubSpot", ["HubSpot", "CRM", "業務効率化"], provider="Impact", pct=30)
    read = _derive(session, "crm 業務効率化 とは")
    match = read.raw_data["matches"][0]
    assert match["fit"] == "core" and match["scoring_eligible"] is True
    assert match["core_terms"] == ["CRM"] and match["loose_terms"] == ["業務効率化"]


def test_unreviewed_terms_and_programs_score_zero_and_are_reported(session: Session) -> None:
    _program(session, "Acme Notes", ["Acme", "ノート術"])  # config に無い program / term
    # Semrush の SEOツール は audit の根拠不足で unreviewed
    _program(session, "Semrush SEO Toolkit", ["Semrush", "SEOツール"], provider="Impact")
    read = _derive(session, "ノート術 SEOツール")
    assert read.normalized_value == 0.0 and read.raw_data["scored_program_count"] == 0
    assert sorted(read.raw_data["weak_unreviewed_program_names"]) == [
        "Acme Notes",
        "Semrush SEO Toolkit",
    ]
    assert read.raw_data["weak_core_program_names"] == []
    assert all(m["fit"] == "unreviewed" for m in read.raw_data["matches"])


def test_strong_match_scores_regardless_of_fit(session: Session) -> None:
    _program(session, "Acme Notes", ["Acme", "ノート術"])  # generic term は unreviewed
    read = _derive(session, "Acme 料金")
    assert read.raw_data["strong_program_names"] == ["Acme Notes"]
    assert read.normalized_value > 0 and read.raw_data["scored_program_count"] == 1
    assert read.raw_data["matches"][0]["fit"] is None  # brand だけ = fit は使わない


def test_bare_fireflies_stays_weak_and_unreviewed_so_it_scores_zero(session: Session) -> None:
    _program(session, "Fireflies.ai", ["Fireflies", "Fireflies.ai", "AI 議事録"], pct=10)
    bare = _derive(session, "fireflies")
    assert bare.normalized_value == 0.0
    match = bare.raw_data["matches"][0]
    assert (match["brand_tier"], match["fit"]) == ("weak", "unreviewed")
    assert match["primary_eligible"] is False
    assert _derive(session, "fireflies.ai 料金").normalized_value > 0  # brand 形は strong
    core = _derive(session, "ai 議事録 ツール")
    assert core.raw_data["weak_core_program_names"] == ["Fireflies.ai"]


def test_no_weak_weight_the_value_depends_only_on_the_eligible_set(session: Session) -> None:
    _program(session, "HubSpot", ["HubSpot", "CRM", "業務効率化"], provider="Impact", pct=30)
    _program(session, "Todoist", ["Todoist", "業務効率化"], provider="direct", pct=50)
    _program(session, "Pipedrive", ["Pipedrive", "CRM"], provider="PartnerStack", pct=20)
    core_only = _derive(session, "crm 比較")  # HubSpot + Pipedrive (core)
    with_loose = _derive(session, "crm 業務効率化 比較")  # + HubSpot 業務効率化 / Todoist (loose)
    assert core_only.raw_data["scored_program_count"] == 2
    assert with_loose.raw_data["scored_program_ids"] == core_only.raw_data["scored_program_ids"]
    assert with_loose.normalized_value == core_only.normalized_value
    assert core_only.normalized_value == _expected_value([30, 20], ["Impact", "PartnerStack"])
