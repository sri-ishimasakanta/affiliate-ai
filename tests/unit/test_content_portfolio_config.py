"""C2.7: app/config/content_portfolio.json の構造検証 (pure / DB・network なし)。

初期記事ポートフォリオは C3 / C4 が読む版管理された artifact。ここでは「壊れた状態で commit
されない」ことだけを守る (cluster / mode / 記事タイプ / wave / priority の整合性)。
スコアは production に 7/7 揃っている keyword だけが持ち、それ以外は null のままでよい
(数値を捏造しない)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.article.monetization import MONETIZATION_MODES
from app.article.planning import ArticleType
from app.keyword.idea_seeds import TARGET_CLUSTERS, load_idea_seed_config

PORTFOLIO = Path(__file__).resolve().parents[2] / "app" / "config" / "content_portfolio.json"
SEEDS_V1 = Path(__file__).resolve().parents[2] / "app" / "config" / "keyword_idea_seeds.json"
SEEDS_V2 = Path(__file__).resolve().parents[2] / "app" / "config" / "keyword_idea_seeds_v2.json"

#: 記事タイプは planning の ArticleType、または template がまだ無い C3 向けの補助ラベル。
_EXTRA_TYPES = {"pricing", "supporting_informational"}
_ROLES = {"pillar", "commercial", "comparison", "supporting", "how-to", "informational"}
_COVERAGE = {"strong", "strong_core", "core_weak", "contextual", "none"}


@pytest.fixture(scope="module")
def portfolio() -> dict:
    return json.loads(PORTFOLIO.read_text(encoding="utf-8"))


def test_both_seed_configs_are_valid_and_version_controlled() -> None:
    for path in (SEEDS_V1, SEEDS_V2):
        config = load_idea_seed_config(path)
        assert config.sets, f"{path.name} has no seed sets"
        for seed_set in config.sets:
            assert seed_set.cluster_id in TARGET_CLUSTERS
            assert 1 <= len(seed_set.seeds) <= 20


def test_portfolio_articles_are_structurally_sound(portfolio: dict) -> None:
    articles = portfolio["articles"]
    assert 20 <= len(articles) <= 30, "the initial portfolio must stay in the 20-30 range"

    keywords = [a["keyword"] for a in articles]
    assert len(set(keywords)) == len(keywords), "duplicate keyword in the portfolio"
    priorities = sorted(a["priority"] for a in articles)
    assert priorities == list(range(1, len(articles) + 1)), "priorities must be 1..N"

    known_types = {t.value for t in ArticleType} | _EXTRA_TYPES
    for a in articles:
        assert a["cluster"] in TARGET_CLUSTERS, a
        assert a["monetization_mode"] in MONETIZATION_MODES, a
        assert a["article_type"] in known_types, a
        assert a["role"] in _ROLES, a
        assert a["affiliate_coverage"] in _COVERAGE, a
        assert a["wave"] in (1, 2, 3), a
        assert a["selection_reason"].strip(), a
        assert isinstance(a["primary_candidates"], list)
        assert isinstance(a["cannibalization_watch"], list)


def test_affiliate_articles_have_a_primary_candidate_and_supporting_ones_need_none(
    portfolio: dict,
) -> None:
    """C2.5.8: affiliate mode は primary 候補が要る。supporting は 0 件でよい。"""

    for a in portfolio["articles"]:
        if a["monetization_mode"] == "affiliate":
            assert a["primary_candidates"], f"{a['keyword']} is affiliate with no primary candidate"
            assert a["affiliate_coverage"] in {"strong", "strong_core", "core_weak"}, a


def test_scores_are_never_fabricated(portfolio: dict) -> None:
    """score は production に 7/7 揃っているものだけ。無いものは null (捏造しない)。"""

    for a in portfolio["articles"]:
        score = a["opportunity_score"]
        assert score is None or 0.0 <= float(score) <= 100.0, a
    assert any(a["opportunity_score"] is None for a in portfolio["articles"])
    assert any(a["opportunity_score"] is not None for a in portfolio["articles"])


def test_the_published_pillar_is_not_re_selected(portfolio: dict) -> None:
    existing = {e["keyword"] for e in portfolio["existing_articles"]}
    assert "業務効率化 ツール おすすめ" in existing
    assert not existing & {a["keyword"] for a in portfolio["articles"]}


def test_reserve_and_reject_entries_carry_a_reason(portfolio: dict) -> None:
    selected = {a["keyword"] for a in portfolio["articles"]}
    for entry in portfolio["reserve"]:
        assert entry["reason"].strip() and entry["keyword"] not in selected
    for entry in portfolio["merge_or_reject"]:
        assert entry["decision"] in {"merge", "reject"}
        assert entry["reason"].strip()
        if entry["decision"] == "merge":
            assert entry["into"] in selected or entry["into"] in {
                e["keyword"] for e in portfolio["existing_articles"]
            }


def test_every_wave_is_described(portfolio: dict) -> None:
    waves = {a["wave"] for a in portfolio["articles"]}
    assert waves == {1, 2, 3}
    for wave in sorted(waves):
        assert portfolio["waves"][str(wave)].strip()
