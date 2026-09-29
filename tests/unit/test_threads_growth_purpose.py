"""Growth Post の目的 (``threads-growth-purpose-1``, pure)。

pin する契約 (#36 の失敗から):

- 開発の出来事 → 直した → 学んだ、で終わる投稿は Growth として通さない (開発日記だけ)。
- 開発の話を含んでも、何をしているアカウントか・これから何を共有するか・誰とつながりたいか
  につながっていれば通る。
- 記事の要約・一般的な励まし・お願いのしすぎは通さない。作った実績は既存の事実の規則で落ちる。
- Luna の自己評価は否決にだけ使う (良いと言っても規則に落ちたものは通さない)。
- 書き方の軸と結びは最近の Growth Post と違うものを優先する (弱い好み。同じでも警告だけ)。
- Growth の prompt には目的の節が必ず入り、成績の参考 (T6.5) は入らない。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.social.threads import growth_purpose as gp
from app.social.threads.growth import GrowthBrief, build_prompt, growth_reason_ids, validate
from app.social.threads.growth_strategy import (
    FAMILIES,
    VALIDATION_FORMAT,
    VALIDATION_PURPOSE,
    Strategy,
    classify_validation,
)

DAY = date(2026, 9, 30)

#: 本番の提案 #36 (2026-09-29、人が却下した)。Growth なのに開発の出来事の報告で終わる。
NG_36 = (
    "Threadsの投稿を読み取る仕組みで、本文だけを取り出すのが難しい。画面に表示される文字まで本文に"
    "混ざってしまい、読み取った内容をそのまま扱えないことがありました。\n\n"
    "そこで、表示文字が混ざらないように読み取り方を直しました。AIのLunaにThreads投稿の下書きを"
    "作ってもらい、作業の自動化も進めながら検証・記録しています。みなさんは自動化で思わぬ情報が"
    "混ざった経験はありますか？よければコメントで教えてください。"
)
NG_DIARY = (
    "今日はAIで投稿の下書きを自動で作る仕組みを直していました。エラーの原因を追いかけて、"
    "設定のズレが問題だと分かりました。\n\n直したら無事に動くようになり、とても勉強になりました。"
    "自動化はやっぱり奥が深いです。明日も続きを進めます。"
)
NG_ARTICLE = (
    "生成AIを業務に取り入れるときのポイントは3つです。まず、目的をはっきり決めること。"
    "次に、社内のルールを作ること。最後に、小さく試して自動化の範囲を少しずつ広げること。\n\n"
    "段階的に進めるのがコツです。AIの活用は、急がず着実に進めましょう。ツール選びよりも、"
    "使い方の設計のほうが大切です。"
)
NG_GENERIC = (
    "毎日少しずつでも前に進むことが大切だと感じます。うまくいかない日があっても、諦めずに"
    "続けることで、きっと道は開けるはずです。\n\nAIも自動化も、焦らず一歩ずつ。今日も一緒に"
    "頑張りましょう！小さな積み重ねが、いつか大きな力になると信じています。"
)
NG_EXCESSIVE_CTA = (
    "AIでメディア運営をどこまで自動化できるか、作りながら検証しているアカウントです。"
    "フォローお願いします！フォローしてくれたら必ずフォロバします。\n\n今すぐフォローして、"
    "一緒に伸ばしましょう。フォロー大歓迎です！これからも自動化の結果を共有していきます。"
)
NG_FABRICATED = (
    "AIで記事サイトとThreadsの運用を自動化しているアカウントです。ついにフォロワー100人を"
    "達成しました！\n\nこれからも自動化の仕組みづくりを共有していきます。同じように自動化に"
    "取り組んでいる方、よければフォローしてください。"
)

OK_INTRO = (
    "はじめまして。AIでブログやThreadsの運用をどこまで自動化できるか、実際に仕組みを作りながら"
    "試しているアカウントです。\n\nうまくいったやり方も、つまずいたところも、ここで正直に共有して"
    "いきます。AI活用や自動化に興味がある方、よかったらフォローして見守ってください。"
)
OK_GOAL = (
    "このアカウントの目標は、AIと自動化で記事サイトの運営を一人でも回せる形にすることです。"
    "いまはまずフォロワー100人を目指しています。\n\nその途中で見えてきたことは、これからも"
    "投稿で共有していきます。同じように自動化を試している方とつながれたら嬉しいです。"
)
OK_CONNECTION = (
    "AIで記事づくりや投稿の自動化を試しているアカウントです。\n\n同じようにAIを仕事やブログに"
    "取り入れようとしている方と、試したことを交換できたら嬉しいです。気軽につながってください。"
    "うまくいったやり方も、これから少しずつ共有していきます。"
)
OK_FUTURE = (
    "これから、AIで記事サイトとThreadsの運用を自動化していく過程を、この場所で記録・共有して"
    "いきます。\n\nどこまで任せられて、どこに人の確認が要るのか。実際に試した結果をそのまま"
    "載せていくので、自動化に興味のある方はぜひフォローしてください。"
)
#: 開発の話を材料にして、アカウントの価値につなげる (#36 と同じ事実)。
OK_BUILD_IN_PUBLIC = (
    "Threadsの投稿を読み取る仕組みで、画面の文字が本文に混ざる問題があり、読み取り方を"
    "直しました。\n\nこのアカウントでは、AIで収益メディアをどこまで自動化できるかを作りながら"
    "検証しています。こうした試行錯誤も含めてこれからも共有していくので、同じことに取り組む方と"
    "つながれたら嬉しいです。"
)
OK_BODIES = {"intro": OK_INTRO, "goal": OK_GOAL, "connection": OK_CONNECTION,
             "future": OK_FUTURE, "build_in_public": OK_BUILD_IN_PUBLIC}  # fmt: skip


def _brief(family="build_in_public", cta="follow_connect", **kw) -> GrowthBrief:
    spec = FAMILIES[family]
    strategy = Strategy(family, spec.hooks[0], cta, spec.structures[0])
    return GrowthBrief(day=DAY, angle=family, follower_target=100, strategy=strategy, **kw)


# == #36 型 ====================================================================================
def test_the_36_post_is_rejected_as_a_development_diary() -> None:
    e = gp.evaluate(NG_36)
    assert e.accepted is False
    assert e.signals["development_diary_only"] is True
    assert e.signals["future_value_signal"] is False and e.signals["connection_signal"] is False
    assert e.signals["follow_invitation_signal"] is False
    assert gp.PROBLEM_WHY in e.problems and gp.PROBLEM_DIARY in e.problems
    # 理由を追える: どの文が開発の話か。
    assert e.as_dict()["evidence_sentences"]["development_sentences"][:3] == [0, 1, 2]
    verdict = validate(NG_36, _brief("failure_improvement", "experience_share"))
    assert verdict["ok"] is False
    assert {"growth_follow_reason_missing", "growth_development_diary_only"} <= set(
        growth_reason_ids("; ".join(verdict["problems"])))  # fmt: skip


def test_a_plain_development_diary_is_rejected() -> None:
    e = gp.evaluate(NG_DIARY)
    assert e.accepted is False and e.signals["development_diary_only"] is True


@pytest.mark.parametrize("name", sorted(OK_BODIES))
def test_growth_posts_that_connect_to_the_account_pass(name) -> None:
    e = gp.evaluate(OK_BODIES[name])
    assert e.accepted, (name, e.problems)
    assert e.signals["identity_signal"] or e.signals["account_purpose_signal"]
    assert (e.signals["future_value_signal"] or e.signals["connection_signal"]
            or e.signals["follow_invitation_signal"])  # fmt: skip
    assert validate(OK_BODIES[name], _brief())["ok"], validate(OK_BODIES[name], _brief())


def test_build_in_public_passes_only_when_connected_to_account_value() -> None:
    connected = gp.evaluate(OK_BUILD_IN_PUBLIC)
    assert connected.signals["build_in_public_signal"] and connected.accepted
    assert connected.signals["development_diary_only"] is False
    assert gp.evaluate(NG_36).accepted is False  # 同じ事実でも、出来事で終われば通らない


@pytest.mark.parametrize(("body", "signal", "problem"), [
    (NG_ARTICLE, "article_summary_like", gp.PROBLEM_ARTICLE),
    (NG_GENERIC, "generic_motivation_only", gp.PROBLEM_GENERIC),
    (NG_EXCESSIVE_CTA, "excessive_cta", gp.PROBLEM_CTA),
])  # fmt: skip
def test_non_growth_shapes_are_rejected(body, signal, problem) -> None:
    e = gp.evaluate(body)
    assert e.signals[signal] is True and problem in e.problems and not e.accepted


def test_a_fabricated_achievement_is_rejected_by_the_existing_fact_rule() -> None:
    verdict = validate(NG_FABRICATED, _brief())
    assert "growth_milestone_claim" in growth_reason_ids("; ".join(verdict["problems"]))
    assert verdict["ok"] is False


def test_the_self_assessment_can_only_veto() -> None:
    honest = dict.fromkeys(gp.ASSESSMENT_FIELDS, False)
    flagged = {**honest, "development_diary_only": True}
    assert gp.evaluate(OK_INTRO, self_assessment=flagged).accepted is False
    assert any(p.startswith(gp.PROBLEM_SELF) for p in
               gp.evaluate(OK_INTRO, self_assessment=flagged).problems)  # fmt: skip
    glowing = {**honest, **dict.fromkeys(gp.POSITIVE_SIGNALS, True)}
    assert gp.evaluate(NG_36, self_assessment=glowing).accepted is False  # 規則は上書きしない
    recorded = gp.evaluate(OK_INTRO, self_assessment=glowing).as_dict()
    assert recorded["source"] == "rules+self_assessment_veto"
    assert gp.evaluate(OK_INTRO, self_assessment={"identity_signal": "yes"}).self_assessment is None


def test_purpose_failures_are_their_own_repairable_class() -> None:
    assert classify_validation(["growth_development_diary_only"]) == VALIDATION_PURPOSE
    # 形 (長さ) の問題と一緒なら形の分類 (書き直しは同じく 1 回、目的の指示も足される)。
    assert classify_validation(["growth_length", "growth_follow_reason_missing"]) == (
        VALIDATION_FORMAT)  # fmt: skip


def test_the_evaluation_is_deterministic() -> None:
    assert gp.evaluate(NG_36).as_dict() == gp.evaluate(NG_36).as_dict()


# == 書き方の揺らし ==============================================================================
def test_the_framing_avoids_the_recent_axis_and_cta() -> None:
    recent = [{"axis": "identity", "cta_kind": "follow"}, {"axis": "future_value",
                                                           "cta_kind": "connect"}]  # fmt: skip
    framing = gp.choose_framing(DAY, family="account_identity", strategy_cta="follow_connect",
                                recent=recent)  # fmt: skip
    assert framing.axis == "connection"  # identity と future_value は直近 2 本で使った
    assert framing.cta_kind != "follow"
    assert set(framing.avoided) == {"identity", "future_value", "follow"}
    # 合うものが全部最近と同じでも、選ぶ (弱い好み)。
    only = gp.choose_framing(DAY, family="milestone", strategy_cta="none",
                             recent=[{"axis": "goal", "cta_kind": "future_preview"},
                                     {"axis": "future_value"}])  # fmt: skip
    assert (only.axis, only.cta_kind) in {("goal", "future_preview"),
                                          ("future_value", "future_preview")}


def test_a_repeated_framing_is_a_warning_not_a_rejection() -> None:
    observed = gp.evaluate(OK_INTRO).framing
    brief = _brief(recent_framings=(dict(observed),))
    verdict = validate(OK_INTRO, brief)
    assert verdict["ok"] is True
    assert any("same growth framing" in w for w in verdict["warnings"])
    assert not any("same growth framing" in w for w in validate(OK_INTRO, _brief())["warnings"])


def test_no_cta_strategy_still_asks_for_a_future_preview() -> None:
    framing = gp.choose_framing(DAY, family="principle", strategy_cta="none", recent=[])
    assert framing.cta_kind == "future_preview"


# == prompt ====================================================================================
def test_the_growth_prompt_states_the_purpose_and_ignores_performance_feedback() -> None:
    framing = gp.choose_framing(DAY, family="failure_improvement",
                                strategy_cta="experience_share", recent=[])  # fmt: skip
    prompt = build_prompt(_brief("failure_improvement", "experience_share", framing=framing,
                                 facts=("読み取り方を直しました",)))  # fmt: skip
    assert "## この投稿の目的 (Growth Post。いちばん優先する)" in prompt
    assert "開発日記" in prompt and "出来事を主役にしないで" in prompt
    assert f"今回の中心: {gp.FRAMING_AXES[framing.axis]}" in prompt
    assert "growth_assessment" in prompt
    assert "成績の参考は Growth には使わない" in prompt
    assert "過去の成績からの補助の参考" not in prompt
    assert prompt.index("## この投稿の目的") < prompt.index("## 今日の投稿")


def test_the_assessment_schema_is_strict_booleans() -> None:
    schema = gp.assessment_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == list(gp.ASSESSMENT_FIELDS)
    assert {v["type"] for v in schema["properties"].values()} == {"boolean"}


# == 個人の話の検査 (「工夫」の「夫」で反応しない) ==========================================
_PERSONAL_PROBLEM = "no personal or family anecdotes"


@pytest.mark.parametrize("extra", [
    "投稿の書き方を工夫しています。",
    "こういう工夫をしています。",
    "仕組みが丈夫になるように直しています。",
    "まだ途中ですが大丈夫です。",
])  # fmt: skip
def test_words_that_merely_contain_a_person_character_are_not_personal(extra) -> None:
    verdict = validate(OK_FUTURE + extra, _brief())
    assert _PERSONAL_PROBLEM not in verdict["problems"]


@pytest.mark.parametrize("extra", [
    "夫にも手伝ってもらっています。",
    "妻と一緒に試しています。",
    "夫婦で運営しています。",
    "家族にも手伝ってもらっています。",
    "息子が使っています。",
    "本業の合間に進めています。",
    "会社員をしながら続けています。",
])  # fmt: skip
def test_real_personal_references_are_still_rejected(extra) -> None:
    verdict = validate(OK_FUTURE + extra, _brief())
    assert _PERSONAL_PROBLEM in verdict["problems"]
    assert "growth_personal_anecdote" in growth_reason_ids("; ".join(verdict["problems"]))
