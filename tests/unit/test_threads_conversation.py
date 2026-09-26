"""T6.3: 会話のきっかけ (pure: 割り当て・検査・prompt・schema・率)。

pin する契約:

- きっかけは 5 つ (none / question / choice / experience / opinion)。依頼の安定した入力から
  SHA-256 で決まる (乱数なし)。同じ入力は同じ型。長く見れば各型が約 20%。隣が同じならずらす。
- 検査: コメント・いいね・フォロー・リポストの依頼、根拠の無い読者像、おすすめ・到達の主張、
  作られた体験、2 つ以上の問いは誤り。none で返事を求めるのは誤り。好みの問題は警告。
- prompt: きっかけを渡したときだけ新しい節と出力の項目が入る。渡さなければ前と同じ形。
- schema: きっかけを求める依頼は、その値だけの enum を必須にする。求めない依頼は前と同じ。
- 率: views が 0 / 不明なら None。
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from app.services.threads_openai_provider import proposal_schema
from app.social.threads.conversation import (
    ACTIVE_HOOKS,
    HOOKS,
    LEGACY,
    conversation_errors,
    engagement_rates,
    hook_from_provenance,
    hook_seed,
    plan_hooks,
    select_hook,
)
from app.social.threads.policy import get_policy as get_style_policy
from app.social.threads.prompt import build_prompt, parse_generated


def _seed(i: int) -> str:
    return hook_seed(article_id=i % 25 + 1, angle="insight", link_mode="none",
                     as_of=f"2026-09-{i % 28 + 1:02d}T00:00:{i % 60:02d}+00:00")  # fmt: skip


# == selection ========================================================================
def test_the_hook_enum_is_fixed() -> None:
    assert HOOKS == ("none", "question", "choice", "experience", "opinion")
    assert ACTIVE_HOOKS == HOOKS[1:]


def test_selection_is_deterministic_and_covers_every_hook() -> None:
    assert select_hook("same") == select_hook("same")
    assert {select_hook(_seed(i)) for i in range(200)} == set(HOOKS)


def test_the_long_run_distribution_is_about_one_in_five_each() -> None:
    counts = Counter(select_hook(f"request-{i}") for i in range(10_000))
    for hook in HOOKS:
        assert 0.18 <= counts[hook] / 10_000 <= 0.22, counts
    active = sum(counts[h] for h in ACTIVE_HOOKS) / 10_000
    assert 0.78 <= active <= 0.82  # 約 5 本に 4 本がきっかけ付き


def test_a_batch_avoids_adjacent_repeats_reproducibly() -> None:
    seeds = [f"batch-{i}" for i in range(50)]
    first, second = plan_hooks(seeds), plan_hooks(seeds)
    assert first == second
    assert all(a != b for a, b in zip(first, first[1:], strict=False))
    same = ["x", "x", "x"]  # 同じ入力が並んでも、隣は同じにならない
    assert len({*plan_hooks(same)[:2]}) == 2


# == validation =======================================================================
GOOD = {
    "none": (
        "議事録ツールは、まず録音の置き場所を決めると選びやすい。形式がそろうと比べ方もぶれない。"
    ),
    "question": (
        "議事録は会議中に取る派と、後でファイルから起こす派で選ぶ道具が変わる。"
        "今の会議はどっちが多い？"
    ),
    "choice": "無料プランの広さを取るか、ノイズ除去を取るか。議事録ツールはここで分かれる。",
    "experience": "ファイルの上限を先に確かめると迷いにくい。導入のとき、どこで引っかかった？",
    "opinion": "月払いと年払いをそろえずに比べるのは危ない、と思っている。別の見方もありそう。",
}


@pytest.mark.parametrize("hook", HOOKS)
def test_reasonable_posts_pass_for_every_hook(hook) -> None:
    errors, _warnings = conversation_errors(GOOD[hook], hook)
    assert errors == []


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("役に立ったらいいねしてください。", "asks for comments, likes"),
        ("フォローお願いします。要点は一つ。", "asks for comments, likes"),
        ("感想はコメントで教えてください。", "asks for comments, likes"),
        ("リポストで広めてほしい。", "asks for comments, likes"),
        ("みんな困っている話。要点は一つ。", "unsupported audience"),
        ("この書き方だとアルゴリズムに乗りやすい。", "recommendation or reach"),
        ("実際に使ってみたら早かった。", "first-person experience"),
        ("どれを選ぶ？どこで決める？", "more than one question"),
    ],
)
def test_bait_solicitation_and_invention_are_errors(body, reason) -> None:
    errors, _ = conversation_errors(body, "question")
    assert any(reason in e for e in errors), errors


def test_none_must_not_solicit_and_generic_hooks_only_warn() -> None:
    errors, _ = conversation_errors("要点は一つ。どう思いますか？", "none")
    assert any("must not solicit" in e for e in errors)
    _, warnings = conversation_errors("要点は一つ。料金で決める？", "none")
    assert any("prefer a plain ending" in w for w in warnings)
    errors, warnings = conversation_errors(
        "料金の比べ方は条件をそろえる。どう思いますか？", "opinion"
    )
    assert errors == [] and any("generic phrase" in w for w in warnings)
    _, warnings = conversation_errors(
        "安いのではないでしょうか。早いのではないでしょうか。", "none"
    )
    assert any("ではないでしょうか" in w for w in warnings)
    errors, _ = conversation_errors("x", "banter")
    assert errors == ["conversation_hook 'banter' is not allowed"]


def test_the_link_placeholder_is_not_counted() -> None:
    errors, warnings = conversation_errors("要点は一つ。\n詳しくは\n{link}", "none")
    assert errors == [] and warnings == []


# == prompt / parsing / schema ======================================================
def _prompt(hook=None):
    return build_prompt(
        source_article_id=7, source_article_title="AI議事録", source_article_body="本文。",
        source_article_body_hash="h", angles=["common_mistake"], policy=get_style_policy(),
        requested_link_mode="article", conversation_hook=hook,
    )  # fmt: skip


def test_the_prompt_adds_the_hook_only_when_requested() -> None:
    legacy = _prompt()
    assert "会話のきっかけ" not in legacy.rendered_prompt
    assert '"conversation_hook"' not in legacy.rendered_prompt
    for hook in HOOKS:
        text = _prompt(hook).rendered_prompt
        assert f"conversation_hook={hook}" in text and '"conversation_hook"' in text
        assert "誰も返信しなくても、それだけで役に立つ投稿にする" in text
        assert "いいね・フォロー・リポスト・シェア・コメントを頼まない" in text
        assert "リンクは補足" in text
        assert "事実の境界" in text and "500 文字以内" in text  # 前からの規則は残る
    assert _prompt("question").prompt_hash == _prompt("question").prompt_hash
    assert _prompt("question").prompt_hash != _prompt("opinion").prompt_hash
    with pytest.raises(ValueError):
        _prompt("banter")


def test_parsing_keeps_the_returned_hook() -> None:
    drafts = parse_generated(json.dumps({"proposals": [
        {"angle": "insight", "conversation_hook": "choice", "link_mode": "none", "body": "x"}
    ]}))  # fmt: skip
    assert drafts[0]["conversation_hook"] == "choice"
    legacy = parse_generated(json.dumps({"proposals": [{"angle": "insight", "body": "x"}]}))
    assert "conversation_hook" not in legacy[0]
    with pytest.raises(ValueError):
        parse_generated(json.dumps({"proposals": [
            {"angle": "insight", "conversation_hook": 3, "body": "x"}]}))  # fmt: skip


def test_the_schema_pins_the_requested_hook() -> None:
    item = proposal_schema(("insight",), "experience")["properties"]["proposals"]["items"]
    assert item["required"] == ["angle", "conversation_hook", "link_mode", "body"]
    assert item["properties"]["conversation_hook"] == {"type": "string", "enum": ["experience"]}
    assert item["additionalProperties"] is False
    legacy = proposal_schema(("insight",))["properties"]["proposals"]["items"]
    assert legacy["required"] == ["angle", "link_mode", "body"]
    assert "conversation_hook" not in legacy["properties"]


# == measurement helpers ===========================================================
def test_rates_are_safe_for_zero_or_unknown_views() -> None:
    assert engagement_rates(views=0, replies=3) == {
        "reply_rate": None, "quote_rate": None, "share_rate": None}  # fmt: skip
    assert engagement_rates(views=None)["reply_rate"] is None
    rates = engagement_rates(views=200, replies=2, quotes=1, shares=4)
    assert rates == {"reply_rate": 0.01, "quote_rate": 0.005, "share_rate": 0.02}


def test_legacy_proposals_are_never_given_an_invented_hook() -> None:
    assert hook_from_provenance(None) == LEGACY
    assert hook_from_provenance({"fingerprint": "x"}) == LEGACY
    assert hook_from_provenance({"generation_brief": {"conversation_hook": "banter"}}) == LEGACY
    assert hook_from_provenance({"generation_brief": {"conversation_hook": "choice"}}) == "choice"


def test_the_previous_proposals_hook_is_not_repeated() -> None:
    first = select_hook("next-request")
    assert plan_hooks(["next-request"], previous=first) != [first]
    assert plan_hooks(["next-request"], previous=None) == [first]
    assert plan_hooks(["next-request"], previous="legacy") == [first]  # 記録の無い提案は無視
