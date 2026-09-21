"""共有 matching helper (app/keyword/affiliate_matching.py) の unit テスト。

分析 CLI と production normalizer / service が同じ照合ルールを使うための helper。
"""

from app.keyword.affiliate_matching import (
    MatchedProgram,
    ProgramFacts,
    match_programs,
    normalize_for_match,
    term_matches,
)


def _pf(
    program_id: int, *, provider: str | None = "direct", terms: tuple[str, ...] = ()
) -> ProgramFacts:
    return ProgramFacts(
        program_id=program_id,
        name=f"P{program_id}",
        provider=provider,
        category="ai",
        commission_type=None,
        commission_value=None,
        currency=None,
        match_terms=terms,
    )


def test_normalize_for_match_nfkc_casefold_whitespace() -> None:
    assert normalize_for_match("  ＡＩ　議事録  ") == "ai 議事録"
    assert normalize_for_match("ChatGPT") == "chatgpt"
    assert normalize_for_match("Power  Automate") == "power automate"


def test_term_matches_japanese_substring() -> None:
    assert term_matches("議事録", "ai 議事録 おすすめ")
    assert not term_matches("議事録", "ai 文字起こし")


def test_term_matches_ascii_boundary() -> None:
    assert term_matches("make", "make 料金")
    assert not term_matches("make", "maker 比較")
    assert not term_matches("rpa", "grpative について")
    assert term_matches("n8n", "n8n 使い方")


def test_term_matches_empty_term_is_false() -> None:
    assert not term_matches("", "anything")


def test_match_programs_japanese() -> None:
    programs = [_pf(1, terms=("議事録", "AI 議事録")), _pf(2, terms=("RPA",))]
    matched = match_programs("AI 議事録 おすすめ", programs)
    assert [m.program_id for m in matched] == [1]
    assert isinstance(matched[0], MatchedProgram)
    assert set(matched[0].matched_terms) == {"議事録", "AI 議事録"}


def test_match_programs_ascii_boundary_and_casefold() -> None:
    programs = [_pf(1, terms=("Make",))]
    assert match_programs("MAKE 料金", programs)  # casefold
    assert not match_programs("maker 向け", programs)  # boundary


def test_match_programs_nfkc() -> None:
    programs = [_pf(1, terms=("AI 議事録",))]
    assert match_programs("ＡＩ　議事録　おすすめ", programs)


def test_match_programs_multiple_and_empty_terms_ignored() -> None:
    programs = [
        _pf(1, terms=("議事録",)),
        _pf(2, provider="x", terms=("", "  ", "AI 議事録")),
        _pf(3, provider="y", terms=("文字起こし",)),
    ]
    matched = match_programs("AI 議事録 比較", programs)
    assert {m.program_id for m in matched} == {1, 2}


# ==========================================================================
# C2.5.1: 日本語 phrase の分かち書きを吸収する opt-in 照合 (Google Ads idea 用)
# ==========================================================================
def test_default_matching_is_unchanged_by_the_spacing_option() -> None:
    programs = [_pf(1, terms=("議事録",))]
    assert match_programs("議事 録 おすすめ", programs) == []  # 既定は従来どおり (分かち書きは別語)
    assert [m.program_id for m in match_programs("議事録 おすすめ", programs)] == [1]


def test_japanese_terms_match_split_and_unsplit_keywords_when_opted_in() -> None:
    programs = [
        _pf(1, terms=("議事録",)),
        _pf(2, terms=("AI 議事録",)),
        _pf(3, terms=("タスク管理",)),
    ]
    for keyword in ("議事録 作成", "議事 録 作成", "議 事録 作成"):
        got = match_programs(keyword, programs, ignore_japanese_spacing=True)
        assert [m.program_id for m in got] == [1], keyword
    # 分割された term と分割されていない term は同じ program に当たる
    split = match_programs("ai 議事 録 おすすめ", programs, ignore_japanese_spacing=True)
    unsplit = match_programs("ai 議事録 おすすめ", programs, ignore_japanese_spacing=True)
    assert [m.program_id for m in split] == [m.program_id for m in unsplit] == [1, 2]
    # 既定の照合でも unsplit は同じ結果 (opt-in は split を unsplit に揃えるだけ)
    assert [m.program_id for m in match_programs("ai 議事録 おすすめ", programs)] == [1, 2]
    # ASCII に glue した表記は従来どおり境界で外れる (term 1)。term 2 は空白の有無を無視する
    glued = match_programs("AI議事録おすすめ", programs, ignore_japanese_spacing=True)
    assert [m.program_id for m in glued] == [2]
    task = match_programs("タスク 管理 ツール", programs, ignore_japanese_spacing=True)
    assert [m.program_id for m in task] == [3]
    assert task[0].matched_terms == ("タスク管理",)  # term は元の表記のまま


def test_split_terms_in_the_catalog_match_unsplit_keywords_when_opted_in() -> None:
    programs = [_pf(1, terms=("議事 録",))]
    assert match_programs("議事録 作成", programs) == []
    assert [
        m.program_id for m in match_programs("議事録 作成", programs, ignore_japanese_spacing=True)
    ] == [1]


def test_spacing_option_never_removes_a_match_the_default_finds() -> None:
    programs = [_pf(1, terms=("議事録", "AI 議事録", "RPA", "Make"))]
    for keyword in ("AI 議事録 おすすめ", "make 使い方 rpa", "議事録 比較", "ＡＩ　議事録"):
        base = {m.program_id: m.matched_terms for m in match_programs(keyword, programs)}
        opted = {
            m.program_id: m.matched_terms
            for m in match_programs(keyword, programs, ignore_japanese_spacing=True)
        }
        assert set(base) <= set(opted), keyword
        assert set(base[1]) <= set(opted[1]), keyword


def test_english_only_terms_and_keywords_keep_their_semantics_when_opted_in() -> None:
    programs = [_pf(1, terms=("CRM",)), _pf(2, terms=("Make",))]
    # 英語だけの keyword は分かち書きを吸収しない
    assert match_programs("c rm", programs, ignore_japanese_spacing=True) == []
    assert match_programs("ma ke", programs, ignore_japanese_spacing=True) == []
    # 日本語を含む keyword でも、英語だけの term は従来どおり (空白を詰めて glue しない)
    assert match_programs("c rm ツール", programs, ignore_japanese_spacing=True) == []
    assert match_programs("ma ke 使い方", programs, ignore_japanese_spacing=True) == []
    # 従来どおり境界を尊重する
    assert match_programs("maker 向け", programs, ignore_japanese_spacing=True) == []
    assert [
        m.program_id for m in match_programs("Make 使い方", programs, ignore_japanese_spacing=True)
    ] == [2]


def test_ascii_boundaries_are_respected_for_japanese_terms_with_ascii_edges() -> None:
    programs = [_pf(1, terms=("AI検索",))]
    assert match_programs("chatai 検索", programs, ignore_japanese_spacing=True) == []  # 語の途中
    for keyword in ("ai 検索 対策", "chat ai 検索", "ai検索", "ai 検 索"):
        got = match_programs(keyword, programs, ignore_japanese_spacing=True)
        assert [m.program_id for m in got] == [1], keyword


def test_ascii_words_inside_a_japanese_term_keep_their_own_spacing() -> None:
    programs = [_pf(1, terms=("生成AI SEO",))]
    matched = match_programs("生成 ai seo 対策", programs, ignore_japanese_spacing=True)
    assert [m.program_id for m in matched] == [1]
    assert match_programs("生成 aiseo 対策", programs, ignore_japanese_spacing=True) == []
