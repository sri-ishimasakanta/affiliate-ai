"""C3: 記事タイプ / template / 編集 subject / supporting prompt の end-to-end 検証。

plan -> approval -> subjects -> fact pack -> snapshot -> prompt package まで、
in-memory DB だけで通す (production には触れない)。
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.article_type_resolution import (
    SOURCE_EXPLICIT,
    SOURCE_INFERRED,
    resolve_article_type,
)
from app.article.cluster_plan import template_readiness
from app.article.content_subjects import load_content_subject_config
from app.article.draft_prompt_package import EditorialOverridesV1, build_prompt_package
from app.article.draft_prompt_render import render_prompt
from app.article.planning import ArticleType
from app.article.portfolio_readiness import evaluate_portfolio
from app.article.schemas import ArticlePlanApproveRequest
from app.exceptions import PlanApprovalError
from app.models import Article, ArticleContentSubject, Keyword
from app.models.enums import AffiliateProgramStatus, KeywordStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.article_plan_service import ArticlePlanService
from app.services.content_subject_service import (
    SOURCE_LEGACY_LINKS,
    SOURCE_PERSISTED,
    ContentSubjectService,
)
from app.services.draft_input_snapshot_builder import DraftInputSnapshotBuilder
from app.services.fact_pack_service import FactPackService

NOW = datetime(2026, 9, 22, tzinfo=UTC)


# ---------------------------------------------------------------- fixtures
def _catalog(session: Session) -> dict[str, int]:
    repo = AffiliateProgramRepository(session)
    ids = {
        "Krisp": repo.create(
            name="Krisp", provider="direct", commission_type="percentage",
            commission_value=30.0, match_terms=["Krisp", "AI 議事録", "議事録"],
            status=AffiliateProgramStatus.ACTIVE).id,
        "HubSpot": repo.create(
            name="HubSpot", provider="Impact", commission_type="percentage",
            commission_value=30.0, match_terms=["HubSpot", "CRM"],
            status=AffiliateProgramStatus.ACTIVE).id,
        "Make": repo.create(
            name="Make", provider="make", commission_type="percentage",
            commission_value=25.0, match_terms=["Make"],
            status=AffiliateProgramStatus.ACTIVE).id,
    }
    session.commit()
    return ids


def _keyword(session: Session, text: str) -> Keyword:
    entity = Keyword(keyword=text)
    entity.status = KeywordStatus.ANALYZED
    entity.opportunity_score = 50.0
    session.add(entity)
    session.commit()
    KeywordSignalRepository(session).create(
        keyword_id=entity.id, component="search_demand", normalized_value=30.0,
        provider="test", observed_at=NOW, raw_data={}, source_reference="test",
    )
    session.commit()
    return entity


def _approve(session: Session, keyword: str, *, slug: str, **over):
    payload = dict(
        title="t", slug=slug, acknowledge_incomplete_plan=True,
        acknowledge_cannibalization=True,
    )
    payload.update(over)
    return ArticlePlanService(session).approve(
        _keyword(session, keyword).id, ArticlePlanApproveRequest(**payload)
    )


# ================================================================ portfolio readiness (L)
def test_every_selected_portfolio_article_is_structurally_producible() -> None:
    """C3 の成功条件: 24 件すべてが型・template・比較対象の面で構造的に作れる。"""

    readiness = evaluate_portfolio()

    assert readiness.total == 24
    assert readiness.types_resolved == 24
    assert readiness.templates_resolved == 24
    assert readiness.blocker_counts() == {}
    assert readiness.producible == 24
    # 「記事タイプ未確定」「template が無い」で止まる記事は 0 件
    assert not [e for e in readiness.entries if not e.article_type_resolved]
    assert not [e for e in readiness.entries if not e.template_resolved]


def test_supporting_comparison_articles_are_unblocked_by_editorial_subjects() -> None:
    """affiliate 候補ゼロの比較 / roundup が、affiliate を捏造せずに比較対象を持てる。"""

    by_keyword = {e.keyword: e for e in evaluate_portfolio().entries}
    for keyword in ("RPA おすすめ", "RPA 比較", "生成AI ツール おすすめ"):
        entry = by_keyword[keyword]
        assert entry.monetization_mode == "supporting"
        assert entry.comparison_subjects_required is True
        assert entry.non_affiliate_subject_count >= 5
        assert entry.affiliate_primary_required is False
        assert entry.blockers == ()


def test_every_article_type_has_its_own_template() -> None:
    versions = set()
    for article_type in ArticleType:
        readiness = template_readiness(article_type)
        assert readiness.ready, article_type
        versions.add(readiness.template_version)
    # 型ごとに別 template (見出しだけ変えた複製ではない)
    assert len(versions) == len(list(ArticleType))


# ================================================================ article type (C / D)
def test_article_type_is_explicitly_selectable_and_overrides_inference(
    session: Session,
) -> None:
    _catalog(session)
    # "ChatGPT Enterprise" は marker が無く推論できない -> 明示選択でだけ承認できる
    plan = ArticlePlanService(session).plan_for_keyword(
        _keyword(session, "ChatGPT Enterprise").id
    )
    assert plan.recommended_article_type is None

    with pytest.raises(PlanApprovalError, match="pass an explicit article_type"):
        _approve(session, "ChatGPT Enterprise 2", slug="ce-2", monetization_mode="supporting")

    read = _approve(
        session, "ChatGPT Enterprise 3", slug="ce-3", monetization_mode="supporting",
        article_type=ArticleType.INFORMATIONAL,
    )
    stored = session.get(Article, read.id)
    assert stored.article_type == "informational"
    effective = resolve_article_type(stored.article_type, "ChatGPT Enterprise 3")
    assert effective.article_type is ArticleType.INFORMATIONAL
    assert effective.source == SOURCE_EXPLICIT
    assert effective.recommended is None  # 推論はできなかったが人が確定した


def test_inference_still_works_and_is_reported_as_a_recommendation(
    session: Session,
) -> None:
    """推論は「推奨」として残る (確定手段が推論だけではなくなっただけ)。"""

    _catalog(session)
    plan = ArticlePlanService(session).plan_for_keyword(_keyword(session, "CRM 比較").id)
    assert plan.recommended_article_type is ArticleType.COMPARISON_LISTICLE
    assert plan.article_type_recommendation_marker == "比較"
    # 省略すれば推奨がそのまま採用される (後方互換)
    read = _approve(session, "CRM おすすめ", slug="crm-inferred", monetization_mode="supporting")
    assert session.get(Article, read.id).article_type == "recommendation_roundup"


def test_legacy_articles_keep_inferring_their_type(session: Session) -> None:
    """article_type が NULL の legacy 行は keyword から推論する (backfill しない)。"""

    effective = resolve_article_type(None, "業務効率化 ツール おすすめ")
    assert effective.article_type is ArticleType.RECOMMENDATION_ROUNDUP
    assert effective.source == SOURCE_INFERRED
    assert effective.stored is None


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("HubSpot 料金", ArticleType.PRICING),
        ("Make 使い方", ArticleType.HOW_TO),
        ("AI 議事録 比較", ArticleType.COMPARISON_LISTICLE),
        ("CRM おすすめ", ArticleType.RECOMMENDATION_ROUNDUP),
        ("生成AI ガイドライン", ArticleType.INFORMATIONAL),
        ("AI ガバナンス", ArticleType.INFORMATIONAL),
        ("生成AI とは", ArticleType.CATEGORY_LANDING),
    ],
)
def test_portfolio_intents_infer_to_the_expected_type(
    keyword: str, expected: ArticleType
) -> None:
    assert resolve_article_type(None, keyword).article_type is expected


def test_free_keywords_are_not_forced_into_pricing() -> None:
    """「無料」は料金ページとは限らない -- 推論せず、人に選ばせる。"""

    assert resolve_article_type(None, "AI 議事録 無料").article_type is None
    assert resolve_article_type(None, "文字起こし 無料").article_type is None


# ================================================================ content subjects (G–J)
def test_editorial_subjects_do_not_require_an_affiliate_program(session: Session) -> None:
    _catalog(session)
    read = _approve(
        session, "RPA おすすめ", slug="rpa-osusume", monetization_mode="supporting",
        article_type=ArticleType.RECOMMENDATION_ROUNDUP,
        content_subject_keys=["uipath", "winactor", "power-automate"],
    )
    resolution = ContentSubjectService(session).resolve(read.id)

    assert resolution.source == SOURCE_PERSISTED
    assert resolution.display_names == ("UiPath", "WinActor", "Power Automate")
    assert all(s.affiliate_program_id is None for s in resolution.subjects)
    assert resolution.programs == []  # affiliate を捏造しない

    pack = FactPackService(session).build(read.id, now=NOW)
    assert pack.readiness.comparison_subject_count == 3
    assert pack.readiness.non_affiliate_subject_count == 3
    assert pack.readiness.comparison_subject_source == "persisted"
    # 比較対象が無いことは blocker ではなくなった (事実未調査は通常の production 作業)
    assert not any("comparison subject" in b for b in pack.readiness.blocking_reasons)
    assert any(b.startswith("UiPath") for b in pack.readiness.blocking_reasons)


def test_affiliate_and_editorial_subjects_can_be_mixed(session: Session) -> None:
    ids = _catalog(session)
    read = _approve(
        session, "AI 議事録 おすすめ", slug="ai-gijiroku-osusume",
        monetization_mode="affiliate", primary_affiliate_program_id=ids["Krisp"],
        article_type=ArticleType.RECOMMENDATION_ROUNDUP,
        content_subject_keys=["notion-ai"],
    )
    resolution = ContentSubjectService(session).resolve(read.id)

    assert resolution.display_names == ("Krisp", "Notion AI")
    assert resolution.subjects[0].affiliate_program_id == ids["Krisp"]
    assert resolution.subjects[1].affiliate_program_id is None
    assert [p.name for p in resolution.programs] == ["Krisp"]


def test_selected_subjects_survive_a_config_change(session: Session) -> None:
    """承認済み記事の比較対象は config を変えても動かない (行として固定済み)。"""

    _catalog(session)
    read = _approve(
        session, "RPA 比較", slug="rpa-hikaku", monetization_mode="supporting",
        article_type=ArticleType.COMPARISON_LISTICLE,
        content_subject_keys=["uipath", "bizrobo"],
    )
    rows = session.scalars(
        select(ArticleContentSubject).where(ArticleContentSubject.article_id == read.id)
    ).all()
    assert {r.subject_key for r in rows} == {"uipath", "bizrobo"}

    # catalog から uipath を消しても、承認済みの行は残る
    empty_catalog = load_content_subject_config()
    service = ContentSubjectService(session, catalog=empty_catalog)
    assert service.resolve(read.id).display_names == ("UiPath", "BizRobo!")


def test_unknown_subject_keys_are_refused_at_approval(session: Session) -> None:
    _catalog(session)
    with pytest.raises(PlanApprovalError, match="unknown content_subject_keys"):
        _approve(
            session, "RPA おすすめ", slug="rpa-bad", monetization_mode="supporting",
            article_type=ArticleType.RECOMMENDATION_ROUNDUP,
            content_subject_keys=["not-a-real-subject"],
        )
    assert session.scalars(select(ArticleContentSubject)).all() == []


def test_articles_without_persisted_subjects_fall_back_to_affiliate_links(
    session: Session,
) -> None:
    """C3 より前に承認された記事 (行が無い) は link から比較対象を作る。"""

    ids = _catalog(session)
    keyword = _keyword(session, "AI 議事録 比較")
    article = Article(title="legacy", slug="legacy-subjects", keyword_id=keyword.id)
    session.add(article)
    session.commit()
    from app.repositories.article_affiliate_program_repository import (
        ArticleAffiliateProgramRepository,
    )

    ArticleAffiliateProgramRepository(session).create(
        article_id=article.id, affiliate_program_id=ids["Krisp"], is_primary=True
    )
    session.commit()

    resolution = ContentSubjectService(session).resolve(article.id)
    assert resolution.source == SOURCE_LEGACY_LINKS
    assert resolution.display_names == ("Krisp",)
    assert resolution.subjects[0].affiliate_program_id == ids["Krisp"]


# ================================================================ snapshot + prompt (E, F, K)
def _snapshot(session: Session, article_id: int):
    return DraftInputSnapshotBuilder(session).build(article_id, now=NOW)


def test_snapshot_carries_the_explicit_type_and_subjects(session: Session) -> None:
    _catalog(session)
    read = _approve(
        session, "RPA おすすめ", slug="rpa-snap", monetization_mode="supporting",
        article_type=ArticleType.RECOMMENDATION_ROUNDUP,
        content_subject_keys=["uipath", "winactor"],
    )
    payload = _snapshot(session, read.id).payload

    assert payload["article"]["article_type"] == "recommendation_roundup"
    assert [s["subject_ref"] for s in payload["content_subjects"]] == ["UiPath", "WinActor"]
    assert all(s["affiliate_program_id"] is None for s in payload["content_subjects"])
    assert payload["monetization"] == {"mode": "supporting", "source": "explicit"}


def test_legacy_snapshots_keep_their_exact_shape(session: Session) -> None:
    """article_type / subject 行が無い記事の payload は C3 以前と同じ 4 キーのまま。"""

    ids = _catalog(session)
    keyword = _keyword(session, "AI 議事録 おすすめ")
    article = Article(title="legacy", slug="legacy-shape", keyword_id=keyword.id)
    session.add(article)
    session.commit()
    from app.repositories.article_affiliate_program_repository import (
        ArticleAffiliateProgramRepository,
    )

    ArticleAffiliateProgramRepository(session).create(
        article_id=article.id, affiliate_program_id=ids["Krisp"], is_primary=True
    )
    session.commit()

    payload = _snapshot(session, article.id).payload
    assert set(payload["article"]) == {"id", "keyword_id", "title", "slug"}
    assert "content_subjects" not in payload
    assert "monetization" not in payload


@pytest.mark.parametrize(
    ("keyword", "slug", "article_type", "expected_template"),
    [
        ("HubSpot 料金", "hubspot-price", ArticleType.PRICING, "article_pricing_v1"),
        ("Make 使い方", "make-howto", ArticleType.HOW_TO, "article_howto_v1"),
        ("AI 議事録 比較", "ai-hikaku", ArticleType.COMPARISON_LISTICLE,
         "article_comparison_v1"),
        ("生成AI ガイドライン", "gai-guideline", ArticleType.INFORMATIONAL,
         "article_informational_v1"),
        ("AI エージェント", "ai-agent", ArticleType.CATEGORY_LANDING, "article_category_v1"),
    ],
)
def test_each_type_renders_its_own_prompt(
    session: Session, keyword: str, slug: str, article_type: ArticleType,
    expected_template: str,
) -> None:
    _catalog(session)
    read = _approve(
        session, keyword, slug=slug, monetization_mode="supporting",
        article_type=article_type, content_subject_keys=["uipath"],
    )
    payload = _snapshot(session, read.id).payload
    package = build_prompt_package(
        snapshot_payload=payload, snapshot_id=1, snapshot_content_hash="0" * 64,
        overrides=EditorialOverridesV1(comparison_set_size=1), now=NOW,
    )
    assert package["template_version"] == expected_template
    prompt = render_prompt(package)
    assert "=== SYSTEM RULES (TRUSTED) ===" in prompt
    # 事実の規律は type によらず共通で残る
    assert "usable_facts に無い値を作らないでください" in prompt


def test_supporting_prompts_are_not_affiliate_text_with_the_primary_removed(
    session: Session,
) -> None:
    """supporting は「primary なし」の断り書きではなく、推薦を目的にしない文面になる。"""

    _catalog(session)
    read = _approve(
        session, "生成AI ガイドライン", slug="gai-guide-2", monetization_mode="supporting",
        article_type=ArticleType.INFORMATIONAL, content_subject_keys=["chatgpt"],
    )
    package = build_prompt_package(
        snapshot_payload=_snapshot(session, read.id).payload, snapshot_id=1,
        snapshot_content_hash="0" * 64,
        overrides=EditorialOverridesV1(comparison_set_size=1), now=NOW,
    )
    prompt = render_prompt(package)

    assert "この記事は supporting content です" in prompt
    assert "購入・申込へ誘導する CTA を置かないでください" in prompt
    assert "PR / 広告表示は不要です" in prompt
    # affiliate 専用の文言は出ない
    assert "PR / 広告（アフィリエイト）である旨の表示を入れます" not in prompt
    assert "primary は CTA 上の候補です" not in prompt


def test_affiliate_prompts_keep_the_disclosure_and_fairness_rules(
    session: Session,
) -> None:
    ids = _catalog(session)
    read = _approve(
        session, "AI 議事録 比較", slug="ai-hikaku-aff", monetization_mode="affiliate",
        primary_affiliate_program_id=ids["Krisp"],
        article_type=ArticleType.COMPARISON_LISTICLE,
    )
    package = build_prompt_package(
        snapshot_payload=_snapshot(session, read.id).payload, snapshot_id=1,
        snapshot_content_hash="0" * 64,
        overrides=EditorialOverridesV1(primary="Krisp", comparison_set_size=1), now=NOW,
    )
    prompt = render_prompt(package)

    assert "PR / 広告（アフィリエイト）である旨の表示を入れます" in prompt
    assert "primary は CTA 上の候補です" in prompt
    assert "この記事は supporting content です" not in prompt


def test_pricing_and_howto_prompts_carry_their_own_discipline(session: Session) -> None:
    _catalog(session)
    pricing = _approve(
        session, "HubSpot 料金", slug="hs-price-2", monetization_mode="supporting",
        article_type=ArticleType.PRICING, content_subject_keys=["uipath"],
    )
    howto = _approve(
        session, "Make 使い方", slug="make-howto-2", monetization_mode="supporting",
        article_type=ArticleType.HOW_TO, content_subject_keys=["uipath"],
    )
    prompts = {}
    for name, read in (("pricing", pricing), ("howto", howto)):
        package = build_prompt_package(
            snapshot_payload=_snapshot(session, read.id).payload, snapshot_id=1,
            snapshot_content_hash="0" * 64,
            overrides=EditorialOverridesV1(comparison_set_size=1), now=NOW,
        )
        prompts[name] = render_prompt(package)

    assert "無料プランなし" in prompts["pricing"]  # 断定を禁じる文脈で言及している
    assert "取得時点（as_of_label）" in prompts["pricing"]
    assert "それらしい偽の手順を作らないでください" in prompts["howto"]
    assert "前提条件" in prompts["howto"]
    assert prompts["pricing"] != prompts["howto"]


def test_fact_pack_uses_the_explicit_article_type_not_the_inference(
    session: Session,
) -> None:
    """C4 canary で見つかった不具合の回帰テスト。

    keyword の推論は comparison_listicle (= 比較対象が必要) でも、人が informational を
    明示したなら比較対象は要らない。推論値で要件を課してはいけない。
    """

    _catalog(session)
    read = _approve(
        session, "AI 議事録 比較", slug="explicit-informational",
        monetization_mode="supporting", article_type=ArticleType.INFORMATIONAL,
    )
    # 推論は comparison_listicle
    assert resolve_article_type(None, "AI 議事録 比較").article_type is (
        ArticleType.COMPARISON_LISTICLE
    )

    readiness = FactPackService(session).build(read.id, now=NOW).readiness

    # 明示した informational が使われるので比較対象は不要 -> subject 0 件でも止まらない
    assert readiness.comparison_subjects_required is False
    assert readiness.comparison_subject_count == 0
    assert readiness.drafting_allowed is True
    assert readiness.blocking_reasons == []


def test_generation_run_records_the_template_it_actually_used(session: Session) -> None:
    """C4.1 で見つかった不具合の回帰テスト。

    run 行の prompt_template_version が roundup 固定だと、pricing / comparison の記事で
    「どの template で生成したか」が追えなくなる。
    """

    from datetime import datetime as _dt

    from app.article.draft_prompt_canonical import (
        compute_prompt_input_hash,
        compute_rendered_prompt_hash,
    )
    from app.services.draft_generation_run_service import DraftGenerationRunService

    ids = _catalog(session)
    read = _approve(
        session, "HubSpot 料金", slug="hs-run-template", monetization_mode="affiliate",
        primary_affiliate_program_id=ids["HubSpot"], article_type=ArticleType.PRICING,
    )
    snapshot = DraftInputSnapshotBuilder(session).build(read.id, now=NOW)
    overrides = EditorialOverridesV1(primary="HubSpot", comparison_set_size=1)
    package = build_prompt_package(
        snapshot_payload=snapshot.payload, snapshot_id=1,
        snapshot_content_hash=snapshot.content_hash, overrides=overrides, now=NOW,
    )
    rendered = render_prompt(package)
    assert package["template_version"] == "article_pricing_v1"

    from app.repositories.draft_input_snapshot_repository import (
        DraftInputSnapshotRepository,
    )

    row = DraftInputSnapshotRepository(session).append(
        article_id=read.id, snapshot_version=snapshot.snapshot_version,
        builder_version=snapshot.builder_version,
        plan_snapshot_origin=snapshot.plan_snapshot_origin,
        primary_affiliate_program_id=snapshot.primary_affiliate_program_id,
        comparison_program_ids=snapshot.comparison_program_ids,
        drafting_allowed_at_freeze=snapshot.drafting_allowed_at_freeze,
        payload=snapshot.payload, content_hash=snapshot.content_hash,
        frozen_at=_dt(2026, 9, 22, tzinfo=UTC),
    )
    session.commit()
    package = build_prompt_package(
        snapshot_payload=row.payload, snapshot_id=row.id,
        snapshot_content_hash=row.content_hash, overrides=overrides, now=NOW,
    )
    rendered = render_prompt(package)

    run, _ = DraftGenerationRunService(session).prepare(
        read.id, snapshot_id=row.id,
        expected_prompt_hash=compute_prompt_input_hash(package),
        expected_rendered_prompt_hash=compute_rendered_prompt_hash(rendered),
        execution_mode="manual", editorial_overrides=overrides, now=NOW,
    )

    assert run.prompt_template_version == "article_pricing_v1"


def test_snapshot_tools_follow_content_subjects_not_affiliate_links(
    session: Session,
) -> None:
    """supporting 記事の editorial subject も tools グリッドに載る。

    tools を affiliate link から組むと、link を持たない supporting 記事は
    subject の fact を 1 件も prompt に載せられず、根拠のない本文を書かせてしまう。
    """
    _catalog(session)
    read = _approve(
        session, "RPA 比較", slug="rpa-tools-grid", monetization_mode="supporting",
        article_type=ArticleType.COMPARISON_LISTICLE,
        content_subject_keys=["uipath", "winactor", "power-automate"],
    )
    payload = _snapshot(session, read.id).payload

    assert [t["subject_ref"] for t in payload["tools"]] == [
        "UiPath", "WinActor", "Power Automate",
    ]
    assert all(t["affiliate_program_id"] is None for t in payload["tools"])
    assert all(t["is_primary"] is False for t in payload["tools"])
    # 対象ごとに fact セルのグリッドが用意される (値は未調査でもキーは揃う)
    assert all(t["cells"] for t in payload["tools"])


def test_snapshot_tools_keep_affiliate_identity_for_linked_subjects(
    session: Session,
) -> None:
    """affiliate 裏付けのある subject は program id と is_primary を保つ。"""
    ids = _catalog(session)
    read = _approve(
        session, "AI 議事録 おすすめ", slug="ai-gijiroku-grid",
        monetization_mode="affiliate", primary_affiliate_program_id=ids["Krisp"],
        article_type=ArticleType.RECOMMENDATION_ROUNDUP,
        content_subject_keys=["notion-ai"],
    )
    payload = _snapshot(session, read.id).payload

    by_ref = {t["subject_ref"]: t for t in payload["tools"]}
    assert by_ref["Krisp"]["affiliate_program_id"] == ids["Krisp"]
    assert by_ref["Krisp"]["is_primary"] is True
    assert by_ref["Notion AI"]["affiliate_program_id"] is None
    assert by_ref["Notion AI"]["is_primary"] is False
