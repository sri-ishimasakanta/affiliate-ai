"""文体の警告で URL を文章として数えない (T6.1)。

本番の提案 #5 に「2 sentence(s) exceed 60 characters」「2 questions; one natural hook is
enough」が付いた。原因は記事の URL: 文の区切りが URL の ``?`` で切れて長い断片が「長い文」
になり、``?`` が問いかけとして数えられていた。

pin する契約:

- URL の ``?`` は問いかけに数えない。URL の文字は「長い文」を作らない。
- URL の外の問いかけ (``？`` / ``?``) と長い文は、これまでどおり警告する。
- URL の直後の句読点は文章として扱う。複数の URL・クエリ・フラグメントも同じ。
- 公開される文字列そのもの・文字数の上限・リンクの検査・禁止表現は変えない。
"""

from __future__ import annotations

import pytest

from app.social.threads.policy import get_policy
from app.social.threads.proposal import (
    LINK_MODE_ARTICLE,
    LINK_MODE_NONE,
    LINK_PLACEHOLDER,
    ThreadsProposalDraft,
    build_proposal,
)
from app.social.threads.validators import style_analysis_text, validate_proposal

_POLICY = get_policy()
_ARTICLE = "https://bizfluxlab.com/ai-transcription-tools/"
_URL = "https://bizfluxlab.com/ai-transcription-tools/?utm_campaign=a-3&utm_source=threads"


def _validate(body: str, link_mode: str = LINK_MODE_NONE):
    proposal = build_proposal(
        ThreadsProposalDraft(angle="question", body=body, link_mode=link_mode),
        source_article_id=3,
        source_article_body_hash="h" * 64,
        article_url=_ARTICLE,
        policy_version=_POLICY.policy_version,
    )
    return proposal, validate_proposal(proposal, _POLICY)


def _question_warning(result) -> list[str]:
    return [w for w in result.warnings if "question" in w]


def _long_warning(result) -> list[str]:
    return [w for w in result.warnings if "exceed" in w]


# == the pure helper ============================================================
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"詳しくは {_URL}", "詳しくは  "),
        (f"{_URL}。次へ", " 。次へ"),
        (f"見る？ {_URL}?", "見る？  ?"),
        (f"(参考 {_URL})", "(参考  )"),
        ("https://a.example/x?b=1#c?d と https://b.example/?q=2", "  と  "),
        ("URL なし？", "URL なし？"),
    ],
)
def test_style_analysis_text_masks_only_url_spans(text, expected) -> None:
    assert style_analysis_text(text) == expected


# == warnings ===================================================================
def test_a_url_question_mark_is_not_a_question() -> None:
    _proposal, result = _validate(
        f"試してから決めるのが安全。\n{LINK_PLACEHOLDER}", LINK_MODE_ARTICLE
    )
    assert _question_warning(result) == []


def test_a_long_url_does_not_make_a_long_sentence() -> None:
    _proposal, result = _validate(f"実際の音声で試す。\n{LINK_PLACEHOLDER}", LINK_MODE_ARTICLE)
    assert _long_warning(result) == []


def test_real_questions_outside_urls_still_count() -> None:
    _proposal, result = _validate(
        f"任せられる？ 本当に? どう使う？\n{LINK_PLACEHOLDER}", LINK_MODE_ARTICLE
    )
    assert _question_warning(result) == ["3 questions; one natural hook is enough"]


def test_real_long_prose_still_warns() -> None:
    long_sentence = "あ" * 70 + "。"
    _proposal, result = _validate(f"{long_sentence}\n{LINK_PLACEHOLDER}", LINK_MODE_ARTICLE)
    assert _long_warning(result) == ["1 sentence(s) exceed 60 characters; prefer shorter ones"]


def test_the_proposal_5_shape_has_no_url_derived_warnings() -> None:
    body = (
        "手元の録音ファイル、会議中じゃなくても文字起こしAIに任せられる？\n"
        "Krisp と Fireflies.ai は、どちらもファイルのアップロード文字起こしを公式に記載"
        "（2026年9月時点）。\n"
        "Krisp は対応形式と最大1GBの上限まで明示している。\n"
        "大きなファイルなら、先に上限を確かめると迷わない。\n"
        "日本語の精度は公式の記載だけでは分からない。実際の音声で試してから決めるのが安全。\n"
        f"{LINK_PLACEHOLDER}"
    )
    proposal, result = _validate(body, LINK_MODE_ARTICLE)
    assert "?" in proposal.publish_text  # URL にはクエリがある
    assert result.errors == []
    assert _question_warning(result) == []  # 本当の問いかけは 1 つだけ
    # 本番の「2 sentence(s)」は、本当に 61 文字ある 2 文目 + URL の断片だった。
    # URL の分だけが消え、本物の長い文の警告は残る。
    assert _long_warning(result) == ["1 sentence(s) exceed 60 characters; prefer shorter ones"]


# == unchanged safety ===========================================================
def test_the_published_text_and_length_limit_are_unchanged() -> None:
    body = "あ" * 480 + f"\n{LINK_PLACEHOLDER}"
    proposal, result = _validate(body, LINK_MODE_ARTICLE)
    assert proposal.destination_url in proposal.publish_text  # URL は本文にそのまま残る
    assert proposal.character_count == len(proposal.publish_text)
    assert any("the Threads limit is" in e for e in result.errors)  # URL 込みの上限のまま


def test_link_validation_still_sees_urls_in_none_mode() -> None:
    _proposal, result = _validate("試す。 https://evil.example/?a=1")
    assert "link_mode is 'none' but the post contains a URL" in result.errors
    assert any("links must point at" in e for e in result.errors)
