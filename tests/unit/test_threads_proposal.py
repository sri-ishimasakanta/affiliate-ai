"""Threads 投稿案の identity と検査 (T2、pure)。

pin する契約:

- hash と URL が **循環しない** (seed -> URL -> 本文 -> hash の 2 段階)。
- 同じ入力からは必ず同じ hash と同じ URL が出る。
- 500 文字を超える案は保存しない。
- ``/go/`` もアフィリエイト直リンクも Threads には出さない。
- リンクは任意 (link_mode=none を許す)。
- AI っぽい言い回しは **警告** であって自動失格ではない。
"""

from __future__ import annotations

from app.social.threads.policy import get_policy
from app.social.threads.proposal import (
    LINK_MODE_ARTICLE,
    LINK_MODE_NONE,
    LINK_PLACEHOLDER,
    ThreadsProposalDraft,
    build_proposal,
    compute_content_seed,
    normalize_text,
)
from app.social.threads.validators import find_duplicates, validate_proposal

_POLICY = get_policy()
_ARTICLE_URL = "https://bizfluxlab.com/generative-ai-guidelines/"
_BODY_HASH = "b" * 64


def _build(
    body: str,
    *,
    angle="insight",
    link_mode=LINK_MODE_NONE,
    article_url=_ARTICLE_URL,
    article_id=21,
    body_hash=_BODY_HASH,
):
    return build_proposal(
        ThreadsProposalDraft(angle=angle, body=body, link_mode=link_mode),
        source_article_id=article_id,
        source_article_body_hash=body_hash,
        article_url=article_url,
        policy_version=_POLICY.policy_version,
    )


def _validate(proposal):
    result = validate_proposal(proposal, _POLICY)
    proposal.warnings = list(result.warnings)
    return result


# -- hash / URL are not circular -------------------------------------------------
def test_the_content_seed_is_computed_before_the_url_exists() -> None:
    """seed は URL を含まない。ここが循環を断つ要点。"""

    seed = compute_content_seed(
        source_article_id=21,
        source_article_body_hash=_BODY_HASH,
        angle="insight",
        link_mode=LINK_MODE_ARTICLE,
        draft_body=f"本文 {LINK_PLACEHOLDER}",
        policy_version=_POLICY.policy_version,
    )

    assert len(seed) == 64
    # seed は URL を一切参照していない (同じ入力なら記事 URL が何であっても同じ)。
    assert seed == compute_content_seed(
        source_article_id=21,
        source_article_body_hash=_BODY_HASH,
        angle="insight",
        link_mode=LINK_MODE_ARTICLE,
        draft_body=f"本文 {LINK_PLACEHOLDER}",
        policy_version=_POLICY.policy_version,
    )


def test_the_url_carries_the_seed_and_the_hash_carries_the_url() -> None:
    proposal = _build(f"短い本文。\n{LINK_PLACEHOLDER}", link_mode=LINK_MODE_ARTICLE)

    assert f"utm_content={proposal.content_seed[:16]}" in proposal.destination_url
    assert proposal.destination_url in proposal.publish_text
    # 最終 hash は seed とは別物 (URL と本文を含むため)。
    assert proposal.proposal_hash != proposal.content_seed


def test_the_proposal_is_deterministic() -> None:
    first = _build(f"本文。\n{LINK_PLACEHOLDER}", link_mode=LINK_MODE_ARTICLE)
    second = _build(f"本文。\n{LINK_PLACEHOLDER}", link_mode=LINK_MODE_ARTICLE)

    assert first.proposal_hash == second.proposal_hash
    assert first.destination_url == second.destination_url
    assert first.publish_text == second.publish_text


def test_changing_anything_changes_the_hash() -> None:
    base = _build("本文。")

    assert _build("別の本文。").proposal_hash != base.proposal_hash
    assert _build("本文。", angle="question").proposal_hash != base.proposal_hash
    assert _build("本文。", body_hash="c" * 64).proposal_hash != base.proposal_hash
    assert _build("本文。", article_id=22).proposal_hash != base.proposal_hash


def test_the_utm_campaign_encodes_the_article_and_angle() -> None:
    proposal = _build(
        f"本文。\n{LINK_PLACEHOLDER}", angle="comparison", link_mode=LINK_MODE_ARTICLE
    )

    assert "utm_campaign=article-21-comparison" in proposal.destination_url
    assert "utm_source=threads" in proposal.destination_url
    assert "utm_medium=social" in proposal.destination_url


# -- link policy -----------------------------------------------------------------
def test_a_post_without_a_link_is_allowed() -> None:
    proposal = _build("リンク無しでも読んで意味のある短い投稿。")

    assert proposal.link_mode == LINK_MODE_NONE
    assert proposal.destination_url is None
    assert "http" not in proposal.publish_text
    assert _validate(proposal).ok


def test_the_placeholder_disappears_when_there_is_no_link() -> None:
    proposal = _build(f"本文。{LINK_PLACEHOLDER}")

    assert LINK_PLACEHOLDER not in proposal.publish_text
    assert _validate(proposal).ok


def test_an_external_link_is_refused() -> None:
    proposal = _build("外部に送る本文。\nhttps://example.com/landing")

    result = _validate(proposal)

    assert not result.ok
    assert any("bizfluxlab.com" in e for e in result.errors)


def test_an_affiliate_redirect_is_refused() -> None:
    """Threads からアフィリエイト先へ直接送らない。"""

    proposal = _build("本文。\nhttps://bizfluxlab.com/go/abc123")

    result = _validate(proposal)

    assert not result.ok
    assert any("/go/" in e for e in result.errors)


def test_an_article_post_carries_exactly_one_link() -> None:
    proposal = _build(
        f"本文。\n{LINK_PLACEHOLDER}\nhttps://bizfluxlab.com/other/", link_mode=LINK_MODE_ARTICLE
    )

    result = _validate(proposal)

    assert not result.ok
    assert any("exactly one link" in e for e in result.errors)


# -- length and shape ------------------------------------------------------------
def test_a_post_over_the_official_limit_is_refused() -> None:
    proposal = _build("あ" * 501)

    result = _validate(proposal)

    assert not result.ok
    assert any("Threads limit is 500" in e for e in result.errors)


def test_an_empty_post_is_refused() -> None:
    result = _validate(_build("   "))

    assert not result.ok


def test_an_invalid_angle_is_refused() -> None:
    result = _validate(_build("本文。", angle="rant"))

    assert not result.ok
    assert any("angle" in e for e in result.errors)


def test_control_characters_and_raw_html_are_refused() -> None:
    assert not _validate(_build("本文\x00です")).ok
    assert not _validate(_build("<script>alert(1)</script>")).ok


# -- style lint is advisory ------------------------------------------------------
def test_article_style_phrasing_is_a_warning_not_a_failure() -> None:
    """文脈によっては自然でありうるので、自動では落とさない。"""

    result = _validate(_build("本記事では生成AIについて解説します。"))

    assert result.ok
    assert any("本記事では" in w for w in result.warnings)


def test_hype_is_refused_outright() -> None:
    result = _validate(_build("これは神ツールです。"))

    assert not result.ok
    assert any("神ツール" in e for e in result.errors)


def test_repeated_polite_endings_are_warned() -> None:
    text = "AIは便利です。運用も大事です。体制も必要です。記録も残します。"

    result = _validate(_build(text))

    assert result.ok
    assert any("consecutive sentences" in w for w in result.warnings)


def test_too_many_questions_are_warned() -> None:
    result = _validate(_build("どうする？ いつやる？ 誰がやる？"))

    assert result.ok
    assert any("questions" in w for w in result.warnings)


# -- duplicates ------------------------------------------------------------------
def test_duplicates_are_detected_after_normalization() -> None:
    first = _build("同じ 内容 の 投稿。")
    second = _build("同じ 内容 の 投稿。")

    assert find_duplicates([first, second]) == [(0, 1)]


def test_normalization_only_evens_out_spacing() -> None:
    assert normalize_text("a  b　c\n\n\n\nd") == "a b c\n\nd"


# -- the approved text is the text that gets posted ------------------------------
def test_japanese_punctuation_survives_into_the_publish_text() -> None:
    """人が承認した文字がそのまま投稿される (NFKC で畳まない)。"""

    proposal = _build("どこから手をつけた？　（別添7）を開く。")

    assert "？" in proposal.publish_text
    assert "（別添7）" in proposal.publish_text
    assert "?" not in proposal.publish_text
    assert _validate(proposal).ok


def test_identity_still_folds_width_differences() -> None:
    """表記幅だけ違う案は、別物として二重に作らない。"""

    from app.social.threads.proposal import canonical_identity

    full = _build("どこから手をつけた？")
    half = _build("どこから手をつけた?")

    assert full.publish_text != half.publish_text
    assert canonical_identity(full.publish_text) == canonical_identity(half.publish_text)
    assert find_duplicates([full, half]) == [(0, 1)]
