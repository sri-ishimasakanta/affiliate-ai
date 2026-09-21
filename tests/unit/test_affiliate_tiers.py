"""app/keyword/affiliate_tiers.py — strong / weak の 2-tier affiliate match (pure)。

C2.5.4 は **報告専用の追加**。legacy の match 集合 (``match_programs``) を変えないこと、英語の
空白を広げないこと、一般語の brand (Make / monday / Reclaim / Fireflies) を無条件に strong に
しないことを検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.keyword.affiliate_matching import ProgramFacts, match_programs
from app.keyword.affiliate_tiers import (
    AMBIGUITY_BARE,
    AMBIGUITY_IDIOM,
    AMBIGUITY_NO_CONTEXT,
    BASIS_ALIAS,
    BASIS_AMBIGUOUS_OWN_NAME,
    BASIS_GENERIC,
    BASIS_OWN_NAME,
    DEFAULT_TIER_CONFIG_PATH,
    TIER_STRONG,
    TIER_WEAK,
    TierConfigError,
    branded_forms,
    default_tier_config,
    is_own_name_term,
    load_tier_config,
    match_programs_tiered,
    name_words,
    own_name_token,
    parse_tier_config,
    summarize_tiers,
    unreviewed_brand_tokens,
)

# C2.5.2 / C2.5.3 で監査した catalog (19 件) の program 名 (config の網羅性の検証用)。
CATALOG_NAMES = (
    "Make",
    "n8n Cloud",
    "Writesonic",
    "Descript",
    "HubSpot",
    "Semrush AI Visibility Toolkit",
    "Semrush SEO Toolkit",
    "ClickUp",
    "monday.com",
    "Grammarly Pro upgrade",
    "Fireflies.ai",
    "Pipedrive",
    "ActiveCampaign",
    "Krisp",
    "Reclaim.ai",
    "Todoist",
    "Canva Pro annual plan",
    "Notion",
    "Jasper Creator / Teams",
)

_TERMS = {
    "Make": ("Make", "業務自動化", "自動化", "ワークフロー", "業務効率化"),
    "n8n Cloud": ("n8n", "業務自動化", "自動化"),
    "Descript": ("Descript", "文字起こし", "議事録"),
    "HubSpot": ("HubSpot", "CRM", "顧客管理", "業務効率化"),
    "monday.com": ("monday.com", "monday", "プロジェクト管理", "タスク管理"),
    "Fireflies.ai": ("Fireflies", "Fireflies.ai", "AI 議事録", "文字起こし"),
    "Reclaim.ai": ("Reclaim.ai", "Reclaim", "AI スケジュール", "業務効率化"),
    "Krisp": ("Krisp", "AI 議事録", "文字起こし"),
    "Pipedrive": ("Pipedrive", "CRM", "SFA", "顧客管理"),
}


def _facts() -> list[ProgramFacts]:
    return [
        ProgramFacts(
            program_id=i,
            name=name,
            provider="direct",
            category="test",
            commission_type=None,
            commission_value=None,
            currency=None,
            match_terms=terms,
        )
        for i, (name, terms) in enumerate(_TERMS.items(), start=1)
    ]


FACTS = _facts()


def _one(keyword: str, name: str, *, spacing: bool = False):
    hits = [m for m in match_programs_tiered(keyword, FACTS, ignore_japanese_spacing=spacing)]
    return next((m for m in hits if m.name == name), None)


# ============================================================ strong: own name
@pytest.mark.parametrize(
    ("keyword", "program"),
    [
        ("Krisp 無料", "Krisp"),
        ("n8n", "n8n Cloud"),
        ("hubspot とは", "HubSpot"),
        ("Pipedrive 料金", "Pipedrive"),
        ("ＨｕｂＳｐｏｔ 使い方", "HubSpot"),  # 全角 (NFKC)
        ("Descript とは", "Descript"),
    ],
)
def test_own_name_spelling_is_strong(keyword: str, program: str) -> None:
    m = _one(keyword, program)
    assert m is not None and m.tier == TIER_STRONG and m.legacy_matched
    assert m.ambiguity is None
    assert any(t.basis == BASIS_OWN_NAME and t.tier == TIER_STRONG for t in m.term_tiers)
    assert m.reason.startswith("strong:")


@pytest.mark.parametrize(
    ("program", "term", "expected"),
    [
        ("Fireflies.ai", "Fireflies", True),
        ("Fireflies.ai", "Fireflies.ai", True),
        ("monday.com", "monday", True),
        ("monday.com", "monday.com", True),
        ("n8n Cloud", "n8n", True),
        ("Semrush AI Visibility Toolkit", "Semrush", True),
        ("Grammarly Pro upgrade", "Grammarly", True),
        ("Make", "Make 料金", True),  # 日本語が付いても own-name の word を含む
        ("Make", "makeup", False),  # 別の word (prefix 一致では strong にしない)
        ("Make", "maker", False),
        ("HubSpot", "CRM", False),
        ("Semrush AI Visibility Toolkit", "AI Visibility", False),
        ("Descript", "議事録", False),
    ],
)
def test_own_name_derivation_is_word_based(program: str, term: str, expected: bool) -> None:
    assert is_own_name_term(program, term) is expected


def test_name_helpers() -> None:
    assert name_words("monday.com") == ("monday", "com")
    assert name_words("n8n Cloud") == ("n8n", "cloud")
    assert own_name_token("Fireflies.ai") == "fireflies"
    assert own_name_token("Jasper Creator / Teams") == "jasper"
    assert own_name_token("") == ""
    assert branded_forms("monday.com") == ("monday.com", "monday com")
    assert branded_forms("Reclaim.ai") == ("reclaim.ai", "reclaim ai")
    assert branded_forms("Make") == ()  # 単語の名前は brand 形を導けない
    assert branded_forms("n8n Cloud") == ()


# ================================================================ weak: generic
@pytest.mark.parametrize(
    ("keyword", "program"),
    [
        ("crm", "HubSpot"),
        ("crm", "Pipedrive"),
        ("議事録 作成", "Descript"),
        ("業務効率化 ツール", "Make"),
        ("タスク管理 おすすめ", "monday.com"),
        ("sfa とは", "Pipedrive"),
    ],
)
def test_generic_terms_are_weak(keyword: str, program: str) -> None:
    m = _one(keyword, program)
    assert m is not None and m.tier == TIER_WEAK and m.legacy_matched
    assert m.strong_terms == () and m.weak_terms
    assert all(t.basis == BASIS_GENERIC for t in m.term_tiers)
    assert m.ambiguity is None
    assert m.reason.startswith("weak: generic term(s) only")


def test_strong_and_weak_terms_on_one_program_report_both_and_the_program_is_strong() -> None:
    m = _one("hubspot crm", "HubSpot")
    assert m is not None and m.tier == TIER_STRONG
    assert m.strong_terms == ("HubSpot",)
    assert m.weak_terms == ("CRM",)
    assert m.matched_terms == ("HubSpot", "CRM")  # legacy の matched_terms は全部
    # 他の program は generic の CRM だけ = weak
    assert _one("hubspot crm", "Pipedrive").tier == TIER_WEAK


def test_summarize_tiers_counts_and_no_strong_indicator() -> None:
    summary = summarize_tiers(match_programs_tiered("crm", FACTS))
    assert (summary.strong_count, summary.weak_count) == (0, 2)
    assert summary.no_strong_affiliate_match is True
    assert summary.weak_names == ("HubSpot", "Pipedrive")
    mixed = summarize_tiers(match_programs_tiered("hubspot crm", FACTS))
    assert (mixed.strong_count, mixed.weak_count) == (1, 1)
    assert mixed.strong_names == ("HubSpot",) and mixed.no_strong_affiliate_match is False
    none = summarize_tiers(match_programs_tiered("chatgpt 使い方", FACTS))
    assert (none.strong_count, none.weak_count, none.no_strong_affiliate_match) == (0, 0, True)


# ================================================================ explicit alias
def test_hubspot_katakana_alias_is_strong_even_without_a_legacy_match() -> None:
    m = _one("ハブスポット とは", "HubSpot")
    assert m is not None and m.tier == TIER_STRONG
    assert m.legacy_matched is False  # legacy の covered 集合は広げない
    assert m.strong_terms == ("ハブスポット",)
    assert m.term_tiers[0].basis == BASIS_ALIAS
    assert match_programs("ハブスポット とは", FACTS) == []  # legacy は従来どおり 0 件


def test_alias_upgrades_a_program_that_is_also_matched_by_generic_terms() -> None:
    m = _one("ハブスポット crm", "HubSpot")
    assert m is not None and m.tier == TIER_STRONG and m.legacy_matched
    assert m.strong_terms == ("ハブスポット",) and m.weak_terms == ("CRM",)
    assert m.matched_terms == ("CRM",)  # legacy の matched_terms は不変


def test_alias_matching_is_case_and_width_insensitive_and_bounded() -> None:
    assert _one("ハブスポット", "HubSpot").tier == TIER_STRONG
    assert _one("ｈｕｂ ｓｐｏｔ", "HubSpot") is None  # 英語の空白は広げない
    assert _one("ハブスポットー", "HubSpot") is not None  # 日本語は境界 (英数字) の対象外


def test_only_audited_aliases_exist() -> None:
    cfg = default_tier_config()
    assert set(cfg.aliases) == {"hubspot", "make"}
    assert cfg.aliases["hubspot"] == ("ハブスポット",)
    assert cfg.aliases["make"] == ("make.com",)


# ============================================================ Japanese spacing
def test_japanese_spacing_is_opt_in_for_alias_and_generic_terms() -> None:
    assert _one("ハブ スポット と は", "HubSpot") is None
    spaced = _one("ハブ スポット と は", "HubSpot", spacing=True)
    assert spaced is not None and spaced.tier == TIER_STRONG and not spaced.legacy_matched
    assert _one("議事 録 作成 ai", "Descript") is None
    generic = _one("議事 録 作成 ai", "Descript", spacing=True)
    assert generic is not None and generic.tier == TIER_WEAK and generic.legacy_matched


def test_english_whitespace_is_never_widened_by_tiering() -> None:
    for spacing in (False, True):
        assert _one("hub spot", "HubSpot", spacing=spacing) is None
        assert _one("click up", "HubSpot", spacing=spacing) is None
        assert _one("c rm", "HubSpot", spacing=spacing) is None
        assert _one("makesure", "Make", spacing=spacing) is None
        assert _one("mak e", "Make", spacing=spacing) is None
        assert _one("n 8 n", "n8n Cloud", spacing=spacing) is None


# ================================================== ambiguous common-word brands
@pytest.mark.parametrize(
    "keyword",
    [
        "Make 料金",
        "Make 使い方",
        "make 代替",
        "MAKE 料金",
        "ＭＡＫＥ 使い方",
        "Make n8n 比較",
        "make 自動化 事例",
    ],
)
def test_make_with_japanese_commercial_context_is_strong(keyword: str) -> None:
    m = _one(keyword, "Make")
    assert m is not None and m.tier == TIER_STRONG, keyword
    assert m.ambiguity is None


@pytest.mark.parametrize("keyword", ["make.com", "make.com 使い方", "Make.com pricing"])
def test_make_domain_is_strong_via_explicit_alias(keyword: str) -> None:
    m = _one(keyword, "Make")
    assert m is not None and m.tier == TIER_STRONG
    assert any(t.basis == BASIS_ALIAS and t.term == "make.com" for t in m.term_tiers)


@pytest.mark.parametrize(
    ("keyword", "ambiguity"),
    [
        ("make sense", AMBIGUITY_IDIOM),
        ("make sure", AMBIGUITY_IDIOM),
        ("make up for", AMBIGUITY_IDIOM),
        ("make in", AMBIGUITY_IDIOM),
        ("Make Sense", AMBIGUITY_IDIOM),
        ("how to make sure it works", AMBIGUITY_IDIOM),
        ("make sure 使い方", AMBIGUITY_IDIOM),  # 日本語が付いても idiom は strong にしない
        ("make sense 比較", AMBIGUITY_IDIOM),
        ("make", AMBIGUITY_BARE),  # 単独語: 明示的に weak + 報告
        ("Make", AMBIGUITY_BARE),
        ("make money online", AMBIGUITY_NO_CONTEXT),
        ("make it work", AMBIGUITY_NO_CONTEXT),
        ("how to make a website", AMBIGUITY_NO_CONTEXT),
        ("make pricing", AMBIGUITY_NO_CONTEXT),
    ],
)
def test_make_english_uses_and_bare_make_are_never_strong(keyword: str, ambiguity: str) -> None:
    m = _one(keyword, "Make")
    assert m is not None, keyword  # legacy では match する (covered は変えない)
    assert m.legacy_matched is True
    assert m.tier == TIER_WEAK and m.strong_terms == ()
    assert m.ambiguity == ambiguity
    assert m.term_tiers[0].basis == BASIS_AMBIGUOUS_OWN_NAME
    assert "ordinary English verb" in m.reason or "non-brand phrase" in m.reason


def test_bare_make_treatment_is_explicit_in_the_reason() -> None:
    m = _one("make", "Make")
    assert "bare token" in m.reason and "not accepted as strong" in m.reason


@pytest.mark.parametrize(
    ("keyword", "tier"),
    [
        ("monday com", TIER_STRONG),  # Google が返す分かち書き
        ("monday.com", TIER_STRONG),
        ("monday com とは", TIER_STRONG),
        ("monday タスク 管理", TIER_STRONG),
        ("monday プロジェクト 管理", TIER_STRONG),
        ("monday", TIER_WEAK),
        ("monday morning", TIER_WEAK),
        ("happy monday", TIER_WEAK),
    ],
)
def test_monday_needs_a_brand_form_or_japanese_context(keyword: str, tier: str) -> None:
    m = _one(keyword, "monday.com")
    assert m is not None and m.tier == tier, keyword


@pytest.mark.parametrize(
    ("keyword", "tier"),
    [
        ("reclaim.ai", TIER_STRONG),
        ("reclaim ai", TIER_STRONG),
        ("reclaim 使い方", TIER_STRONG),
        ("reclaim", TIER_WEAK),
        ("reclaim your time", TIER_WEAK),
        ("fireflies.ai", TIER_STRONG),
        ("fireflies ai", TIER_STRONG),
        ("fireflies 料金", TIER_STRONG),
        ("fireflies", TIER_WEAK),
        ("fireflies in the sky", TIER_WEAK),
    ],
)
def test_reclaim_and_fireflies_guard(keyword: str, tier: str) -> None:
    name = "Reclaim.ai" if keyword.startswith("reclaim") else "Fireflies.ai"
    m = _one(keyword, name)
    assert m is not None and m.tier == tier, keyword


def test_distinctive_brands_are_not_guarded() -> None:
    for keyword, name in (
        ("krisp", "Krisp"),
        ("n8n", "n8n Cloud"),
        ("hubspot", "HubSpot"),
        ("pipedrive", "Pipedrive"),
    ):
        m = _one(keyword, name)
        assert m.tier == TIER_STRONG and m.ambiguity is None, keyword  # 単独でも strong


def test_without_context_strong_config_flips_the_bare_treatment() -> None:
    raw = json.loads(DEFAULT_TIER_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["ambiguous_brands"]["fireflies"]["without_context"] = "strong"
    cfg = parse_tier_config(raw)
    bare = [
        m for m in match_programs_tiered("fireflies", FACTS, config=cfg) if m.name == "Fireflies.ai"
    ]
    assert bare[0].tier == TIER_STRONG  # 明示的な config 判断のとき
    default = [m for m in match_programs_tiered("fireflies", FACTS) if m.name == "Fireflies.ai"]
    assert default[0].tier == TIER_WEAK  # 既定は weak


def test_unrelated_english_uses_of_ambiguous_words_do_not_become_strong() -> None:
    for keyword in (
        "make money",
        "make it happen",
        "monday blues",
        "reclaim the night",
        "fireflies song",
    ):
        for m in match_programs_tiered(keyword, FACTS):
            assert m.tier == TIER_WEAK, (keyword, m.name)


# ============================================================ legacy preservation
_KEYWORDS = [
    "make",
    "Make 料金",
    "make sure",
    "make.com",
    "MAKE 使い方",
    "maker 比較",
    "makers",
    "ハブスポット",
    "ハブ スポット と は",
    "議事 録 作成 ai",
    "ai 議事 録",
    "AI 議事録 おすすめ",
    "crm",
    "c rm",
    "hub spot",
    "hubspot crm",
    "業務 効率 化 ツール",
    "業務効率化 ツール",
    "タスク 管理",
    "monday",
    "monday com",
    "reclaim",
    "fireflies",
    "krisp 無料",
    "n8n",
    "n 8 n",
    "chatgpt 使い方",
    "",
    "  ",
    "ｍａｋｅ　ｓｕｒｅ",
    "顧客 管理",
    "sfa",
    "文字 起こし",
    "ai スケジュール",
    "descript",
]


@pytest.mark.parametrize("spacing", [False, True])
def test_legacy_matched_set_is_identical_to_match_programs(spacing: bool) -> None:
    for keyword in _KEYWORDS:
        legacy = match_programs(keyword, FACTS, ignore_japanese_spacing=spacing)
        tiered = match_programs_tiered(keyword, FACTS, ignore_japanese_spacing=spacing)
        assert [m.program for m in tiered if m.legacy_matched] == legacy, keyword
        # 追加で増えるのは alias だけの strong program だけ
        extra = [m for m in tiered if not m.legacy_matched]
        assert all(m.tier == TIER_STRONG and m.term_tiers[0].basis == BASIS_ALIAS for m in extra), (
            keyword
        )


def test_legacy_program_objects_are_untouched_including_matched_terms() -> None:
    for m in match_programs_tiered("hubspot crm", FACTS):
        if m.legacy_matched:
            assert m.program.matched_terms == m.matched_terms
    assert [m.program for m in match_programs_tiered("crm", FACTS)] == match_programs("crm", FACTS)


def test_result_order_follows_the_catalog_order() -> None:
    names = [m.name for m in match_programs_tiered("crm 顧客管理 hubspot", FACTS)]
    assert names == [f.name for f in FACTS if f.name in names]


# ================================================================ config
def test_default_config_is_valid_and_covers_every_audited_catalog_name() -> None:
    cfg = default_tier_config()
    assert set(cfg.ambiguous) == {"make", "monday", "reclaim", "fireflies", "notion", "jasper"}
    assert all(b.without_context == "weak" for b in cfg.ambiguous.values())
    assert cfg.ambiguous["make"].deny_phrases == (
        "make sense",
        "make sure",
        "make up for",
        "make in",
    )
    programs = [
        ProgramFacts(i, name, None, None, None, None, None, ())
        for i, name in enumerate(CATALOG_NAMES, start=1)
    ]
    # 19 件すべての own-name token が「一般語として guard 済み」か「distinctive と確認済み」
    assert unreviewed_brand_tokens(programs) == []


def test_unreviewed_brand_token_is_flagged_for_new_programs() -> None:
    new = [ProgramFacts(1, "Acme CRM Suite", None, None, None, None, None, ())]
    assert unreviewed_brand_tokens(new) == [("Acme CRM Suite", "acme")]
    assert (
        unreviewed_brand_tokens(
            new, parse_tier_config({"version": 1, "reviewed_distinctive_brands": ["acme"]})
        )
        == []
    )


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ([], "must be a JSON object"),
        ({"version": 2}, "version must be 1"),
        ({"version": 1, "bogus": 1}, "unknown top-level"),
        ({"version": 1, "aliases": []}, "aliases must be an object"),
        ({"version": 1, "aliases": {"X": []}}, "non-empty list"),
        ({"version": 1, "aliases": {"X": [""]}}, "non-empty list"),
        ({"version": 1, "aliases": {"X": ["a"], "x": ["b"]}}, "duplicate alias"),
        (
            {
                "version": 1,
                "ambiguous_brands": {"Make": {"reason": "r", "without_context": "weak"}},
            },
            "lowercase",
        ),
        (
            {"version": 1, "ambiguous_brands": {"make": {"reason": "", "without_context": "weak"}}},
            "reason",
        ),
        (
            {
                "version": 1,
                "ambiguous_brands": {"make": {"reason": "r", "without_context": "maybe"}},
            },
            "without_context",
        ),
        (
            {
                "version": 1,
                "ambiguous_brands": {
                    "make": {"reason": "r", "without_context": "weak", "deny_phrases": "x"}
                },
            },
            "deny_phrases",
        ),
        (
            {
                "version": 1,
                "ambiguous_brands": {"make": {"reason": "r", "without_context": "weak", "x": 1}},
            },
            "unknown keys",
        ),
        ({"version": 1, "reviewed_distinctive_brands": "x"}, "reviewed_distinctive_brands"),
        (
            {
                "version": 1,
                "ambiguous_brands": {"make": {"reason": "r", "without_context": "weak"}},
                "reviewed_distinctive_brands": ["make"],
            },
            "both ambiguous and reviewed",
        ),
    ],
)
def test_config_validation(raw, fragment: str) -> None:
    with pytest.raises(TierConfigError) as exc:
        parse_tier_config(raw)
    assert fragment in str(exc.value)


def test_config_load_errors(tmp_path: Path) -> None:
    with pytest.raises(TierConfigError, match="not found"):
        load_tier_config(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    with pytest.raises(TierConfigError, match="not readable JSON"):
        load_tier_config(bad)


def test_module_is_pure_no_db_network_or_llm_imports() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "app" / "keyword" / "affiliate_tiers.py"
    ).read_text(encoding="utf-8")
    for banned in (
        "sqlalchemy",
        "httpx",
        "requests",
        "google",
        "openai",
        "anthropic",
        "socket",
        "fastapi",
    ):
        assert f"import {banned}" not in source and f"from {banned}" not in source, banned


# ============================================== C2.5.4 final review: recorded decisions
def test_bare_fireflies_stays_weak_by_decision_while_brand_and_context_forms_may_be_strong() -> (
    None
):
    """決定: 単独の ``fireflies`` は weak のまま (SERP / GSC の証拠が出るまで再検討しない)。"""

    cfg = default_tier_config()
    assert cfg.ambiguous["fireflies"].without_context == "weak"
    bare = _one("fireflies", "Fireflies.ai")
    assert bare is not None and bare.legacy_matched  # legacy の covered は変えない
    assert bare.tier == TIER_WEAK and bare.ambiguity == AMBIGUITY_BARE
    assert bare.strong_terms == ()
    for keyword in ("fireflies.ai", "fireflies ai", "fireflies 料金", "fireflies 使い方 zoom"):
        strong = _one(keyword, "Fireflies.ai")
        assert strong is not None and strong.tier == TIER_STRONG, keyword
    # 英語だけの文脈 (brand 形なし) も strong にしない
    assert _one("fireflies pricing", "Fireflies.ai").tier == TIER_WEAK
    assert (
        "SERP/GSC"
        in json.loads(DEFAULT_TIER_CONFIG_PATH.read_text(encoding="utf-8"))["ambiguous_brands"][
            "fireflies"
        ]["reason"]
    )


def test_tiering_is_deterministic_and_driven_by_config_not_code() -> None:
    keywords = [
        "make sure",
        "Make 料金",
        "monday com",
        "monday",
        "fireflies",
        "crm",
        "ハブスポット",
        "krisp",
    ]
    first = [
        [
            (m.name, m.tier, m.ambiguity, m.legacy_matched, m.reason)
            for m in match_programs_tiered(k, FACTS)
        ]
        for k in keywords
    ]
    second = [
        [
            (m.name, m.tier, m.ambiguity, m.legacy_matched, m.reason)
            for m in match_programs_tiered(k, FACTS)
        ]
        for k in keywords
    ]
    assert first == second  # 乱数 / 時刻 / 入力順に依存しない
    # config を変えるだけで結果が変わる (コードの変更は不要)
    raw = json.loads(DEFAULT_TIER_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["reviewed_distinctive_brands"].remove("krisp")
    raw["ambiguous_brands"]["krisp"] = {
        "reason": "test",
        "without_context": "weak",
        "deny_phrases": [],
    }
    raw["aliases"]["Krisp"] = ["クリスプ"]
    custom = parse_tier_config(raw)
    assert _one("krisp", "Krisp").tier == TIER_STRONG  # 既定 config: distinctive
    bare = [m for m in match_programs_tiered("krisp", FACTS, config=custom) if m.name == "Krisp"]
    assert bare[0].tier == TIER_WEAK and bare[0].ambiguity == AMBIGUITY_BARE
    alias = [
        m for m in match_programs_tiered("クリスプ", FACTS, config=custom) if m.name == "Krisp"
    ]
    assert alias and alias[0].tier == TIER_STRONG and alias[0].legacy_matched is False


def test_alias_only_matches_never_change_the_legacy_program_set_for_any_keyword() -> None:
    """alias だけで strong の program は legacy の集合に入らない (全 keyword / 両 spacing 設定)。"""

    for spacing in (False, True):
        for keyword in _KEYWORDS + [
            "ハブスポット crm",
            "make.com",
            "Make.com 料金",
            "ハブ スポット",
        ]:
            tiered = match_programs_tiered(keyword, FACTS, ignore_japanese_spacing=spacing)
            legacy = match_programs(keyword, FACTS, ignore_japanese_spacing=spacing)
            legacy_ids = [m.program_id for m in legacy]
            assert [m.program_id for m in tiered if m.legacy_matched] == legacy_ids, keyword
            assert all(m.program_id not in legacy_ids for m in tiered if not m.legacy_matched), (
                keyword
            )
