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
