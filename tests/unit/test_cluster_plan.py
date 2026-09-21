"""app/article/cluster_plan.py — cluster 定義の検証 / 意図重複 / 制作キュー (pure)。

DB / HTTP / LLM には一切触れない。
"""

from __future__ import annotations

import copy
import json
import random
import re
from pathlib import Path

import pytest

from app.article.cluster_plan import (
    CHAR_OVERLAP_THRESHOLD,
    DECISION_BLOCKED,
    DECISION_MERGE,
    DECISION_OK,
    TEMPLATE_SCOPE,
    AffiliateMatch,
    ArticleInput,
    ClusterConfigError,
    Intent,
    KeywordInput,
    affiliate_coverage,
    build_content_queue,
    compare_profiles,
    fact_research,
    intent_profile,
    load_cluster_config,
    near_overlap_similarity,
    parse_cluster_config,
    serp_family,
    template_readiness,
)
from app.article.planning import ArticleType
from tests.support.c22_pool import POOL_30 as _POOL_30

_ROOT = Path(__file__).resolve().parents[2]
_TRACKED_CONFIG = _ROOT / "app" / "config" / "content_clusters.json"

_VOCAB = {
    "product_terms": ["Make", "Zapier", "Notion AI", "ChatGPT"],
    "generic_theme_tokens": ["ツール"],
    "theme_aliases": {"法人向け": "法人"},
}


def _cfg(clusters: list[dict], **extra) -> dict:
    return {"version": 1, "clusters": clusters, "vocabulary": _VOCAB, **extra}


def _cluster(cid: str, priority: int, pillar: str, *supporting: str) -> dict:
    return {
        "id": cid,
        "name": f"cluster {cid}",
        "priority": priority,
        "keywords": [{"keyword": pillar, "role": "pillar"}]
        + [{"keyword": s, "role": "supporting"} for s in supporting],
    }


def _kw(kid: int, text: str, score: float | None = None, **kw) -> KeywordInput:
    return KeywordInput(
        id=kid,
        keyword=text,
        status=kw.pop("status", "analyzed"),
        opportunity_score=score,
        **kw,
    )


def _queue(clusters: list[dict], keywords: list[KeywordInput], articles=(), **extra):
    return build_content_queue(parse_cluster_config(_cfg(clusters, **extra)), keywords, articles)


def _by_kw(queue) -> dict:
    return {e.keyword: e for e in (*queue.slots, *queue.merged, *queue.blocked)}


# ============================================================ config validation
def test_tracked_config_parses_with_approved_priority_and_one_pillar_each() -> None:
    config = load_cluster_config(_TRACKED_CONFIG)
    assert [c.id for c in config.clusters] == ["B", "C", "A", "D"]
    assert [c.priority for c in config.clusters] == [1, 2, 3, 4]
    for cluster in config.clusters:
        assert sum(k.role == "pillar" for k in cluster.keywords) == 1
    assert [d.id for d in config.deferred] == ["E"]  # CRM/sales は keyword 拡張まで defer


def test_tracked_config_keywords_are_in_the_current_pool_and_assigned_once() -> None:
    pool = {text for _, text, _, _ in _POOL_30}
    assert len(pool) == 30
    config = load_cluster_config(_TRACKED_CONFIG)
    assigned = [k.keyword for c in config.clusters for k in c.keywords]
    assert set(assigned) <= pool
    assert len(assigned) == len(set(assigned))


def test_parse_accepts_minimal_valid_config() -> None:
    config = parse_cluster_config(_cfg([_cluster("X", 1, "RPA おすすめ", "RPA 導入")]))
    assert config.clusters[0].keywords[0].role == "pillar"
    assert config.vocabulary.product_terms == ("Make", "Zapier", "Notion AI", "ChatGPT")


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (["not-an-object"], "must be a JSON object"),
        ({"version": 2, "clusters": [_cluster("X", 1, "a")]}, "version must be 1"),
        ({"version": 1, "clusters": []}, "clusters must be a non-empty list"),
        ({"version": 1, "clusters": [_cluster("X", 1, "a")], "bogus": 1}, "unknown top-level"),
        (_cfg([_cluster("X", 1, "a"), _cluster("X", 2, "b")]), "duplicate cluster id"),
        (_cfg([_cluster("X", 1, "a"), _cluster("Y", 1, "b")]), "duplicate priority"),
        (_cfg([_cluster("X", 0, "a")]), "priority must be an integer >= 1"),
        (_cfg([{"id": "X", "name": "n", "priority": 1, "keywords": []}]), "non-empty list"),
        (
            _cfg([{"id": "X", "name": "n", "priority": 1, "keywords": [{"keyword": "a"}]}]),
            "invalid role",
        ),
        (
            _cfg(
                [
                    {
                        "id": "X",
                        "name": "n",
                        "priority": 1,
                        "keywords": [{"keyword": "a", "role": "hub"}],
                    }
                ]
            ),
            "invalid role 'hub'",
        ),
        (
            _cfg(
                [
                    {
                        "id": "X",
                        "name": "n",
                        "priority": 1,
                        "keywords": [{"keyword": "a", "role": "supporting"}],
                    }
                ]
            ),
            "exactly one pillar (found 0)",
        ),
        (
            _cfg(
                [
                    {
                        "id": "X",
                        "name": "n",
                        "priority": 1,
                        "keywords": [
                            {"keyword": "a", "role": "pillar"},
                            {"keyword": "b", "role": "pillar"},
                        ],
                    }
                ]
            ),
            "exactly one pillar (found 2)",
        ),
        (
            _cfg(
                [
                    {
                        "id": "X",
                        "name": "n",
                        "priority": 1,
                        "keywords": [{"keyword": "a", "role": "pillar", "extra": 1}],
                    }
                ]
            ),
            "unknown keys",
        ),
        (
            _cfg(
                [_cluster("X", 1, "a")], deferred_clusters=[{"id": "X", "name": "n", "reason": "r"}]
            ),
            "collides with an active cluster",
        ),
        (_cfg([_cluster("X", 1, "a")], deferred_clusters=[{"id": "E"}]), "needs non-empty"),
    ],
)
def test_parse_rejects_invalid_config(raw, fragment: str) -> None:
    with pytest.raises(ClusterConfigError) as exc:
        parse_cluster_config(raw)
    assert fragment in str(exc.value)


def test_parse_rejects_duplicate_keyword_within_and_across_clusters() -> None:
    within = _cfg([_cluster("X", 1, "RPA おすすめ", "RPA おすすめ")])
    with pytest.raises(ClusterConfigError, match="twice in the same cluster"):
        parse_cluster_config(within)

    # 正規化 (NFKC / casefold / 空白) 後に同一なら 1 keyword = 1 cluster 違反。
    across = _cfg([_cluster("X", 1, "AI 議事録"), _cluster("Y", 2, "ａｉ　議事録")])
    with pytest.raises(ClusterConfigError, match="in clusters 'X' and 'Y'"):
        parse_cluster_config(across)


def test_parse_reports_all_errors_together() -> None:
    raw = _cfg([_cluster("X", 1, "a"), _cluster("X", 1, "b")])
    with pytest.raises(ClusterConfigError) as exc:
        parse_cluster_config(raw)
    assert len(exc.value.errors) >= 2


@pytest.mark.parametrize(
    "vocabulary",
    [
        "nope",
        {"product_terms": "Make"},
        {"generic_theme_tokens": [1]},
        {"theme_aliases": {"a": 1}},
        {"unknown": []},
    ],
)
def test_parse_rejects_bad_vocabulary(vocabulary) -> None:
    raw = {"version": 1, "clusters": [_cluster("X", 1, "a")], "vocabulary": vocabulary}
    with pytest.raises(ClusterConfigError):
        parse_cluster_config(raw)


def test_load_reports_missing_and_invalid_json(tmp_path: Path) -> None:
    with pytest.raises(ClusterConfigError, match="not found"):
        load_cluster_config(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ClusterConfigError, match="not readable JSON"):
        load_cluster_config(bad)


def test_unknown_keyword_is_rejected_against_the_pool() -> None:
    clusters = [_cluster("X", 1, "RPA おすすめ", "RPA 存在しない")]
    with pytest.raises(ClusterConfigError, match="unknown keyword 'RPA 存在しない'"):
        _queue(clusters, [_kw(1, "RPA おすすめ")])


def test_ambiguous_keyword_in_pool_is_rejected() -> None:
    with pytest.raises(ClusterConfigError, match="ambiguous keyword"):
        _queue(
            [_cluster("X", 1, "AI 議事録")],
            [_kw(1, "AI 議事録"), _kw(2, "ai 議事録")],
        )


# ==================================================================== intents
def _profile(text: str):
    return intent_profile(text, parse_cluster_config(_cfg([_cluster("X", 1, "a")])).vocabulary)


def test_intent_profile_strips_modifiers_and_generic_tokens() -> None:
    p = _profile("業務効率化 ツール おすすめ")
    assert p.theme_tokens == frozenset({"業務効率化"})
    assert p.intent is Intent.SELECT and p.family == "selection"
    assert _profile("業務効率化 ツール 比較").intent is Intent.COMPARE
    assert _profile("AI 議事録").intent is Intent.HEAD


def test_intent_profile_applies_aliases_and_intent_priority() -> None:
    assert _profile("法人向け 生成AI").theme_tokens == _profile("生成AI 法人").theme_tokens
    # 複数 modifier: HOWTO > COMPARE > SELECT > PRICING > FREE > DEFINITION
    assert _profile("RPA 導入 おすすめ").intent is Intent.HOWTO
    assert _profile("RPA 比較 おすすめ").intent is Intent.COMPARE
    assert _profile("RPA 料金 無料").intent is Intent.PRICING


def test_intent_profile_marks_product_specific_keywords() -> None:
    p = _profile("Make 料金")
    assert p.product_specific and p.family == "product_plan"
    assert p.product_key == frozenset({"make"})
    multi = _profile("Notion AI 料金")
    assert multi.product_key == frozenset({"notion ai"})
    assert not _profile("RPA 比較").product_specific
    # ASCII 境界を尊重: "maker" は Make ではない
    assert not _profile("maker 料金").product_specific


def test_intent_profile_splits_glued_japanese_modifier_only() -> None:
    assert _profile("ChatGPT料金").intent is Intent.PRICING
    assert _profile("間違い").intent is Intent.HEAD  # stem が短すぎる語は分割しない


def test_serp_family_grouping() -> None:
    for intent in (Intent.SELECT, Intent.COMPARE, Intent.PRICING, Intent.HEAD):
        assert serp_family(intent, product_specific=False) == "selection"
    assert serp_family(Intent.FREE, product_specific=False) == "free"
    assert serp_family(Intent.HOWTO, product_specific=False) == "howto"
    assert serp_family(Intent.DEFINITION, product_specific=True) == "definition"
    assert serp_family(Intent.FREE, product_specific=True) == "product_plan"


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("AI 議事録 おすすめ", "AI 議事録 比較", True),  # recommendation vs comparison
        ("AI 議事録 おすすめ", "AI 議事録 料金", True),  # generic pricing shares the SERP
        ("AI 議事録 おすすめ", "AI 議事録", True),  # head term
        ("Make 料金", "Make 無料", True),  # 同一製品の plan/pricing
        ("Make 料金", "Zapier 料金", False),  # 別製品
        ("AI 議事録 おすすめ", "AI 議事録 無料", False),  # 無料 は別 family
        ("AI 議事録 おすすめ", "AI 議事録 使い方", False),  # how-to は別 family
        ("AI 議事録 おすすめ", "AI 議事録 とは", False),  # definition は別 family
        ("AI 議事録 おすすめ", "RPA おすすめ", False),  # 別 theme
        ("RPA おすすめ", "Make 料金", False),  # product_specific が異なる
        ("業務効率化 ツール おすすめ", "AI 業務効率化", False),  # theme が異なる
    ],
)
def test_compare_profiles_intent_overlap(a: str, b: str, expected: bool) -> None:
    assert (compare_profiles(_profile(a), _profile(b)) is not None) is expected


def test_compare_profiles_char_overlap_is_a_secondary_safety_net() -> None:
    overlap = compare_profiles(_profile("chatbot おすすめ"), _profile("chatbots 比較"))
    assert overlap is not None and overlap.kind == "char"
    assert overlap.similarity >= CHAR_OVERLAP_THRESHOLD


def test_modifier_only_keywords_never_overlap() -> None:
    assert compare_profiles(_profile("おすすめ"), _profile("おすすめ")) is None


def test_near_overlap_is_information_only() -> None:
    sim = near_overlap_similarity(_profile("法人向け 生成AI"), _profile("生成AI ツール おすすめ"))
    assert sim is not None and 0.6 <= sim < CHAR_OVERLAP_THRESHOLD
    assert compare_profiles(_profile("法人向け 生成AI"), _profile("生成AI ツール おすすめ")) is None
    assert near_overlap_similarity(_profile("Make 料金"), _profile("Zapier 料金")) is None


# ==================================================================== queue
_POOL = [
    _kw(1, "業務効率化 ツール おすすめ", 68.81, components={"search_demand": 29.83}),
    _kw(2, "業務効率化 ツール 比較"),
    _kw(3, "業務効率化 ツール 無料", 60.39),
    _kw(4, "RPA おすすめ", 50.94),
    _kw(5, "RPA 比較", 50.47),
    _kw(6, "RPA 導入", 50.54),
    _kw(7, "Make 料金"),
    _kw(8, "Zapier 料金", 54.19),
    _kw(9, "ChatGPT 料金", 53.13),
]
_CLUSTERS = [
    _cluster(
        "B", 1, "業務効率化 ツール おすすめ", "業務効率化 ツール 比較", "業務効率化 ツール 無料"
    ),
    _cluster("C", 2, "RPA おすすめ", "RPA 比較", "RPA 導入", "Make 料金", "Zapier 料金"),
]
_ARTICLE_1 = ArticleInput(
    id=1,
    keyword_id=1,
    keyword="業務効率化 ツール おすすめ",
    title="t",
    status="published",
    fact_count=91,
)


def test_decisions_ok_merge_blocked() -> None:
    queue = _queue(_CLUSTERS, _POOL, [_ARTICLE_1])
    by = _by_kw(queue)

    assert by["業務効率化 ツール おすすめ"].decision == DECISION_BLOCKED
    assert by["業務効率化 ツール おすすめ"].reason_code == "already_has_article"
    assert by["業務効率化 ツール おすすめ"].existing_article_id == 1
    assert by["業務効率化 ツール 比較"].decision == DECISION_BLOCKED
    assert by["業務効率化 ツール 比較"].reason_code == "overlaps_existing_article"
    assert by["業務効率化 ツール 比較"].overlaps_article_id == 1
    assert by["業務効率化 ツール 無料"].decision == DECISION_OK  # 別 family
    assert by["RPA おすすめ"].decision == DECISION_OK
    assert by["RPA 比較"].decision == DECISION_MERGE
    assert by["RPA 比較"].merge_target_keyword == "RPA おすすめ"
    assert by["RPA 比較"].merge_group == by["RPA おすすめ"].merge_group == "mg-4"
    assert by["RPA おすすめ"].absorbed_keywords == ("RPA 比較",)
    assert by["RPA 導入"].decision == DECISION_OK
    assert by["Make 料金"].decision == DECISION_OK
    assert by["Zapier 料金"].decision == DECISION_OK  # 別製品の料金は別 slot
    assert {e.keyword for e in queue.unassigned} == {"ChatGPT 料金"}
    assert queue.summary["new_slots"] == 5
    assert queue.summary["merged"] == 1
    assert queue.summary["blocked"] == 2


@pytest.mark.parametrize(
    "status", ["idea", "planned", "drafting", "review", "approved", "published", "rewrite"]
)
def test_every_non_archived_in_flight_article_blocks_an_overlapping_intent(status: str) -> None:
    article = ArticleInput(
        id=7, keyword_id=1, keyword="業務効率化 ツール おすすめ", title="t", status=status
    )
    queue = _queue(_CLUSTERS, _POOL, [article])
    by = _by_kw(queue)
    assert by["業務効率化 ツール 比較"].decision == DECISION_BLOCKED
    assert by["業務効率化 ツール 比較"].overlaps_article_status == status
    assert by["業務効率化 ツール おすすめ"].existing_article_status == status


def test_a_different_intent_family_is_not_blocked_by_an_existing_article() -> None:
    queue = _queue(_CLUSTERS, _POOL, [_ARTICLE_1])
    assert _by_kw(queue)["業務効率化 ツール 無料"].decision == DECISION_OK


def test_rejected_keyword_is_blocked() -> None:
    pool = [_kw(1, "RPA おすすめ"), _kw(2, "RPA 導入", status="rejected")]
    queue = _queue([_cluster("C", 1, "RPA おすすめ", "RPA 導入")], pool)
    assert _by_kw(queue)["RPA 導入"].reason_code == "keyword_rejected"


def test_queued_keyword_overlap_merges_across_clusters_into_the_pillar() -> None:
    clusters = [
        _cluster("B", 1, "業務効率化 ツール おすすめ", "RPA 比較"),
        _cluster("C", 2, "RPA おすすめ"),
    ]
    pool = [_kw(1, "業務効率化 ツール おすすめ"), _kw(2, "RPA 比較", 99.0), _kw(3, "RPA おすすめ")]
    by = _by_kw(_queue(clusters, pool))
    # pillar は supporting より先に anchor になる (score が高くても cluster 優先度が高くても)。
    assert by["RPA おすすめ"].decision == DECISION_OK
    assert by["RPA 比較"].decision == DECISION_MERGE
    assert by["RPA 比較"].merge_target_keyword == "RPA おすすめ"
    assert by["RPA 比較"].cluster_id == "B"  # cross-cluster merge


def test_merge_is_anchor_based_and_never_chains() -> None:
    a = _profile("chatbot おすすめ")
    b = _profile("chatbots おすすめ")
    c = _profile("chatbotss おすすめ")
    assert compare_profiles(a, b) is not None
    assert compare_profiles(b, c) is not None
    assert compare_profiles(a, c) is None  # 前提: a と c は直接は重ならない

    pool = [
        _kw(1, "chatbot おすすめ", 90.0),
        _kw(2, "chatbots おすすめ", 80.0),
        _kw(3, "chatbotss おすすめ", 70.0),
    ]
    clusters = [_cluster("X", 1, "chatbot おすすめ", "chatbots おすすめ", "chatbotss おすすめ")]
    by = _by_kw(_queue(clusters, pool))
    assert by["chatbots おすすめ"].decision == DECISION_MERGE
    assert by["chatbotss おすすめ"].decision == DECISION_OK  # b は anchor ではないので併合されない


def test_char_similarity_alone_does_not_merge_different_families() -> None:
    pool = [_kw(1, "chatbot おすすめ"), _kw(2, "chatbots 無料")]
    by = _by_kw(_queue([_cluster("X", 1, "chatbot おすすめ", "chatbots 無料")], pool))
    assert by["chatbots 無料"].decision == DECISION_OK


def test_article_without_keyword_warns_and_does_not_crash() -> None:
    orphan = ArticleInput(id=3, keyword_id=None, keyword=None, title="孤立", status="planned")
    queue = _queue(_CLUSTERS, _POOL, [orphan])
    assert any("article #3" in w and "no keyword" in w for w in queue.warnings)


def test_queue_order_is_cluster_then_role_then_score() -> None:
    clusters = [
        _cluster("C", 2, "RPA おすすめ", "RPA 導入", "Make 料金", "Zapier 料金"),
        _cluster("B", 1, "業務効率化 ツール 無料", "AI 業務効率化"),
    ]
    pool = [
        _kw(1, "業務効率化 ツール 無料", 10.0),
        _kw(2, "AI 業務効率化", 90.0),
        _kw(3, "RPA おすすめ"),  # 未スコアでも pillar が先
        _kw(4, "RPA 導入", 50.0),
        _kw(5, "Make 料金", 70.0),
        _kw(6, "Zapier 料金", 70.0),  # score 同点 -> keyword id 昇順
    ]
    queue = _queue(clusters, pool)
    assert [e.keyword for e in queue.slots] == [
        "業務効率化 ツール 無料",  # cluster B (priority 1) の pillar
        "AI 業務効率化",
        "RPA おすすめ",  # cluster C の pillar は未スコアでも先頭
        "Make 料金",
        "Zapier 料金",
        "RPA 導入",
    ]
    assert [e.position for e in queue.slots] == [1, 2, 3, 4, 5, 6]


def test_scored_supporting_keywords_precede_unscored_ones() -> None:
    clusters = [_cluster("C", 1, "RPA おすすめ", "RPA 導入", "Make 料金", "Zapier 料金")]
    pool = [
        _kw(1, "RPA おすすめ", 1.0),
        _kw(2, "RPA 導入"),
        _kw(3, "Make 料金", 5.0),
        _kw(4, "Zapier 料金", 9.0),
    ]
    order = [e.keyword for e in _queue(clusters, pool).slots]
    assert order == ["RPA おすすめ", "Zapier 料金", "Make 料金", "RPA 導入"]


def test_only_ok_slots_have_positions() -> None:
    queue = _queue(_CLUSTERS, _POOL, [_ARTICLE_1])
    assert all(e.position is not None for e in queue.slots)
    assert all(e.position is None for e in (*queue.merged, *queue.blocked))


def test_queue_is_deterministic_for_any_input_ordering() -> None:
    baseline = _queue(_CLUSTERS, _POOL, [_ARTICLE_1]).to_dict()
    rng = random.Random(20260922)
    for _ in range(10):
        pool = list(_POOL)
        rng.shuffle(pool)
        clusters = copy.deepcopy(_CLUSTERS)
        rng.shuffle(clusters)
        for cluster in clusters:
            rng.shuffle(cluster["keywords"])
        assert _queue(clusters, pool, [_ARTICLE_1]).to_dict() == baseline


def test_queue_output_is_json_serialisable() -> None:
    payload = _queue(_CLUSTERS, _POOL, [_ARTICLE_1]).to_dict()
    decoded = json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    assert set(decoded) >= {"slots", "merged", "blocked", "unassigned", "summary"}
    assert decoded["summary"]["new_slots"] == len(decoded["slots"])


def test_entries_report_score_components_prerequisites_and_notes() -> None:
    pool = [
        _kw(
            1,
            "RPA おすすめ",
            50.0,
            components={"search_demand": 46.49, "competition_ease": 98.0},
        ),
        _kw(2, "RPA 導入", missing_components=("trend", "competition_ease")),
        _kw(3, "Make 料金"),
    ]
    queue = _queue([_cluster("C", 1, "RPA おすすめ", "RPA 導入", "Make 料金")], pool)
    by = _by_kw(queue)
    assert by["RPA おすすめ"].components == {"search_demand": 46.49, "competition_ease": 98.0}
    assert by["RPA おすすめ"].opportunity_score == 50.0
    assert by["RPA 導入"].prerequisites[0] == "signals_incomplete:competition_ease|trend"
    assert "no_prompt_template:how_to" in by["RPA 導入"].prerequisites
    assert by["Make 料金"].prerequisites[0] == "unscored"
    assert "article_type_unclassified" in by["Make 料金"].prerequisites
    assert "no_affiliate_match" in by["RPA おすすめ"].notes


def test_near_overlaps_are_listed_but_do_not_change_decisions() -> None:
    pool = [_kw(1, "法人向け 生成AI"), _kw(2, "生成AI ツール おすすめ")]
    queue = _queue([_cluster("D", 1, "法人向け 生成AI", "生成AI ツール おすすめ")], pool)
    by = _by_kw(queue)
    assert by["法人向け 生成AI"].decision == by["生成AI ツール おすすめ"].decision == DECISION_OK
    assert any(
        n.startswith("near_overlap:生成AI ツール おすすめ") for n in by["法人向け 生成AI"].notes
    )


def test_affiliate_coverage_levels_and_providers() -> None:
    assert affiliate_coverage(()).level == "none"
    one = affiliate_coverage([AffiliateMatch(2, "Zed", "make")])
    assert (one.level, one.program_count, one.providers) == ("single", 1, ("make",))
    many = affiliate_coverage(
        [
            AffiliateMatch(3, "Zed", "impact"),
            AffiliateMatch(1, "Alpha", None),
            AffiliateMatch(2, "Mid", "impact"),
        ]
    )
    assert many.level == "multiple"
    assert many.program_names == ("Alpha", "Mid", "Zed")
    assert many.providers == ("impact",)

    pool = [_kw(1, "RPA おすすめ", affiliate_matches=(AffiliateMatch(9, "UiPath", "direct"),))]
    entry = _queue([_cluster("C", 1, "RPA おすすめ")], pool).slots[0]
    assert entry.affiliate.level == "single"
    assert "no_affiliate_match" not in entry.notes
    assert entry.fact_research.suggested_subjects == ("UiPath",)


def test_template_readiness_reflects_the_single_roundup_template() -> None:
    ready = template_readiness(ArticleType.RECOMMENDATION_ROUNDUP)
    assert ready.ready and ready.template_version == "article_roundup_v1"
    for other in (
        ArticleType.COMPARISON_LISTICLE,
        ArticleType.HOW_TO,
        ArticleType.CATEGORY_LANDING,
    ):
        result = template_readiness(other)
        assert not result.ready and other.value in result.reason
    assert template_readiness(None).reason == "article_type_unclassified"


def test_template_scope_matches_the_real_prompt_template_registry() -> None:
    from app.article.draft_prompt_render import _TEMPLATES
    from app.models.draft_generation_run import PROMPT_TEMPLATE_VERSION

    assert set(TEMPLATE_SCOPE) == set(_TEMPLATES)
    assert PROMPT_TEMPLATE_VERSION in TEMPLATE_SCOPE


def test_fact_research_requirements_by_article_type() -> None:
    roundup = fact_research(ArticleType.RECOMMENDATION_ROUNDUP, existing_fact_count=0, subjects=())
    assert (roundup.requirement, roundup.status, roundup.required) == (
        "tool_facts",
        "missing",
        True,
    )
    assert (
        fact_research(ArticleType.COMPARISON_LISTICLE, existing_fact_count=3, subjects=()).status
        == "present"
    )
    assert (
        fact_research(ArticleType.HOW_TO, existing_fact_count=0, subjects=()).requirement
        == "official_sources"
    )
    assert fact_research(None, existing_fact_count=0, subjects=()).requirement == "unknown_type"


def test_existing_article_facts_are_reported_on_its_keyword() -> None:
    queue = _queue(_CLUSTERS, _POOL, [_ARTICLE_1])
    entry = _by_kw(queue)["業務効率化 ツール おすすめ"]
    assert entry.fact_research.existing_fact_count == 91
    assert entry.fact_research.status == "present"


def test_unassigned_and_deferred_are_reported() -> None:
    queue = _queue(
        _CLUSTERS,
        _POOL,
        [_ARTICLE_1],
        deferred_clusters=[{"id": "E", "name": "CRM", "reason": "later"}],
    )
    assert [u.keyword for u in queue.unassigned] == ["ChatGPT 料金"]
    assert queue.unassigned[0].opportunity_score == 53.13
    assert [d.id for d in queue.deferred] == ["E"]
    assert queue.summary["unassigned_keywords"] == 1
    assert queue.summary["by_cluster"]["C"] == {"new_slots": 4, "merged": 1, "blocked": 0}


# ------------------------------------------------ approved map on the tracked pool
def _pool_30() -> list[KeywordInput]:
    return [
        KeywordInput(
            id=kid,
            keyword=text,
            status="analyzed",
            opportunity_score=score,
            missing_components=tuple(missing.split("|")) if missing else (),
        )
        for kid, text, score, missing in _POOL_30
    ]


def test_approved_cluster_map_on_the_current_pool_is_defensible_not_forced() -> None:
    config = load_cluster_config(_TRACKED_CONFIG)
    article = ArticleInput(
        id=1, keyword_id=21, keyword="業務効率化 ツール おすすめ", title="t", status="published"
    )
    queue = build_content_queue(config, _pool_30(), [article])

    assert 12 <= queue.summary["new_slots"] <= 16  # 20-30 本を pool から無理に作らない
    assert {e.keyword for e in queue.blocked} == {
        "業務効率化 ツール おすすめ",
        "業務効率化 ツール 比較",
    }
    assert {e.keyword for e in queue.merged} == {
        "RPA 比較",
        "AI 議事録",
        "AI 議事録 比較",
        "AI 議事録 料金",
        "生成AI ツール 比較",
    }
    assert {e.keyword for e in queue.unassigned} == {
        "ChatGPT とは",
        "ChatGPT 使い方",
        "ChatGPT 無料",
        "ChatGPT 料金",
        "ChatGPT Plus 料金",
        "生成AI とは",
        "生成AI 無料",
    }
    # 承認済み優先順: B -> C -> A -> D
    cluster_order = [e.cluster_id for e in queue.slots]
    assert cluster_order == sorted(cluster_order, key="BCAD".index)
    # 各 cluster の pillar は同 cluster 内で最初に並ぶ (既に article がある B を除く)
    for cid in "CAD":
        first = next(e for e in queue.slots if e.cluster_id == cid)
        assert first.role == "pillar"


def test_source_modules_have_no_write_network_or_llm_imports() -> None:
    forbidden_imports = re.compile(
        r"^\s*(?:import|from)\s+(httpx|requests|urllib\.request|socket|openai|anthropic|aiohttp)\b",
        re.MULTILINE,
    )
    forbidden_calls = re.compile(r"session\.(commit|add|add_all|delete|merge|flush|execute)\(")
    for rel in (
        "app/article/cluster_plan.py",
        "app/services/content_queue_service.py",
        "scripts/plan_content_clusters.py",
    ):
        source = (_ROOT / rel).read_text(encoding="utf-8")
        assert not forbidden_imports.search(source), rel
        assert not forbidden_calls.search(source), rel


# ==========================================================================
# Phase C2.4: product qualifiers / new intent families / aliases / optional qualifiers
# ==========================================================================
_TRACKED_VOCAB = load_cluster_config(_TRACKED_CONFIG).vocabulary


def _tp(text: str):
    """追跡済み cluster config の vocabulary で profile を作る。"""

    return intent_profile(text, _TRACKED_VOCAB)


def test_clickup_pricing_and_alternatives_are_distinct_articles_regression() -> None:
    pricing, alternatives = _tp("ClickUp 料金"), _tp("ClickUp 代替")
    assert pricing.family == "product_plan" and alternatives.family == "alternatives"
    assert compare_profiles(pricing, alternatives) is None
    # 同じ製品でも how-to / 日本語対応 / 事例 も別 SERP
    for other in ("ClickUp 使い方", "ClickUp 日本語", "ClickUp 導入事例", "ClickUp 補助金"):
        assert compare_profiles(pricing, _tp(other)) is None
    # 料金と無料は同じ製品プラン SERP なので重複
    assert compare_profiles(pricing, _tp("ClickUp 無料")) is not None


def test_product_overlap_compares_residual_qualifiers_not_only_the_product_name() -> None:
    plain = _tp("Make 料金")
    qualified = _tp("Make セルフホスト 料金")
    assert plain.residual_tokens == frozenset() and qualified.residual_tokens == {"セルフホスト"}
    assert compare_profiles(plain, qualified) is None
    assert compare_profiles(_tp("Make 料金"), _tp("Make 無料")) is not None


@pytest.mark.parametrize(
    ("text", "intent", "family"),
    [
        ("Zapier 代替", Intent.ALTERNATIVE, "alternatives"),
        ("Zapier 乗り換え", Intent.ALTERNATIVE, "alternatives"),
        ("業務効率化 ツール 事例", Intent.CASE_STUDY, "cases"),
        ("生成AI 導入事例", Intent.CASE_STUDY, "cases"),
        ("Make 日本語", Intent.JP_SUPPORT, "jp_support"),
        ("生成AI 導入 補助金", Intent.SUBSIDY, "subsidy"),
        ("SFA CRM 違い", Intent.DIFFERENCE, "difference"),
    ],
)
def test_new_intent_families(text: str, intent: Intent, family: str) -> None:
    profile = _tp(text)
    assert profile.intent is intent and profile.family == family


def test_new_intents_win_over_howto_and_pricing_modifiers() -> None:
    assert _tp("生成AI 導入 事例").intent is Intent.CASE_STUDY  # 導入 (howto) より事例
    assert _tp("生成AI 導入 補助金").intent is Intent.SUBSIDY
    assert _tp("Make 使い方 日本語").intent is Intent.JP_SUPPORT
    assert _tp("HubSpot 料金 代替").intent is Intent.ALTERNATIVE


def test_new_intent_families_do_not_collide_with_the_roundup_family() -> None:
    roundup = _tp("業務効率化 ツール おすすめ")
    for other in ("業務効率化 ツール 事例", "業務効率化 ツール 補助金", "業務効率化 ツール 代替"):
        assert compare_profiles(roundup, _tp(other)) is None
    assert compare_profiles(_tp("業務効率化 ツール 事例"), _tp("業務効率化 事例")) is not None


def test_head_to_head_comparison_is_covered_by_a_product_alternatives_page() -> None:
    versus = _tp("Zapier Make 比較")
    assert versus.family == "alternatives" and versus.product_key == {"zapier", "make"}
    assert compare_profiles(_tp("Zapier 代替"), versus) is not None  # 包含される
    assert compare_profiles(versus, _tp("Zapier 代替")) is not None  # 対称
    assert compare_profiles(_tp("Make n8n 比較"), versus) is None  # 別の組み合わせ
    assert compare_profiles(_tp("Make n8n 比較"), _tp("Zapier 代替")) is None
    assert _tp("Make n8n 違い").family == "difference"  # 違い は別 SERP


def test_longest_product_term_wins() -> None:
    assert _tp("Notion AI 料金").product_key == frozenset({"notion ai"})
    assert _tp("Notion 使い方").product_key == frozenset({"notion"})
    assert _tp("Microsoft Copilot 法人 料金").product_key == frozenset({"microsoft copilot"})
    assert _tp("Gemini for Workspace 料金").product_key == frozenset({"gemini for workspace"})


def test_aliases_map_synonyms_and_multi_token_phrases() -> None:
    assert _tp("顧客管理 ツール 比較").theme_tokens == frozenset({"crm"})
    assert _tp("SFA おすすめ").theme_tokens == frozenset({"crm"})
    assert _tp("営業 管理 ツール").theme_tokens == frozenset({"crm"})  # 複数 token の alias
    assert _tp("MA ツール 比較").theme_tokens == frozenset({"マーケティングオートメーション"})
    assert _tp("生成AI 社内 導入").theme_tokens == _tp("生成AI 法人 導入").theme_tokens
    assert _tp("生成AI ガイドライン 企業").theme_tokens == _tp("生成AI 社内 ルール").theme_tokens
    assert _tp("AI 議事録").theme_tokens == _tp("議事録 ツール").theme_tokens == {"議事録"}
    assert _tp("議事録 AI 精度 比較").theme_tokens == frozenset({"議事録"})


def test_alias_hits_prefer_the_canonical_spelling() -> None:
    assert _tp("CRM おすすめ").alias_hits == 0
    assert _tp("SFA おすすめ").alias_hits == 1
    assert _tp("生成AI ガイドライン 企業").alias_hits == 2
    assert compare_profiles(_tp("CRM おすすめ"), _tp("SFA おすすめ")) is not None


def test_optional_qualifiers_are_dropped_only_when_other_tokens_remain() -> None:
    assert _tp("RPA 中小企業").theme_tokens == frozenset({"rpa"})
    assert _tp("テレワーク 業務効率化 ツール").theme_tokens == frozenset({"業務効率化"})
    assert _tp("ノーコード 業務自動化").theme_tokens == frozenset({"業務自動化"})
    assert _tp("中小企業 おすすめ").theme_tokens == frozenset({"中小企業"})  # theme を空にしない
    assert compare_profiles(_tp("中小企業 CRM"), _tp("CRM おすすめ")) is not None


def test_ai_is_not_a_global_optional_qualifier() -> None:
    """AI 業務効率化 は 業務効率化 ツール おすすめ (article #1) と別 intent のまま。"""

    assert compare_profiles(_tp("AI 業務効率化"), _tp("業務効率化 ツール おすすめ")) is None


def test_vocabulary_accepts_and_validates_optional_qualifiers() -> None:
    raw = {
        "version": 1,
        "clusters": [_cluster("X", 1, "a")],
        "vocabulary": {"optional_qualifiers": ["中小企業"]},
    }
    assert parse_cluster_config(raw).vocabulary.optional_qualifiers == ("中小企業",)
    raw["vocabulary"] = {"optional_qualifiers": "中小企業"}
    with pytest.raises(ClusterConfigError):
        parse_cluster_config(raw)


def test_tracked_vocabulary_covers_the_c23_needs() -> None:
    vocab = _TRACKED_VOCAB
    for term in ("ClickUp", "Fireflies", "HubSpot", "n8n", "Zoom", "Teams", "Salesforce"):
        assert term in vocab.product_terms
    aliases = dict(vocab.theme_aliases)
    for key in ("顧客管理", "sfa", "社内", "ai 議事録", "営業 管理", "ma"):
        assert key in aliases
    for qualifier in ("中小企業", "テレワーク", "ノーコード", "精度"):
        assert qualifier in vocab.optional_qualifiers


# ==========================================================================
# C2.5.4: strong / weak tier の報告 (追加のみ・順序 / 判定は不変)
# ==========================================================================
def test_coverage_tier_fields_are_none_when_matches_carry_no_tier_data() -> None:
    cov = affiliate_coverage([AffiliateMatch(1, "Alpha", "direct")])  # legacy の呼び出し
    assert (cov.level, cov.program_count) == ("single", 1)
    assert cov.strong_program_count is None and cov.weak_program_count is None
    assert cov.no_strong_affiliate_match is None
    assert cov.strong_program_names == () and cov.weak_program_names == ()


def test_coverage_with_no_matches_reports_no_strong_match() -> None:
    cov = affiliate_coverage(())
    assert (cov.level, cov.program_count) == ("none", 0)
    assert (cov.strong_program_count, cov.weak_program_count) == (0, 0)
    assert cov.no_strong_affiliate_match is True


def test_coverage_counts_tiers_while_the_legacy_fields_stay_the_same() -> None:
    matches = [
        AffiliateMatch(1, "Alpha", "direct", tier="strong"),
        AffiliateMatch(2, "Mid", "impact", tier="weak"),
        AffiliateMatch(3, "Zed", "impact", tier="weak"),
    ]
    cov = affiliate_coverage(matches)
    assert (cov.level, cov.program_count, cov.program_names) == (
        "multiple",
        3,
        ("Alpha", "Mid", "Zed"),
    )
    assert cov.providers == ("direct", "impact")
    assert (cov.strong_program_count, cov.weak_program_count) == (1, 2)
    assert cov.strong_program_names == ("Alpha",) and cov.weak_program_names == ("Mid", "Zed")
    assert cov.no_strong_affiliate_match is False
    # tier の有無で legacy の 4 項目は同一
    plain = affiliate_coverage([AffiliateMatch(m.program_id, m.name, m.provider) for m in matches])
    assert (plain.level, plain.program_count, plain.providers, plain.program_names) == (
        cov.level,
        cov.program_count,
        cov.providers,
        cov.program_names,
    )


def test_alias_only_strong_match_is_counted_as_strong_but_not_as_legacy_covered() -> None:
    matches = [AffiliateMatch(7, "HubSpot", "impact", tier="strong", legacy=False)]
    cov = affiliate_coverage(matches)
    assert (cov.level, cov.program_count, cov.program_names, cov.providers) == ("none", 0, (), ())
    assert cov.strong_program_count == 1 and cov.strong_program_names == ("HubSpot",)
    assert cov.alias_only_strong_program_names == ("HubSpot",)  # 明示的に識別できる
    assert cov.no_strong_affiliate_match is False
    # legacy の no_affiliate_match の意味 (level none) は変わらない
    entry = _queue(
        [_cluster("C", 1, "RPA おすすめ")],
        [_kw(1, "RPA おすすめ", affiliate_matches=tuple(matches))],
    ).slots[0]
    assert "no_affiliate_match" in entry.notes and "no_strong_affiliate_match" not in entry.notes
    assert entry.fact_research.suggested_subjects == ()  # 既存の subjects は legacy の covered だけ


def test_queue_notes_report_no_strong_match_without_changing_order_or_decisions() -> None:
    clusters = [_cluster("C", 1, "RPA おすすめ", "RPA 導入")]
    weak = (AffiliateMatch(9, "UiPath", "direct", tier="weak"),)
    strong = (AffiliateMatch(9, "UiPath", "direct", tier="strong"),)

    def build(matches):
        pool = [
            _kw(1, "RPA おすすめ", score=60.0, affiliate_matches=matches),
            _kw(2, "RPA 導入", score=70.0, affiliate_matches=matches),
        ]
        return _queue(clusters, pool)

    plain = build((AffiliateMatch(9, "UiPath", "direct"),))
    for matches, expect_note in ((weak, True), (strong, False)):
        q = build(matches)
        assert [e.keyword for e in q.slots] == [e.keyword for e in plain.slots]
        assert [(e.position, e.decision, e.reason_code) for e in q.slots] == [
            (e.position, e.decision, e.reason_code) for e in plain.slots
        ]
        assert [e.keyword for e in q.merged] == [e.keyword for e in plain.merged]
        assert all(("no_strong_affiliate_match" in e.notes) is expect_note for e in q.slots)
        assert q.slots[0].affiliate.level == plain.slots[0].affiliate.level == "single"
    assert all(
        "no_strong_affiliate_match" not in e.notes for e in plain.slots
    )  # tier 未算出は note なし


def test_affiliate_match_positional_construction_still_works_and_defaults_are_legacy() -> None:
    m = AffiliateMatch(1, "Alpha", None)
    assert (m.tier, m.legacy) == (None, True)


def test_alias_only_strong_programs_are_identified_and_never_widen_the_legacy_coverage() -> None:
    legacy_matches = [
        AffiliateMatch(1, "Make", "direct", tier="strong"),
        AffiliateMatch(2, "Pipedrive", "impact", tier="weak"),
    ]
    alias_only = AffiliateMatch(7, "HubSpot", "impact", tier="strong", legacy=False)
    base = affiliate_coverage(legacy_matches)
    widened = affiliate_coverage([*legacy_matches, alias_only])
    # legacy の 4 項目は alias-only の有無で完全に同一
    assert (widened.level, widened.program_count, widened.providers, widened.program_names) == (
        base.level,
        base.program_count,
        base.providers,
        base.program_names,
    )
    assert widened.program_names == ("Make", "Pipedrive")
    # tier 項目は alias-only を strong として数え、alias_only_* で明示する
    assert (base.strong_program_count, base.weak_program_count) == (1, 1)
    assert (widened.strong_program_count, widened.weak_program_count) == (2, 1)
    assert widened.strong_program_names == ("HubSpot", "Make")
    assert widened.alias_only_strong_program_names == ("HubSpot",)
    assert base.alias_only_strong_program_names == ()
    # tier 未算出 / 0 件では alias_only は空
    assert affiliate_coverage(()).alias_only_strong_program_names == ()
    assert affiliate_coverage([AffiliateMatch(1, "A", None)]).alias_only_strong_program_names == ()


def test_weak_count_is_legacy_generic_matches_only_and_strong_count_includes_alias_only() -> None:
    """count の意味 (文書化された仕様): weak = legacy で match し strong でない program、
    strong = 自身の名前 / alias で match した program (alias だけの program を含む)。"""

    cov = affiliate_coverage(
        [
            AffiliateMatch(1, "A", None, tier="weak"),
            AffiliateMatch(2, "B", None, tier="weak"),
            AffiliateMatch(3, "C", None, tier="strong"),
            AffiliateMatch(4, "D", None, tier="strong", legacy=False),
        ]
    )
    assert cov.program_count == 3  # legacy: A, B, C
    assert (cov.strong_program_count, cov.weak_program_count) == (2, 2)
    assert (
        cov.strong_program_count + cov.weak_program_count == cov.program_count + 1
    )  # alias-only 1 件
    doc = type(cov).__doc__ or ""  # 仕様は class の docstring に文書化されている
    for name in ("strong_program_count", "weak_program_count", "alias_only_strong_program_names"):
        assert name in doc, name
