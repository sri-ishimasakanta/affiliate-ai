"""FactPackService.build の検証: readiness / usable-claims / freshness / derived。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, ArticleAffiliateProgram, ArticleFact, Source
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_fact_repository import ArticleFactRepository
from app.services.fact_pack_service import FactPackService

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
FRESH = NOW - timedelta(days=5)
STALE_PRICING = NOW - timedelta(days=40)

_REQUIRED_VALUES = {
    "official_product_name": ("verified", "Make"),
    "official_url": ("verified", "https://www.make.com/"),
    "primary_use_cases": ("verified", ["ワークフロー自動化"]),
    "key_features": ("verified", ["シナリオ", "アプリ連携"]),
    "pricing_summary": ("verified", "Freeプランあり / Team $9/月〜"),
    "free_plan_available": ("verified", True),
}


def _article(session: Session) -> Article:
    a = Article(title="t", slug="a", keyword_id=None)
    session.add(a)
    session.flush()
    session.commit()
    return a


def _link_program(session: Session, article_id: int, name: str) -> int:
    p = AffiliateProgramRepository(session).create(
        name=name, provider="direct", status=AffiliateProgramStatus.ACTIVE
    )
    session.add(
        ArticleAffiliateProgram(article_id=article_id, affiliate_program_id=p.id)
    )
    session.commit()
    return p.id


def _source(session: Session, article_id: int) -> int:
    s = Source(
        article_id=article_id, source_type="official_pricing",
        source_url=f"https://example.test/{article_id}", title="x", checked_at=FRESH,
    )
    session.add(s)
    session.flush()
    session.commit()
    return s.id


def _append(session, article_id, subject, key, status, value, source_id, checked_at):
    ArticleFactRepository(session).append(
        article_id=article_id, subject_ref=subject, affiliate_program_id=None,
        fact_key=key, fact_value=value, value_status=status,
        unknown_reason=None if status == "verified" else "公式に記載なし",
        source_id=source_id, checked_at=checked_at,
    )
    session.commit()


def _fill_required(session, article_id, subject, source_id, *, checked_at=FRESH,
                   pricing_status="verified", pricing_checked_at=None):
    for key, (status, value) in _REQUIRED_VALUES.items():
        if key in ("pricing_summary", "free_plan_available"):
            st = pricing_status
            val = value if st == "verified" else None
            ca = pricing_checked_at or checked_at
        else:
            st, val, ca = status, value, checked_at
        _append(session, article_id, subject, key, st, val, source_id, ca)


# -- readiness ------------------------------------------------------
def test_empty_facts_drafting_false(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    pack = FactPackService(session).build(art.id, now=NOW)
    assert pack.readiness.drafting_allowed is False
    assert pack.readiness.per_tool[0].ok is False
    assert set(pack.readiness.per_tool[0].missing_required) >= {
        "official_product_name", "pricing_summary"
    }


def test_partial_tool_drafting_false(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    sid = _source(session, art.id)
    _append(session, art.id, "Make", "official_url", "verified",
            "https://www.make.com/", sid, FRESH)
    pack = FactPackService(session).build(art.id, now=NOW)
    assert pack.readiness.drafting_allowed is False


def test_all_required_two_tools_drafting_true(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    _link_program(session, art.id, "HubSpot")
    sid = _source(session, art.id)
    _fill_required(session, art.id, "Make", sid)
    _fill_required(session, art.id, "HubSpot", sid)
    pack = FactPackService(session).build(art.id, now=NOW)
    assert pack.readiness.drafting_allowed is True
    assert all(t.ok for t in pack.readiness.per_tool)
    assert pack.readiness.blocking_reasons == []


def test_pricing_missing_blocks(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    sid = _source(session, art.id)
    for key, (status, value) in _REQUIRED_VALUES.items():
        if key in ("pricing_summary", "free_plan_available"):
            continue
        _append(session, art.id, "Make", key, status, value, sid, FRESH)
    pack = FactPackService(session).build(art.id, now=NOW)
    assert pack.readiness.drafting_allowed is False
    assert "pricing_summary" in pack.readiness.per_tool[0].missing_required


def test_pricing_explicit_unknown_allowed(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    sid = _source(session, art.id)
    _fill_required(session, art.id, "Make", sid, pricing_status="unknown")
    pack = FactPackService(session).build(art.id, now=NOW)
    assert pack.readiness.drafting_allowed is True
    # do_not_claim に pricing が入る
    tf = pack.tool_facts[0]
    assert "pricing_summary" in tf.do_not_claim
    assert "free_plan_available" in tf.do_not_claim
    assert {m.fact_key for m in pack.missing_facts if m.reason == "unknown"} >= {
        "pricing_summary", "free_plan_available"
    }


def test_stale_pricing_blocks(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    sid = _source(session, art.id)
    _fill_required(session, art.id, "Make", sid, pricing_checked_at=STALE_PRICING)
    pack = FactPackService(session).build(art.id, now=NOW)
    assert pack.readiness.drafting_allowed is False
    assert "pricing_summary" in pack.readiness.per_tool[0].stale_required
    assert pack.freshness.within_policy is False


def test_recommended_missing_allows_with_warning(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    sid = _source(session, art.id)
    _fill_required(session, art.id, "Make", sid)
    pack = FactPackService(session).build(art.id, now=NOW)
    assert pack.readiness.drafting_allowed is True
    assert any("recommended_fact_missing[Make]" in w for w in pack.warnings)


# -- usable / do_not_claim + derived --------------------------
def test_usable_claims_verified_only_and_derived_timestamps(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    sid = _source(session, art.id)
    _fill_required(session, art.id, "Make", sid, checked_at=FRESH)
    # ai_features を unknown, integrations を verified で追加
    _append(session, art.id, "Make", "ai_features", "unknown", None, sid, FRESH)
    _append(session, art.id, "Make", "integrations", "verified", ["Slack"], sid,
            NOW - timedelta(days=3))

    tf = FactPackService(session).build(art.id, now=NOW).tool_facts[0]
    assert "official_url" in tf.usable_claims
    assert "integrations" in tf.usable_claims
    assert "ai_features" in tf.do_not_claim
    # last_verified_at = 最新 verified fact の checked_at (integrations = -3d)
    assert tf.last_verified_at == NOW - timedelta(days=3)
    # pricing_checked_at = pricing 系 fact の最新 checked_at
    assert tf.pricing_checked_at == FRESH


def test_no_subjects_blocks(session: Session) -> None:
    art = _article(session)
    pack = FactPackService(session).build(art.id, now=NOW)
    assert pack.readiness.drafting_allowed is False
    assert any("no comparison subjects" in r for r in pack.readiness.blocking_reasons)


def _all_keys() -> set[str]:
    from app.article.fact_keys import FactKey

    return {str(k) for k in FactKey}


def test_empty_tool_all_17_keys_in_do_not_claim(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    tf = FactPackService(session).build(art.id, now=NOW).tool_facts[0]
    assert tf.usable_claims == []
    assert set(tf.do_not_claim) == _all_keys()
    assert len(tf.do_not_claim) == 17


def test_required_verified_only_remaining_keys_in_do_not_claim(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    sid = _source(session, art.id)
    _fill_required(session, art.id, "Make", sid)
    tf = FactPackService(session).build(art.id, now=NOW).tool_facts[0]
    assert set(tf.usable_claims) | set(tf.do_not_claim) == _all_keys()
    assert not (set(tf.usable_claims) & set(tf.do_not_claim))
    # 未取得の recommended は do_not_claim に入るが drafting は許可
    assert "ai_features" in tf.do_not_claim
    assert "japanese_language_support" in tf.do_not_claim


def test_unknown_and_verified_partition_do_not_claim(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    sid = _source(session, art.id)
    _fill_required(session, art.id, "Make", sid)
    _append(session, art.id, "Make", "ai_features", "unknown", None, sid, FRESH)
    _append(session, art.id, "Make", "integrations", "verified", ["Slack"], sid, FRESH)
    tf = FactPackService(session).build(art.id, now=NOW).tool_facts[0]
    assert "ai_features" in tf.do_not_claim and "ai_features" not in tf.usable_claims
    assert "integrations" in tf.usable_claims and "integrations" not in tf.do_not_claim


def test_do_not_claim_is_factkey_definition_order(session: Session) -> None:
    from app.article.fact_keys import FactKey

    art = _article(session)
    _link_program(session, art.id, "Make")
    tf = FactPackService(session).build(art.id, now=NOW).tool_facts[0]
    order = [str(k) for k in FactKey]
    assert tf.do_not_claim == [k for k in order if k in set(tf.do_not_claim)]


def test_future_checked_at_not_silently_fresh(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    sid = _source(session, art.id)
    # NOW より未来の checked_at で required を埋める (freshness で通ってはいけない)
    future = NOW + timedelta(hours=9)
    _fill_required(session, art.id, "Make", sid, checked_at=future,
                   pricing_checked_at=future)
    pack = FactPackService(session).build(art.id, now=NOW)
    assert pack.readiness.drafting_allowed is False
    assert pack.freshness.within_policy is False
    assert any("fact_checked_at_in_future[Make]" in w for w in pack.warnings)
    # FactEntry.fresh も False
    tf = pack.tool_facts[0]
    assert all(e.fresh is False for e in tf.facts)


def test_build_does_not_write(session: Session) -> None:
    art = _article(session)
    _link_program(session, art.id, "Make")
    sid = _source(session, art.id)
    _fill_required(session, art.id, "Make", sid)
    before = (
        session.scalar(select(func.count()).select_from(ArticleFact)),
        session.scalar(select(func.count()).select_from(Source)),
    )
    FactPackService(session).build(art.id, now=NOW)
    FactPackService(session).build(art.id, now=NOW)
    after = (
        session.scalar(select(func.count()).select_from(ArticleFact)),
        session.scalar(select(func.count()).select_from(Source)),
    )
    assert before == after
