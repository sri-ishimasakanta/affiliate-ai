"""T6.3.1: Threads の投稿案の質 (pure: 長さ・密度・きっかけの形・話題の指紋・重なり・prompt)。

pin する契約:

- 長さは読み手が読む本文 (URL・{link} を除く) で測る。目安 280〜360、420 まで可、420 超は
  書き直しの対象。日付の条件 (「〜年〜月時点」) があれば例外 (警告)。500 字の上限 (T2) は同じ。
- 金額 3 つ以上は書き直しの対象。軸 3 つ以上・数値 5 つ以上は警告。
- 「A と B、先に見るのはどちらですか？」は choice。question を求めたのに choice なら書き直し。
- 話題の指紋は決定的。同じ製品 + 同じ金額 + 同じ軸は、言い回しが違っても「重なり」。
  同じ記事というだけでは重ならない。完全一致は重なる。
- prompt: 1 投稿 1 要点の規則と最近の話題 (短い行) が入る。きっかけの 80/20 は変えない。
"""

from __future__ import annotations

from collections import Counter

from app.social.threads.conversation import ACTIVE_HOOKS, HOOKS, select_hook
from app.social.threads.policy import get_policy
from app.social.threads.prompt import build_prompt
from app.social.threads.quality import (
    MAX_RECENT_TOPIC_LINES,
    PREFERRED_RANGE,
    QUALITY_CEILING,
    hook_forms,
    money_figures,
    numeric_facts,
    overlap,
    prose_length,
    quality_findings,
    recent_overlap,
    recent_topic_lines,
    topic_signature,
)

# #12 (本番の最初の T6.3 の案) と同じ形の本文。
DENSE_12 = (
    "AI議事録を無料で続けたいなら、まず確認するのは「無料」の期間です。Fireflies.aiはFree $0・"
    "「Free forever」と記載。一方、Krispは7日間のFree Trialで、クレジットカード不要。期間の定めの"
    "ない無料プランがあるかは、公式情報では確認できませんでした。\n\n最初の一歩は、同じ会議の音声を"
    "両方で試して、文字起こしだけでなく音声品質も比べること。Krispはノイズキャンセリングも用途に"
    "含まれます。料金まで比較するなら、KrispはCoreが月$8/ユーザー、Fireflies.aiのProは月払い$18、"
    "年払い$10/seat/月です（2026年9月時点）。無料継続と音声品質、先に見るのはどちらですか？ {link}"
)
PRICE_11 = (
    "AI議事録選びでありがちな間違いは、月額の数字だけで比べること。Fireflies.aiは月払い$18、"
    "年払い$10、KrispのCoreは$8/月。支払い条件をそろえないと比較を誤りやすい。"
)


# == length / density =================================================================
def test_length_is_measured_on_the_prose_without_the_link() -> None:
    url = "https://bizfluxlab.com/ai-meeting-notes/?utm_campaign=x&utm_source=threads"
    assert prose_length("本文です。" + url) == prose_length("本文です。{link}") == 5
    assert prose_length(DENSE_12) < 360  # #12 の本文は 322 字前後 (残りは追跡 URL)


def test_length_policy_thresholds() -> None:
    ok = "あ" * 400
    soft, warnings = quality_findings(ok, None)
    assert soft == [] and any("above the preferred 360" in w for w in warnings)
    soft, _ = quality_findings("あ" * 421, None)
    assert any(f"within {QUALITY_CEILING}" in s for s in soft)
    dated = "あ" * 420 + "（2026年9月時点）"
    soft, warnings = quality_findings(dated, None)
    assert soft == [] and any("length exception" in w for w in warnings)
    assert PREFERRED_RANGE == (280, 360) and QUALITY_CEILING == 420
    assert get_policy().max_characters == 500  # 上限は T2 のまま


def test_a_dense_multi_price_post_is_a_repair_candidate() -> None:
    assert money_figures(DENSE_12) == ["$0", "$10", "$18", "$8"]
    soft, warnings = quality_findings(DENSE_12, "question")
    assert any("4 prices" in s for s in soft)
    assert any("comparison axes" in w for w in warnings)
    assert numeric_facts("2026年9月時点で3つ") == []  # 日付の条件と小さな数は数えない
    soft, _ = quality_findings("Proは$18、Coreは$8。", "none")
    assert soft == []  # 2 つまでは問題にしない


# == hook forms =======================================================================
def test_question_and_choice_are_distinguished() -> None:
    assert hook_forms("無料継続と音声品質、先に見るのはどちらですか？") == {"choice"}
    assert hook_forms("担当と道具、どっちから決める？") == {"choice"}
    assert hook_forms("無料期間の確認で、いちばん迷うのはどこ？") == {"question"}
    assert "experience" in hook_forms("導入のとき、どこで止まった？")
    assert "opinion" in hook_forms("道具が先という見方もあると思う。")
    assert hook_forms("要点は一つ。それで十分。") == {"none"}


def test_12_style_ending_under_question_needs_a_repair() -> None:
    soft, _ = quality_findings(DENSE_12, "question")
    assert any("A/B choice" in s for s in soft)
    soft, _ = quality_findings("料金の比べ方は条件をそろえる。今の会議で迷うのはどこ？", "choice")
    assert any("no alternatives" in s for s in soft)
    soft, warnings = quality_findings("料金の比べ方は条件をそろえる。", "experience")
    assert soft == [] and any("reader's own use" in w for w in warnings)  # 警告にとどめる


# == topic signature / overlap ======================================================
def test_the_topic_signature_is_deterministic() -> None:
    a = topic_signature(article_id=8, angle="beginner_tip", link_mode="article", body=DENSE_12)
    b = topic_signature(article_id=8, angle="beginner_tip", link_mode="article", body=DENSE_12)
    assert a == b and len(a["fingerprint"]) == 16
    assert a["entities"] == ["fireflies.ai", "krisp"]
    assert {"$8", "$18", "$10"} <= set(a["numbers"])
    other = topic_signature(article_id=9, angle="beginner_tip", link_mode="article", body=DENSE_12)
    assert other["fingerprint"] != a["fingerprint"]


def test_same_products_prices_and_axis_overlap_even_with_different_wording() -> None:
    result = overlap(DENSE_12, PRICE_11)
    assert result["high"] and {"$8", "$18", "$10"} <= set(result["shared_numbers"])
    assert overlap(PRICE_11, PRICE_11)["high"]  # 完全一致


def test_the_same_article_with_a_different_point_does_not_overlap() -> None:
    noise = (
        "Krispはノイズキャンセリングを用途に挙げている。"
        "会議の聞き取りにくさが悩みなら、まずそこから。"
    )
    assert not overlap(noise, PRICE_11)["high"]
    free = "Fireflies.aiには期限のないFreeがある。まず無料で続けたいなら、ここを確認する。"
    assert not overlap(free, "HubSpotのStarterは$7から。通常は$20。")["high"]


def test_recent_context_is_short_and_bounded() -> None:
    recent = [{"ref": f"proposal #{i}", "text": PRICE_11 + f" 記事{i}"} for i in range(30)]
    lines = recent_topic_lines(recent)
    assert 1 <= len(lines) <= MAX_RECENT_TOPIC_LINES
    assert all(len(line) < 120 for line in lines) and "$18" in lines[0]
    assert recent_overlap(DENSE_12, recent)["ref"] == "proposal #0"
    assert recent_overlap("まったく別の話。", recent) is None


# == prompt / planner ===============================================================
def _prompt(hook="question", recent=None):
    return build_prompt(
        source_article_id=8, source_article_title="AI議事録無料", source_article_body="本文。",
        source_article_body_hash="h", angles=["beginner_tip"], policy=get_policy(),
        requested_link_mode="article", conversation_hook=hook, recent_topics=recent,
    )  # fmt: skip


def test_the_prompt_carries_the_one_point_rules_and_recent_topics() -> None:
    text = _prompt(recent=["fireflies.ai, krisp / free・pricing / $10, $18, $8"]).rendered_prompt
    for phrase in ("Threads は記事の圧縮ではない", "要点は 1 つだけ", "280〜360 字",
                   "420 字を超えない", "金額は多くても 2 つまで",
                   "最後に付け足した呼びかけにしない",
                   "二択・優先を聞くのは choice", "## 最近の話題", "$10, $18, $8"):  # fmt: skip
        assert phrase in text, phrase
    assert "事実の境界" in text and "500 文字以内" in text and "{link}" in text
    legacy = build_prompt(
        source_article_id=8, source_article_title="t", source_article_body="本文。",
        source_article_body_hash="h", angles=["beginner_tip"], policy=get_policy(),
    ).rendered_prompt  # fmt: skip
    assert "要点は 1 つだけ" not in legacy and "最近の話題" not in legacy  # 前の形のまま


def test_the_80_20_hook_distribution_is_unchanged() -> None:
    counts = Counter(select_hook(f"request-{i}") for i in range(10_000))
    active = sum(counts[h] for h in ACTIVE_HOOKS) / 10_000
    assert set(counts) == set(HOOKS) and 0.78 <= active <= 0.82
