"""Growth Post の目的 (``threads-growth-purpose-1``): 役割・柱・目的の検査・書き方の揺らし。

Growth Post は通常の投稿の追加の枠ではない。**第一の目的は、プロフィールを見てもらう・フォロー
してもらう・つながる・やり取りすること。** 開発日記・記事の別の切り口・一般的な励ましにしない。

読者が本文だけから、少なくとも次の **十分な組み合わせ** を分かること:

- A. このアカウント (人) は何者か (``identity`` / ``account_purpose``)
- B. これから何を発信するか (``future_value``)
- C. フォローすると何が得られるか (``future_value`` / ``follow_invitation``)
- D. どんな人とつながりたいか、または自然なやり取りの呼びかけ (``connection``)

検査 (``evaluate``) は決まった規則で **文ごとに** 見る (言葉が 1 つあるかだけではなく、どの文が
何をしているか・開発の話が本文のどれだけを占めるか)。生成した Luna の自己評価
(``growth_assessment``) は **否決にだけ** 使う (Luna が「開発日記だ」と言えば通さない。
Luna が「良い」と言っても、規則に落ちたものは通さない)。

優先順位 (T6.5 との境界): **Growth の目的 > 事実・文体の規則 > 成績の参考 (T6.5)**。
T6.5 の成績の参考は Growth の生成に使わない (Growth は n が少なく ``insufficient_data``。
参考は通常の投稿だけのもの)。成績の上で弱い書き方があっても、自己紹介や作りながらの公開を
禁止しない。強い書き方があっても、目的を満たさない投稿は通さない。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

#: -2 (2026-10-01): 本文の中心が Growth であること (参加・フォロー・目標・つながり・コメント・
#: こちらからも見に行く・応援し合う) を確かめる。開発の出来事・学びが中心の本文は、最後に
#: フォローのお願いを足しても通さない (#36・#51)。
GROWTH_PURPOSE_POLICY_VERSION = "threads-growth-purpose-2"

#: Growth の柱 (何を伝えるか)。``build_in_public`` は **補助の材料** で、単独では成立しない。
PILLARS: Mapping[str, str] = {
    "identity": "自己紹介: 何をしている人・アカウントか",
    "account_purpose": "このアカウントで何を発信・記録しているか",
    "future_value": "これからどんな情報・学び・検証の結果を共有するか",
    "goal": "何を目指しているか・何を作ろうとしているか",
    "connection": "同じ関心を持つ人との交流の呼びかけ",
    "follow_invitation": "自然なフォロー・続けて見てもらう案内",
    "build_in_public": "開発・検証の途中であること (補助の材料。これだけで終えない)",
}

#: 目的の検査の結果に出る信号。
POSITIVE_SIGNALS = ("identity_signal", "account_purpose_signal", "future_value_signal",
                    "connection_signal", "follow_invitation_signal")  # fmt: skip
NEGATIVE_SIGNALS = ("development_diary_only", "article_summary_like", "generic_motivation_only",
                    "excessive_cta")  # fmt: skip
#: Luna の自己評価の項目 (strict な JSON schema の boolean)。
ASSESSMENT_FIELDS = (*POSITIVE_SIGNALS, "development_diary_only", "article_summary_like",
                     "generic_motivation_only")  # fmt: skip

#: 採用の条件 (``evaluate`` の docstring も参照)。
GATE = {
    "who": "identity_signal or account_purpose_signal",
    "why_follow": "future_value_signal or connection_signal or follow_invitation_signal",
    "minimum_positive_signals": 2,
    "must_be_false": list(NEGATIVE_SIGNALS),
    "self_assessment": "veto only (a flagged negative rejects; a positive never overrides)",
}
MINIMUM_POSITIVE_SIGNALS = 2
#: 開発の話の文が本文のこの割合以上で、B/D の信号が無ければ「開発日記だけ」。
DIARY_SHARE = 0.5
#: 記事の説明のような文がこの割合以上で、自己紹介の印が無ければ「記事の要約のよう」。
ARTICLE_SHARE = 0.5
#: フォローの言葉・フォローのお願いの文がこれより多いと「お願いのしすぎ」(-2: つながり・交流の
#: 呼びかけは Growth Post の本題なので数えない。押し売りの言葉は不可)。
MAX_FOLLOW_MENTIONS = 2
MAX_CTA_SENTENCES = 2
#: -2: Growth の意図 (自己紹介とは別に) が少なくともこの数の種類あること。
MIN_GROWTH_INTENTS = 2
#: -2: 開発の話の文の割合がこれを超えるか、書き出しの文が開発の話なら、開発が中心とみなす。
DEVELOPMENT_MAX_SHARE = 0.25
#: -2: 自己紹介か Growth の意図を持つ文が、本文のこの割合以上あること。
GROWTH_SENTENCE_MIN_SHARE = 0.5
#: -2: Growth の意図の種類 (自己紹介 ``who`` は別に必須)。
GROWTH_INTENTS = ("participation", "follow_invitation", "follower_goal", "peers", "interaction",
                  "reciprocity", "mutual_support")  # fmt: skip

#: 検査の理由 (英語の文。``growth.GROWTH_REASON_IDS`` が ID に変える)。
PROBLEM_WHO = "growth purpose: say who the account is or what it shares"
PROBLEM_WHY = "growth purpose: give a reason to follow or connect (future value or connection)"
PROBLEM_DIARY = "growth purpose: reads as a development diary only"
PROBLEM_ARTICLE = "growth purpose: reads like an article summary"
PROBLEM_GENERIC = "growth purpose: generic motivation only"
PROBLEM_CTA = "growth purpose: too many follow requests"
PROBLEM_WEAK = "growth purpose: not enough growth signals"
PROBLEM_SELF = "growth purpose: the generator's own assessment flagged"
PROBLEM_DEV_CENTER = "growth purpose: the body is centered on development, not on connecting"
PROBLEM_INTENTS = ("growth purpose: not enough growth intents (participation / follow / goal / "
                   "peers / comments / follow-back / mutual support)")  # fmt: skip
PROBLEM_TAIL = "growth purpose: growth appears only as a closing line"
PROBLEM_GROWTH_SHARE = "growth purpose: most sentences are not about the account or connecting"

#: 目的に落ちたときの書き直しの指示 (同じ事実のまま、主役を入れ替える)。
REWRITE_INSTRUCTION = "\n".join([
    "Growth Post として書き直す (目的の検査に通らなかった):",
    "- 開発・実装・データの話・学びを本文に入れない (Growth Post の主題ではない)。",
    "- 自己紹介は 1 文で短く (例: AI自動化・Web運用を実際に試しながら発信しています)。",
    "- 本文の中心は、フォロワーの目標・同じ目標の人とつながりたいこと・フォロー歓迎・"
    "コメント歓迎・こちらからも見に行くこと・一緒に頑張ること、のうち 2 つ以上。",
    "- 最後の 1 文だけでお願いするのではなく、本文全体をつながりの呼びかけにする。",
    "- 元の事実以上のこと (出来事・数字・実績) を作らない。",
])  # fmt: skip

# -- 書き方の揺らし (soft preference) ----------------------------------------------------------

#: 中心にする軸 (最近の Growth Post と違うものを優先する。禁止ではない)。
FRAMING_AXES: Mapping[str, str] = {
    "identity": "自分が何者か・何をしているアカウントかを中心に",
    "goal": "何を目指しているかを中心に",
    "future_value": "これから何を共有していくか (フォローすると見られるもの) を中心に",
    "connection": "どんな人とつながりたいかを中心に",
    "current_build": "いま作っている仕組みを材料にして、アカウントの価値につなげる",
    "learning_journey": "学びながら進めていることを材料にして、これからの共有につなげる",
}
#: 結びの種類 (最近と違うものを優先する)。
CTA_KINDS: Mapping[str, str] = {
    "follow": "自然なフォローの案内 (続けて見てもらう)",
    "connect": "つながりの呼びかけ",
    "comment": "コメントでのやり取りの呼びかけ (これだけで終えず、フォローの理由も入れる)",
    "same_theme_call": "同じテーマに取り組む人への呼びかけ",
    "future_preview": "これからの発信の予告 (次に何を共有するか)",
}
#: 書き方の種類 (``growth_strategy.FAMILIES``) → 合う軸。
FAMILY_AXES: Mapping[str, tuple[str, ...]] = {
    "account_identity": ("identity", "future_value", "connection"),
    "goal_progress": ("goal", "future_value"),
    "build_in_public": ("current_build", "future_value", "connection"),
    "behind_the_scenes": ("current_build", "future_value"),
    "lesson_learned": ("learning_journey", "future_value"),
    "failure_improvement": ("learning_journey", "future_value"),
    "experiment": ("current_build", "future_value"),
    "community_question": ("connection", "identity"),
    "principle": ("identity", "future_value"),
    "next_step": ("future_value", "goal"),
    "milestone": ("goal", "future_value"),
    "mutual_growth": ("connection", "goal"),
    # -2: Growth の目的の書き方
    "participation": ("connection", "goal"),
    "follow_goal": ("goal", "connection"),
    "connect_with_peers": ("connection", "identity"),
    "introduction": ("identity", "connection"),
    "mutual_support": ("connection", "goal"),
    "comment_invitation": ("connection", "identity"),
}
#: 書き方の結び (``growth_strategy.CTAS``) → 合う結びの種類。``none`` でも予告は入れる
#: (結びなしの Growth を当たり前にしない)。
STRATEGY_CTA_KINDS: Mapping[str, tuple[str, ...]] = {
    "follow_connect": ("follow", "connect", "same_theme_call"),
    "mutual_growth": ("follow", "connect"),
    "question": ("comment", "same_theme_call"),
    "experience_share": ("comment", "same_theme_call"),
    "soft_connection": ("connect", "same_theme_call"),
    "none": ("future_preview",),
    # -2
    "follow_welcome": ("follow", "connect"),
    "connect_peers": ("same_theme_call", "connect"),
    "comment_welcome": ("comment", "same_theme_call"),
    "reciprocal_visit": ("follow", "connect"),
    "mutual_support": ("connect", "same_theme_call"),
}
#: 開発の話を材料にする書き方 (目的の柱につなげることを prompt で特に求める)。
DEVELOPMENT_FAMILIES = frozenset({"build_in_public", "behind_the_scenes", "lesson_learned",
                                  "failure_improvement", "experiment"})  # fmt: skip


# -- 文の規則 ---------------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[。！!？?])|\n+")
_DOMAIN = re.compile(r"AI|ＡＩ|Luna|自動化|自動で|メディア|ブログ|Threads|WordPress|記事|投稿|"
                     r"仕組み|計測|分析", re.I)  # fmt: skip
_SELF = re.compile(r"アカウント|私|僕|わたし|自分|中の人|個人で")
#: 自分の活動を言う文末 (主語を書かない日本語の自己紹介)。
_SELF_ACTIVITY = re.compile(r"(?:検証|記録|発信|運営|開発|自動化|挑戦|公開|共有|実験|作|試|育|"
                            r"進め|取り組|つく|まとめ)[^。！!？?\n]{0,6}(?:ています|中です|てます|"
                            r"ている(?:アカウント|ところ))")  # fmt: skip
_SHARE = re.compile(r"(?:発信|共有|記録|公開|シェア|届け|紹介|報告|まとめ)[^。！!？?\n]{0,4}"
                    r"(?:ています|てます|ている|していき|していく|します|する予定)")  # fmt: skip
_FUTURE = re.compile(
    r"(?:これから|今後|引き続き|これからも|この先|次は|次に|続けて)[^。！!？?\n]{0,40}"
    r"(?:発信|共有|紹介|公開|記録|届け|書いて|伝え|シェア|報告|載せ|出して)"
    r"|(?:発信|共有|紹介|公開|記録|シェア|報告)[^。！!？?\n]{0,4}(?:していきます|していく|"
    r"する予定|していこう|します)"
    r"|結果(?:も|を|は)[^。！!？?\n]{0,10}(?:共有|公開|報告|発信)"
    r"|(?:知りたい|気になる|興味のある|興味がある)(?:人|方)(?:は|に|へ)"
)  # fmt: skip
#: つながり・交流・会話したい意図。「コメントで教えて」だけの問いかけは ``_COMMENT`` で、ここには
#: 入れない (#36 のように、それだけではフォローの理由にならない)。
_CONNECTION = re.compile(r"(?:同じ|似た)[^。！!？?\n]{0,20}(?:人|方)|(?:取り組んで|試して|やって|"
                         r"興味(?:の|が)ある|関心(?:の|が)ある)[^。！!？?\n]{0,6}(?:人|方)|"
                         r"つなが|繋が|交流|仲間|(?:話し|語り合い|会話し)たい|話せたら|語り合え|"
                         r"意見交換|情報交換")  # fmt: skip
_TOGETHER = re.compile(r"一緒に")
_FOLLOW = re.compile(r"フォロー|フォロバ")
#: **このアカウントを続けて見る** 案内だけ (フォロー・今後も見て・続けてチェック・次の投稿も)。
#: 「つながる」だけでは入れない (それは ``_CONNECTION``)。
_FOLLOW_INVITE = re.compile(
    r"(?:フォロー|フォロバ)[^。！!？?\n]{0,20}(?:いただけ|もらえ|ください|お待ち|返し|します|"
    r"大歓迎|嬉し|うれし|して(?:ね|み)|どうぞ)"
    r"|(?:よければ|よかったら|気軽に|ぜひ)[^。！!？?\n]{0,20}フォロー"
    r"|(?:続けて|これからも|今後も|引き続き|次の投稿も|次回も)[^。！!？?\n]{0,12}"
    r"(?:見て|読んで|のぞいて|チェック|見守って|お付き合い)"
    r"|(?:見に|のぞきに|覗きに|遊びに)来て"
)  # fmt: skip
_COMMENT = re.compile(r"コメント|教えて|聞かせ|[？?]\s*$")
_GOAL = re.compile(r"目標|目指")
_BUILD = re.compile(r"作りながら|作っている|作っています|開発中|検証中|検証して|試して|組み立て|"
                    r"仕組みを|仕組みごと")  # fmt: skip
_DEVELOPMENT = re.compile(
    r"問題|不具合|エラー|バグ|直しました|直した|直す|修正|改善しました|うまくいかな|うまく動かな|"
    r"難しい|混ざ|詰まっ|ハマ|原因|解決|勉強になった|学びになった|分かりました|わかりました|"
    r"気づきました|対応しました|ズレ|ずれ|失敗|トラブル|今日は|昨日は|先日"
)  # fmt: skip
_EXPLAINER = re.compile(r"ポイント|コツ|方法|手順|とは|メリット|デメリット|注意点|おすすめ|"
                        r"まとめると|まず|次に|最後に|第一に|すべき|しましょう|大切です|"
                        r"重要です")  # fmt: skip
_BULLET = re.compile(r"^\s*(?:[・\-*•]|\d+[.)．]|[①②③④⑤])", re.M)
_MOTIVATION = re.compile(r"頑張|がんば|挑戦し続け|一歩ずつ|継続は力|諦めず|あきらめず|夢|前向き|"
                         r"成長し続け|大切なのは|信じ|負けず|努力")  # fmt: skip
_PUSHY = re.compile(r"今すぐフォロー|絶対(?:に)?フォロー|必ずフォロー|フォローしないと|拡散|"
                    r"フォロー(?:を)?お願いします[！!]{2,}")  # fmt: skip

# -- -2: Growth の意図 (本文の中心が Growth か) ----------------------------------------------
#: 企画への参加の表明 (「インサイト祭り」という語だけでは数えない)。
_PARTICIPATION = re.compile(r"(?:参加|参戦)(?:します|しました|しています|中|させて|してみ)|"
                            r"(?:祭り|企画)[^。！!？?\n]{0,10}(?:参加|参戦|混ぜ|乗っ)")  # fmt: skip
#: フォロワーの目標 (「100人」という語だけでは数えない)。
_FOLLOWER_GOAL = re.compile(r"(?:フォロワー|\d+\s*人)[^。！!？?\n]{0,15}(?:目標|目指)|"
                            r"(?:目標|目指)[^。！!？?\n]{0,15}(?:フォロワー|\d+\s*人)")  # fmt: skip
#: 同じ目標・関心の人への呼びかけ。
_PEERS = re.compile(r"(?:同じように|同じ目標|同じく|一緒に)[^。！!？?\n]{0,20}(?:人|方|仲間)|"
                    r"目指して(?:いる|る)(?:人|方)")  # fmt: skip
#: コメント・やり取りの歓迎。
_INTERACTION = re.compile(r"コメント|返信|やり取り|交換|話しかけ|話せたら|語り合|"
                          r"気軽に(?:声|話|絡|コメント)|絡んで|教えて")  # fmt: skip
#: こちらからも見に行く・フォローを返す。
_RECIPROCITY = re.compile(r"こちらからも|見に行|見にいき|遊びに行|フォロバ|フォロー返|"
                          r"お返し")  # fmt: skip
#: 一緒に伸ばす・応援し合う。
_MUTUAL = re.compile(r"一緒に(?:頑張|がんば|伸ば|目指|増や|進め|盛り上)|応援し合|お互い|"
                     r"励まし合|支え合|盛り上げ")  # fmt: skip
#: フォローの歓迎 (``_FOLLOW_INVITE`` に足す)。
_FOLLOW_WELCOME = re.compile(r"フォロー[^。！!？?\n]{0,8}(?:歓迎|大歓迎|嬉し|うれし|お待ち)")
#: Growth Post で主題にしない、開発・実装・データ・学びの話 (通常の投稿の検査には使わない)。
_DEV_DETAIL = re.compile(
    r"実装|デバッグ|データ|見比べ|照らし合わせ|気づけ|気づい|検証してみ|試してみ|作ってみ|"
    r"仕組みで|直し|設定を|処理|取り出|読み取|比較し|原因|改善し|学び|教訓|分かった|分かりました|"
    r"わかりました|ズレ|ずれ|不具合|エラー|バグ|修正|うまくいかな"
)  # fmt: skip

INTENT_PATTERNS: Mapping[str, re.Pattern] = {
    "participation": _PARTICIPATION,
    "follower_goal": _FOLLOWER_GOAL,
    "peers": _PEERS,
    "interaction": _INTERACTION,
    "reciprocity": _RECIPROCITY,
    "mutual_support": _MUTUAL,
}


def growth_intents(sentence: str) -> set[str]:
    """1 つの文の Growth の意図 (自己紹介は含めない)。"""

    found = {name for name, pattern in INTENT_PATTERNS.items() if pattern.search(sentence)}
    if _FOLLOW_INVITE.search(sentence) or _FOLLOW_WELCOME.search(sentence):
        found.add("follow_invitation")
    if _CONNECTION.search(sentence):
        found.add("peers")
    return found


def intent_set(body: str) -> set[str]:
    """本文全体の Growth の意図の種類 (重複の検査の purpose similarity にも使う)。"""

    out: set[str] = set()
    for sentence in sentences(body):
        out |= growth_intents(sentence)
    return out


def sentences(text: str) -> list[str]:
    parts = _SENTENCE_END.split(unicodedata.normalize("NFKC", text or ""))
    return [p.strip() for p in parts if p and p.strip()]


@dataclass(frozen=True)
class PurposeEvaluation:
    """目的の検査の結果 (決定的)。``evidence`` はどの文がどの信号を出したか (文の番号)。"""

    signals: Mapping[str, bool]
    evidence: Mapping[str, tuple[int, ...]]
    shares: Mapping[str, float]
    problems: tuple[str, ...]
    self_assessment: Mapping[str, bool] | None = None
    self_assessment_disagrees: tuple[str, ...] = ()
    framing: Mapping[str, str | None] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return not self.problems

    @property
    def positive_count(self) -> int:
        return sum(1 for s in POSITIVE_SIGNALS if self.signals.get(s))

    def as_dict(self) -> dict:
        return {
            "policy_version": GROWTH_PURPOSE_POLICY_VERSION,
            "accepted": self.accepted,
            "signals": dict(self.signals),
            "positive_signals": self.positive_count,
            "evidence_sentences": {k: list(v) for k, v in self.evidence.items() if v},
            "shares": dict(self.shares),
            "problems": list(self.problems),
            "self_assessment": dict(self.self_assessment) if self.self_assessment else None,
            "self_assessment_disagrees": list(self.self_assessment_disagrees),
            "framing": dict(self.framing),
            "source": "rules+self_assessment_veto" if self.self_assessment else "rules",
        }


def evaluate(body: str, *, self_assessment: Mapping | None = None) -> PurposeEvaluation:
    """Growth Post の目的の検査。

    採用の条件 (``GATE``): A (何者か) の信号が 1 つ以上・B/C/D (フォローの理由) の信号が
    1 つ以上・良い信号が合わせて 2 つ以上、かつ 開発日記だけ・記事の要約のよう・一般的な
    励ましだけ・お願いのしすぎ がどれも偽。Luna の自己評価は否決にだけ使う。
    """

    items = sentences(body)
    body_has_domain = bool(_DOMAIN.search(body or ""))
    hits: dict[str, list[int]] = {k: [] for k in (
        "identity", "account_purpose", "future_value", "connection", "follow_invitation",
        "comment", "goal", "build", "development", "explainer", "motivation", "cta")}  # fmt: skip
    follow_mentions = len(_FOLLOW.findall(body or ""))
    for i, sentence in enumerate(items):
        domain = bool(_DOMAIN.search(sentence))
        self_marked = bool(_SELF.search(sentence))
        if (self_marked or _SELF_ACTIVITY.search(sentence)) and (domain or body_has_domain):
            hits["identity"].append(i)
        if _SHARE.search(sentence) and (domain or self_marked or body_has_domain):
            hits["account_purpose"].append(i)
        if _FUTURE.search(sentence):
            hits["future_value"].append(i)
        if _CONNECTION.search(sentence) or (_TOGETHER.search(sentence)
                                            and (_FOLLOW.search(sentence) or domain)):
            hits["connection"].append(i)
        if _FOLLOW_INVITE.search(sentence):
            hits["follow_invitation"].append(i)
        if _COMMENT.search(sentence):
            hits["comment"].append(i)
        if _GOAL.search(sentence):
            hits["goal"].append(i)
        if _BUILD.search(sentence):
            hits["build"].append(i)
        if _DEVELOPMENT.search(sentence):
            hits["development"].append(i)
        if _EXPLAINER.search(sentence):
            hits["explainer"].append(i)
        if _MOTIVATION.search(sentence):
            hits["motivation"].append(i)
        # -2: お願いの文は、フォローそのものの依頼だけを数える (つながり・交流の呼びかけは
        # Growth Post の本題なので「お願いのしすぎ」に数えない)
        if (_FOLLOW.search(sentence) or _FOLLOW_INVITE.search(sentence)) and not _FUTURE.search(
                sentence):  # fmt: skip
            hits["cta"].append(i)
    n = len(items) or 1
    development_share = round(len(hits["development"]) / n, 3)
    explainer_share = round(len(hits["explainer"]) / n, 3)
    signals = {
        "identity_signal": bool(hits["identity"]),
        "account_purpose_signal": bool(hits["account_purpose"]),
        "future_value_signal": bool(hits["future_value"]),
        "connection_signal": bool(hits["connection"]),
        "follow_invitation_signal": bool(hits["follow_invitation"]),
        "goal_signal": bool(hits["goal"]),
        "build_in_public_signal": bool(hits["build"]),
        "interaction_cta_signal": bool(hits["comment"]),
    }
    who = signals["identity_signal"] or signals["account_purpose_signal"]
    why = (signals["future_value_signal"] or signals["connection_signal"]
           or signals["follow_invitation_signal"])  # fmt: skip
    # -2: Growth の意図 (目標・参加・コメント・こちらからも見に行く等) もフォローの理由になる
    why = why or any(growth_intents(s) for s in items)
    signals["development_diary_only"] = bool(
        hits["development"] and development_share >= DIARY_SHARE
        and not (signals["future_value_signal"] or signals["connection_signal"]))  # fmt: skip
    signals["article_summary_like"] = bool(
        (explainer_share >= ARTICLE_SHARE and not _SELF.search(body or "")
         and not signals["identity_signal"])
        or len(_BULLET.findall(body or "")) >= 3)  # fmt: skip
    signals["generic_motivation_only"] = bool(
        hits["motivation"] and not (signals["identity_signal"] or signals["account_purpose_signal"]
                                    or signals["future_value_signal"]))  # fmt: skip
    signals["excessive_cta"] = bool(follow_mentions > MAX_FOLLOW_MENTIONS
                                    or len(hits["cta"]) > MAX_CTA_SENTENCES
                                    or _PUSHY.search(body or ""))  # fmt: skip
    # -- -2: 本文の中心が Growth か (文ごと) ------------------------------------------------
    intro_idx = set(hits["identity"]) | set(hits["account_purpose"])
    per_intents = [growth_intents(s) for s in items]
    intents = set().union(*per_intents) if per_intents else set()
    # 開発・学びの話の文。アカウントの発信の予告 (「学びを共有していきます」) は数えない。
    dev_idx = [i for i, s in enumerate(items)
               if (_DEV_DETAIL.search(s) or _DEVELOPMENT.search(s))
               and not (_FUTURE.search(s) or _SHARE.search(s) or per_intents[i])]  # fmt: skip
    dev_share = round(len(dev_idx) / n, 3)
    growth_idx = [i for i in range(len(items)) if i in intro_idx or per_intents[i]]
    growth_share = round(len(growth_idx) / n, 3)
    intent_idx = [i for i in range(len(items)) if per_intents[i]]
    head = items[:-1]
    head_intro = sum(1 for i in range(len(head)) if i in intro_idx or _FUTURE.search(head[i]))
    signals["development_centered"] = bool(dev_idx and (0 in dev_idx
                                                        or dev_share > DEVELOPMENT_MAX_SHARE))
    signals["growth_tail_only"] = bool(len(items) >= 2 and intent_idx
                                       and set(intent_idx) == {len(items) - 1}
                                       and head_intro * 2 < len(head))  # fmt: skip
    problems = []
    if not who:
        problems.append(PROBLEM_WHO)
    if not why:
        problems.append(PROBLEM_WHY)
    if len(intents) < MIN_GROWTH_INTENTS:
        problems.append(PROBLEM_INTENTS)
    if signals["development_centered"]:
        problems.append(PROBLEM_DEV_CENTER)
    if signals["growth_tail_only"]:
        problems.append(PROBLEM_TAIL)
    if growth_share < GROWTH_SENTENCE_MIN_SHARE:
        problems.append(PROBLEM_GROWTH_SHARE)
    positives = sum(1 for s in POSITIVE_SIGNALS if signals[s])
    if who and why and positives < MINIMUM_POSITIVE_SIGNALS:
        problems.append(PROBLEM_WEAK)  # pragma: no cover - who と why で 2 つ以上になる
    for name, problem in (("development_diary_only", PROBLEM_DIARY),
                          ("article_summary_like", PROBLEM_ARTICLE),
                          ("generic_motivation_only", PROBLEM_GENERIC),
                          ("excessive_cta", PROBLEM_CTA)):  # fmt: skip
        if signals[name]:
            problems.append(problem)
    assessment = normalize_assessment(self_assessment)
    disagrees: list[str] = []
    if assessment is not None:
        disagrees = sorted(k for k in ASSESSMENT_FIELDS if assessment[k] != signals.get(k))
        flagged = [k for k in ("development_diary_only", "article_summary_like",
                               "generic_motivation_only") if assessment[k]]  # fmt: skip
        if flagged:
            problems.append(f"{PROBLEM_SELF} {', '.join(flagged)}")
    evidence = {
        "identity_signal": hits["identity"], "account_purpose_signal": hits["account_purpose"],
        "future_value_signal": hits["future_value"], "connection_signal": hits["connection"],
        "follow_invitation_signal": hits["follow_invitation"],
        "interaction_cta_signal": hits["comment"], "goal_signal": hits["goal"],
        "build_in_public_signal": hits["build"], "development_sentences": hits["development"],
        "explainer_sentences": hits["explainer"], "motivation_sentences": hits["motivation"],
        "cta_sentences": hits["cta"], "development_detail_sentences": dev_idx,
        "growth_sentences": growth_idx,
        **{f"intent_{name}": [i for i, found in enumerate(per_intents) if name in found]
           for name in GROWTH_INTENTS},
    }  # fmt: skip
    return PurposeEvaluation(
        signals=signals,
        evidence={k: tuple(v) for k, v in evidence.items()},
        shares={"development": development_share, "explainer": explainer_share,
                "sentences": len(items), "follow_mentions": follow_mentions,
                "development_detail": dev_share, "growth_sentences": growth_share,
                "growth_intents": len(intents)},  # fmt: skip
        problems=tuple(problems),
        self_assessment=assessment,
        self_assessment_disagrees=tuple(disagrees),
        framing=observed_framing(signals, hits, len(items)),
    )


def normalize_assessment(raw: Mapping | None) -> dict[str, bool] | None:
    """Luna の自己評価 (全部の項目が boolean のときだけ使う。欠けていれば無いものとする)。"""

    if not isinstance(raw, Mapping):
        return None
    if not all(isinstance(raw.get(k), bool) for k in ASSESSMENT_FIELDS):
        return None
    return {k: bool(raw[k]) for k in ASSESSMENT_FIELDS}


def observed_framing(signals: Mapping[str, bool], hits: Mapping[str, list[int]],
                     count: int) -> dict[str, str | None]:  # fmt: skip
    """本文から読み取れる軸と結びの種類 (最近の書き方の履歴に使う)。"""

    if signals.get("development_diary_only") is False and hits["development"]:
        axis = "learning_journey"
    elif signals.get("build_in_public_signal"):
        axis = "current_build"
    elif signals.get("goal_signal"):
        axis = "goal"
    elif signals.get("future_value_signal"):
        axis = "future_value"
    elif signals.get("connection_signal"):
        axis = "connection"
    else:
        axis = "identity" if signals.get("identity_signal") else None
    last = count - 1
    if hits["comment"] and last in hits["comment"]:
        cta = "comment"
    elif hits["follow_invitation"]:
        cta = "follow"
    elif hits["connection"]:
        cta = ("same_theme_call" if any(i in hits["connection"] for i in hits["cta"])
               and not hits["follow_invitation"] else "connect")  # fmt: skip
    elif hits["future_value"]:
        cta = "future_preview"
    else:
        cta = None
    return {"axis": axis, "cta_kind": cta}


@dataclass(frozen=True)
class Framing:
    """今回の Growth Post で中心にする軸と結びの種類 (弱い好み。禁止ではない)。"""

    axis: str
    cta_kind: str
    avoided: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"axis": self.axis, "cta_kind": self.cta_kind, "avoided": list(self.avoided)}


def choose_framing(day: date, *, family: str, strategy_cta: str,
                   recent: Sequence[Mapping[str, str | None]]) -> Framing:  # fmt: skip
    """最近 (新しい順) の 1〜2 本と同じ軸・同じ結びを避けて選ぶ (決定的)。合うものが全部
    最近と同じなら、それでも使う (弱い好み)。"""

    recent_axes = [r.get("axis") for r in recent[:2] if r.get("axis")]
    last_cta = next((r.get("cta_kind") for r in recent[:1] if r.get("cta_kind")), None)

    def rank(value: str) -> bytes:
        return hashlib.sha256(f"{GROWTH_PURPOSE_POLICY_VERSION}:{day}:{value}".encode()).digest()

    axes = FAMILY_AXES.get(family, tuple(FRAMING_AXES))
    axis = sorted(axes, key=lambda a: (a in recent_axes, rank(a)))[0]
    kinds = STRATEGY_CTA_KINDS.get(strategy_cta, tuple(CTA_KINDS))
    cta = sorted(kinds, key=lambda k: (k == last_cta, rank(k)))[0]
    avoided = tuple(sorted({*(a for a in recent_axes if a in axes),
                            *((last_cta,) if last_cta in kinds else ())}))  # fmt: skip
    return Framing(axis=axis, cta_kind=cta, avoided=avoided)


def repeated_framing(framing: Mapping[str, str | None],
                     recent: Iterable[Mapping[str, str | None]]) -> bool:  # fmt: skip
    """直前の Growth Post と同じ軸・同じ結び (警告だけ。検査は落とさない)。"""

    previous = next(iter(recent), None)
    return bool(previous and framing.get("axis") and framing.get("axis") == previous.get("axis")
                and framing.get("cta_kind") == previous.get("cta_kind"))  # fmt: skip


def prompt_section(*, family: str, framing: Framing | None,
                   topic: str | None = None) -> list[str]:  # fmt: skip
    """Growth の生成の prompt に必ず入れる、目的の節 (-2)。"""

    lines = [
        "## この投稿の目的 (Growth Post。いちばん優先する)",
        "目的は、読んだ人が「フォローしたい」「つながりたい」「話しかけたい」と感じること。",
        "開発の内容を説明する投稿ではない。通常の投稿の追加の枠・開発日記・記事の別の切り口"
        "でもない。",
        "本文の中心にするもの (2 つ以上を、本文全体で):",
        "- フォロワーの目標 (まずは 100 人) を目指していること",
        "- 同じように目標を目指す人・同じことに関心がある人と、一緒につながりたいこと",
        "- フォローを歓迎すること (押し付けない)",
        "- コメント・やり取りを歓迎すること",
        "- フォローしてくれた人のところへ、こちらからも見に行くこと",
        "- 一緒に頑張りたい・応援し合いたいこと",
        "自己紹介は 1 文で短く (例: AI自動化・Web運用を実際に試しながら発信しています 程度)。",
        "- 書かないこと: 開発の出来事・実装・デバッグ・データのズレ・仕組みの改善・学び・"
        "分かったこと (Growth Post の主題にしない)。",
        "- 最後に「フォローしてください」を 1 文足すだけの投稿にしない "
        "(本文全体を呼びかけにする)。",
        "- フォローの語は 2 回まで。押し売り (今すぐ・必ず・拡散) にしない。",
        "- 誇張・作った実績・作った体験談は書かない (上の事実だけ)。",
        "- 毎回同じ文にしない: 書き出し・段落の組み立て・結び・絵文字の置き方を変える。"
        "目的は同じでよい。",
    ]
    if topic:
        lines += [
            f"- 今日は Threads の企画「{topic}」に参加する日。トピックは公開のときに自動で付く。"
            f"本文では「{topic}」に参加していることを自然な言葉で伝えてよい (「#」は付けない)。",
        ]
    if framing is not None:
        lines += [
            f"- 今回の中心: {FRAMING_AXES[framing.axis]}。",
            f"- 今回の結び: {CTA_KINDS[framing.cta_kind]}。",
        ]
    lines.append("- 優先順位: この目的 > 事実・文体の規則 (成績の参考は Growth には使わない)。")
    return lines


def assessment_schema() -> dict:
    """Luna の自己評価の strict な JSON schema (``proposals[].growth_assessment``)。"""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(ASSESSMENT_FIELDS),
        "properties": {k: {"type": "boolean"} for k in ASSESSMENT_FIELDS},
    }


ASSESSMENT_PROMPT = (
    "growth_assessment には、書いた本文について正直に true / false を入れる "
    "(identity_signal: 何者か分かる / account_purpose_signal: 何を発信しているか分かる / "
    "future_value_signal: これから何を共有するか分かる / connection_signal: 誰とつながりたいか "
    "分かる / follow_invitation_signal: 自然なフォローの案内がある / development_diary_only: "
    "開発の出来事の報告だけ / article_summary_like: 記事の要約・解説のよう / "
    "generic_motivation_only: 一般的な励ましだけ)。"
)


__all__ = [
    "ASSESSMENT_FIELDS", "ASSESSMENT_PROMPT", "CTA_KINDS", "DEVELOPMENT_FAMILIES",
    "FAMILY_AXES", "FRAMING_AXES", "GATE", "GROWTH_PURPOSE_POLICY_VERSION", "NEGATIVE_SIGNALS",
    "PILLARS", "POSITIVE_SIGNALS", "REWRITE_INSTRUCTION", "STRATEGY_CTA_KINDS", "Framing",
    "PurposeEvaluation", "assessment_schema", "choose_framing", "evaluate",
    "normalize_assessment", "observed_framing", "prompt_section", "repeated_framing",
    "sentences",
]  # fmt: skip
