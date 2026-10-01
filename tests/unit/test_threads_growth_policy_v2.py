"""Growth Post の目的と重複の方針 (2026-10-01、``threads-growth-purpose-2`` /
``growth-duplication-2``)。**通常の投稿の検査は変えない。**

pin する契約:

- Growth Post はフォロー・交流のための投稿。本文の中心が Growth の意図 (参加・フォロー・目標・
  つながり・コメント・こちらからも見に行く・応援し合う) であること。開発の出来事・学びが中心の
  本文は、最後にフォローのお願いを足しても通さない (#51 の形)。トピックの語・「100人」・
  「フォロー」の語だけでは足りない。
- Growth では同じ目的の繰り返しは正常。決まった言葉 (インサイト祭り・フォロワー100人・
  フォロー歓迎など) が共通するだけでは止めない。止めるのは、ほぼ同じ全文・同じ書き出し・
  同じ結び・数語だけ替えた同じ組み立て・言い回しが近すぎるもの (直近 2 本)。
- 通常の投稿の重複の検査と、開発の話の扱いは変わらない。Growth と通常の投稿は別の枠のまま
  (トピック・日 1 本・人の承認)。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.social.threads import growth as g
from app.social.threads import growth_purpose as gp
from app.social.threads import growth_strategy as gs
from app.social.threads import quality
from app.social.threads.growth import GrowthBrief, growth_reason_ids, validate

DAY = date(2026, 10, 2)  # インサイト祭りの期間 (10/4 まで)

#: 本番の提案 #51 (2026-10-01、人が却下した)。開発の出来事 + 最後にフォローのお願い。
N51 = ("AIで投稿の下書き生成や作業の自動化を進め、実際の画面と見比べてみました。自動で集めた"
       "データも、画面と照らし合わせないと小さなズレに気づけないと分かりました。\n\n"
       "このアカウントでは、AIを使ったメディア運営をどこまで自動化できるか、作りながら検証し、"
       "学びや結果を共有していきます。同じテーマに関心がある方は、これからの記録を見るために"
       "フォローして、経験があればコメントで教えてください。")  # fmt: skip
#: 人が示した方向の例 (固定の型ではない)。トピックは公開のときに付くので「#」は書かない。
FESTIVAL = ("インサイト祭り、参加します🙌\n\nまずはフォロワー100人が目標です！\n\n"
            "AI自動化・Web運用を実際に試しながら発信しています。\n\n"
            "同じように100人を目指している方、ぜひ一緒につながりましょう😊\n\n"
            "フォローいただいた方はこちらからも見に行きます！")  # fmt: skip
#: 同じ目的 (100 人・インサイト祭り) で、書き出し・組み立て・結びが違う。
SAME_GOAL_NEW_WORDS = ("AI自動化とWeb運用を、試しながら発信しているアカウントです。\n\n"
                       "インサイト祭りに参加中で、まずはフォロワー100人を目指しています。\n\n"
                       "同じ目標の方、気軽にコメントで話しかけてください。"
                       "お互いに見に行きましょう！")  # fmt: skip
PEER_CALL = ("100人を目指して頑張っている方、いませんか？\n\n"
             "AIで自動化を試しながら、Web運用について発信しています。\n\n"
             "フォローいただいたらこちらからも見に行きますし、"
             "コメントもとても嬉しいです🙂")  # fmt: skip


def _brief(family="participation", cta=None, topic="インサイト祭り") -> GrowthBrief:
    spec = gs.FAMILIES[family]
    strategy = gs.Strategy(family, spec.hooks[0], cta or spec.ctas[0], spec.structures[0])
    return GrowthBrief(day=DAY, angle=family, follower_target=100, strategy=strategy,
                       topic=topic)  # fmt: skip


def _ids(verdict: dict) -> set[str]:
    return set(growth_reason_ids("; ".join(verdict["problems"])))


# == Growth: 目的 ==================================================================================
def test_festival_participation_with_a_follow_goal_passes() -> None:
    verdict = validate(FESTIVAL, _brief("participation", "reciprocal_visit"))
    assert verdict["ok"], verdict["problems"]
    intents = set(gp.intent_set(FESTIVAL))
    assert {"participation", "follower_goal", "peers", "reciprocity"} <= intents


def test_the_51_development_episode_fails_even_with_a_closing_follow_request() -> None:
    e = gp.evaluate(N51)
    assert not e.accepted
    assert gp.PROBLEM_DEV_CENTER in e.problems and gp.PROBLEM_TAIL in e.problems
    assert {"growth_development_centered", "growth_tail_only"} <= _ids(
        validate(N51, _brief("follow_goal")))  # fmt: skip


def test_a_development_episode_centered_body_fails() -> None:
    body = ("今日はAIで投稿の下書きを作る仕組みのエラーを直しました。原因はデータのズレでした。\n\n"
            "直したら動くようになり、学びが多かったです。AI自動化について発信しています。"
            "フォローしてください。")  # fmt: skip
    e = gp.evaluate(body)
    assert not e.accepted and gp.PROBLEM_DEV_CENTER in e.problems


@pytest.mark.parametrize("body, missing", [
    # トピックの語だけ (参加の表明・呼びかけが無い)
    ("インサイト祭りの日です。AI自動化・Web運用を実際に試しながら発信しています。", "intents"),
    # 「100人」の語だけ (目標になっていない)
    ("AI自動化・Web運用を試しながら発信しています。記事は100人くらいに読まれました。", "intents"),
    # 「フォロー」の語が 1 回だけ
    ("AI自動化・Web運用を試しながら発信しているアカウントです。フォローしてください。", "intents"),
])  # fmt: skip
def test_a_single_word_is_not_enough_for_growth(body, missing) -> None:
    e = gp.evaluate(body)
    assert not e.accepted and gp.PROBLEM_INTENTS in e.problems


def test_human_decisions_on_record_are_reproduced() -> None:
    approved_42 = ("いまは、AIで収益メディアの運営をどこまで自動化できるか、実際に作りながら検証・"
                   "記録しています。WordPressの記事サイトやThreads投稿、Lunaによる下書き生成などに"
                   "取り組み、次は記録したデータを投稿の書き方の見直しに使えるか試します。"
                   "分かったことも共有していきます。\n\n同じようにAIを使った発信やメディア運営に"
                   "取り組む人と、気軽につながれたら嬉しいです。興味が近い方は、これからの記録を"
                   "見に来てください。")  # fmt: skip
    assert gp.evaluate(approved_42).accepted  # 人が承認した
    assert not gp.evaluate(N51).accepted  # 人が却下した


# == Growth: 重複 ==================================================================================
def _recent(*texts: str) -> list[dict]:
    return [{"ref": f"proposal #{i}", "text": t} for i, t in enumerate(texts, start=100)]


def test_the_same_goal_with_different_wording_passes() -> None:
    audit = g.recent_similarity(SAME_GOAL_NEW_WORDS, _recent(FESTIVAL))
    assert not audit["blocked"], audit["top"]
    assert audit["max_purpose_similarity"] >= 0.5  # 目的は同じ (止める理由にしない)
    assert validate(SAME_GOAL_NEW_WORDS, _brief("introduction", "comment_welcome"),
                    _recent(FESTIVAL))["ok"]  # fmt: skip


def test_the_same_festival_purpose_with_another_hook_and_cta_passes() -> None:
    audit = g.recent_similarity(PEER_CALL, _recent(FESTIVAL, SAME_GOAL_NEW_WORDS))
    assert not audit["blocked"], audit["top"]
    top = {c["ref"]: c for c in audit["top"]}
    assert all(c["opening"] < g.GROWTH_EDGE_MAX and c["closing"] < g.GROWTH_EDGE_MAX
               for c in top.values())  # fmt: skip


def test_sharing_only_the_stable_follow_welcome_phrases_passes() -> None:
    a = ("AI自動化について発信しています。フォロー歓迎です！コメントも歓迎です。"
         "一緒に頑張りましょう！")
    b = "Web運用の試行錯誤を記録しているアカウントです。フォロー歓迎、コメントも歓迎です。"
    assert g.wording_similarity(b, a) < g.GROWTH_WORDING_MAX
    assert not g.recent_similarity(b, _recent(a))["blocked"]


def test_a_near_identical_full_text_fails() -> None:
    copy = FESTIVAL.replace("😊", "✨")
    audit = g.recent_similarity(copy, _recent(SAME_GOAL_NEW_WORDS, PEER_CALL, "x", "y", FESTIVAL))
    assert audit["blocked"] and "near_copy" in audit["blocked_rules"]  # 少し古くても止める
    assert "growth_near_copy" in _ids(validate(copy, _brief(), _recent(FESTIVAL)))


def test_a_few_words_swapped_in_the_same_structure_fails() -> None:
    swapped = (FESTIVAL.replace("参加します", "参加しています").replace("ぜひ", "よければ")
               .replace("こちらからも見に行きます", "こちらからも遊びに行きます"))  # fmt: skip
    audit = g.recent_similarity(swapped, _recent(FESTIVAL))
    assert audit["blocked"] and set(audit["blocked_rules"]) & {"same_structure", "same_opening",
                                                                "same_closing", "near_copy"}


def test_the_same_opening_hook_in_the_latest_two_fails() -> None:
    other = ("インサイト祭り、参加します🙌\n\nAIとWeb運用の実験を発信しているアカウントです。"
             "同じように始めたばかりの方、コメントで話しましょう。")  # fmt: skip
    audit = g.recent_similarity(other, _recent(FESTIVAL))
    assert "same_opening" in audit["blocked_rules"]
    # 直近 2 本より前なら、書き出しが同じでも止めない (同じ目的を続けられる)
    older = g.recent_similarity(other, _recent(PEER_CALL, SAME_GOAL_NEW_WORDS, FESTIVAL))
    assert "same_opening" not in older["blocked_rules"]


# == 通常の投稿 ====================================================================================
def test_the_regular_post_duplication_policy_is_unchanged() -> None:
    a = "Notionの料金プランを比較しました。無料プランでも十分使えます。"
    b = "Notionの料金プランを比較しました。無料プランでも十分使えます！"
    assert quality.overlap(b, a)["containment"] >= 0.6  # 通常の投稿の上限はそのまま
    import inspect

    source = inspect.getsource(quality.overlap)
    assert "containment >= 0.6" in source and "growth" not in source.lower()


def test_development_episodes_are_not_rejected_for_regular_posts() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for module in ("app/social/threads/quality.py", "app/social/threads/stock.py",
                   "app/services/threads_proposal_stock_service.py"):  # fmt: skip
        assert "growth_purpose" not in (root / module).read_text(encoding="utf-8"), module
    findings, _warnings = quality.quality_findings(N51, None)
    assert not any("development" in f for f in findings)  # 通常の検査に Growth の目的は無い


# == 枠・運用 ======================================================================================
def test_the_growth_lane_stays_separate_with_one_post_a_day_and_human_approval() -> None:
    from datetime import UTC, datetime

    from app.social.threads.topic import topic_tag_for

    assert topic_tag_for("article") == "AI Threads"
    assert topic_tag_for("account_growth", at=datetime(2026, 10, 2, 3, 0, tzinfo=UTC)) == (
        "インサイト祭り")  # fmt: skip
    assert g.GROWTH_POST_TARGET_PER_JST_DAY == 1 and gs.MAX_GROWTH_MODEL_CALLS_PER_DAY == 4
    assert g.GROWTH_ANGLE_COLUMN == "account_growth"
    assert "## この投稿の目的" in g.build_prompt(_brief())
    assert "「#」は付けない" in g.build_prompt(_brief())


def test_development_families_are_never_chosen_for_growth() -> None:
    every = set(gs.FACT_KINDS) | {gs.FACT_FOLLOWER_COUNT, gs.FACT_GROWTH_TOPIC}
    chosen = set(gs.eligible_families(every))
    assert chosen <= set(gs.GROWTH_PURPOSE_FAMILIES)
    assert not chosen & set(gp.DEVELOPMENT_FAMILIES)
    order = gs.family_order(DAY, chosen, [])
    assert "lesson_learned" not in order


# == 下見で見つかった誤検知 (2026-10-01) =======================================================
def test_a_participation_sentence_starting_with_today_is_not_development() -> None:
    body = ("今日はインサイト祭りに参加します！\n\nまずはフォロワー100人を目標に、"
            "AI自動化・Web運用を実際に試しながら発信しています。\n\n"
            "同じように目標に向かっている方、一緒につながりましょう。")  # fmt: skip
    e = gp.evaluate(body)
    assert e.accepted, e.problems
    assert e.signals["development_centered"] is False


def test_connection_calls_are_not_counted_as_too_many_follow_requests() -> None:
    body = ("同じように発信している人はいませんか？\n\n"
            "AI自動化・Web運用を試しながら発信しています。"
            "同じ目標を追う人とつながりたいです。フォローは歓迎です。"
            "お互いに応援できたらうれしいです。"
            "同じテーマに取り組む方、気軽にコメントで話しかけてください。")  # fmt: skip
    e = gp.evaluate(body)
    assert e.signals["excessive_cta"] is False and e.accepted, e.problems
    pushy = body.replace("フォローは歓迎です。",
                         "今すぐフォローしてください！フォローお願いします！")
    assert gp.evaluate(pushy).signals["excessive_cta"] is True  # 押し売りは今まで通り落とす
