"""アカウントを育てる投稿 (Growth Post、T6.3.3) の方針・事実の境界・検査。

- 記事から作る通常の投稿に **足す** もの (通常の本数・在庫・計画は変えない)。
- JST の 1 日に多くても 1 本。07:00 から、その日の 24:00 まで。**取り戻さない** (昨日の分は
  今日に持ち越さない。期限を過ぎた提案は ``expires_at`` で公開されない)。
- 人の承認が要る (通常の投稿と同じ)。トピックなし・リンクなし (``link_mode=none``)。
- 事実の境界は記事ではなく、下の **アカウントの紹介** だけ。収益・人数・実績・個人の話は
  作らない。フォロワー数は、観測した記録があるときだけ使う (Luna に推測させない)。
- フォローのお願い・フォロバの言葉は **この種類だけ** に許す (通常の投稿の検査は変えない)。
  実際のフォロー返しは人が手で行う (ここでは文章を作るだけ)。
- 目標 (フォロワー 100 人) に届いた記録があれば、新しい Growth Post を作らずに止める。
  次の目標は人が決める (自動で 200・500 などにしない)。
- T6.3.3c: 書き方 (family / hook / CTA / structure、``growth_strategy``) を brief に載せる。
  目標の人数を必ず書くか・どんなお願いで終えるかは書き方で決まる (アカウントの紹介・事実の
  境界・リンク・絵文字・長さ・似ている度合いの上限は、どの書き方でも同じ)。
- Growth の目的 (``growth_purpose``): prompt に目的の節を必ず入れ、検査で目的 (何者か +
  フォローの理由、開発日記・記事の要約・一般的な励まし・お願いのしすぎでない) を確かめる。
  どの書き方でも同じ。優先順位は Growth の目的 > 事実・文体の規則 > 成績の参考 (T6.5 は
  Growth に使わない)。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.social.threads import growth_purpose as gp
from app.social.threads.growth_strategy import CTAS as GROWTH_CTAS
from app.social.threads.growth_strategy import FAMILIES, HOOKS, STRUCTURES, Strategy
from app.social.threads.quality import prose_length

GROWTH_POLICY_VERSION = "t6.3.3"
#: -2 (T6.3.3c): 書き方を選び、似すぎたら別の書き方で書き直す (1 日に多くても 4 回呼ぶ)。
#: -3: Growth の目的の節と目的の検査 (``growth_purpose``)、Luna の自己評価 (否決にだけ使う)。
#: -4 (2026-10-01): Growth の目的の書き方だけ・本文の中心の検査・Growth 専用の重複の方針。
GROWTH_GENERATOR_VERSION = "threads-growth-4"
#: 方針として有効か。本番で動かすには、さらに worker を ``--maintain-growth-posts`` で起動する。
GROWTH_POSTS_ENABLED = True
#: JST の 1 日あたりの目安 (上限でもある)。取り戻さない。
GROWTH_POST_TARGET_PER_JST_DAY = 1
#: 最初のフォロワーの目標 (人が決めた)。届いたら止まり、次の目標は人が決める。
GROWTH_FOLLOWER_TARGET = 100
#: この時刻 (JST) から、その日の Growth Post を作ってよい。
GROWTH_ELIGIBLE_FROM = time(7, 0)
#: 提案の ``angle`` の列に入れる値 (記事の切り口と混ざらないように、種類そのもの)。
GROWTH_ANGLE_COLUMN = "account_growth"

GROWTH_ANGLES = (
    "account_identity",
    "goal_progress",
    "build_in_public",
    "community",
    "mutual_growth",
)
GROWTH_ANGLE_INTENTS = {
    "account_identity": "このアカウントが何を作り、何を検証しているか",
    "goal_progress": "いまの目標 (フォロワーの目標) と、事実として分かる進み具合",
    "build_in_public": "自動化の仕組みを、作りながら公開していること",
    "community": "AI 活用・ブログ運営・自動化に取り組む人とつながりたいこと",
    "mutual_growth": "一緒に伸ばしていきたいこと・フォローを返すこと",
}

#: アカウントの紹介 (事実の境界)。これを超える話は書かない。
ACCOUNT_IDENTITY = (
    "AIを使って、収益メディアをどこまで自動化できるか。\n"
    "実際に作りながら検証・記録しているアカウント。"
)
#: このプロジェクトに実際にあるもの (触れてよい話題)。
PROJECT_AREAS = (
    "WordPress の記事サイト",
    "Threads への投稿",
    "AI (Luna) による投稿の下書きの生成",
    "作業の自動化",
    "投稿の計測と分析",
    "これからの収益化の実験 (まだ結果は無い)",
)

#: 本文の長さ (読み手が読む本文。URL は無い)。目安 120〜280 字。外れたら書き直しの対象。
GROWTH_PROSE_TARGET = (120, 280)
GROWTH_PROSE_BOUNDS = (80, 320)
#: Threads の上限 (T2 のまま)。
GROWTH_HARD_LIMIT = 500
#: 絵文字は控えめに。
GROWTH_MAX_EMOJI = 3
#: 最近の Growth Post と比べる件数と、本文全体の 3-gram の重なり (包含) の上限。-4: これは
#: 「ほぼ同じ全文」を止めるための上限 (0.80 以上で書き直し)。同じ目的の言葉 (インサイト祭り・
#: フォロワー100人・つながり・フォロー歓迎など) が共通するのは Growth では正常なので、言い回しの
#: 近さは ``GROWTH_WORDING_MAX`` で、決まった言葉を除いてから、直近
#: ``GROWTH_WORDING_WINDOW`` 本と比べる。
RECENT_GROWTH_WINDOW = 7
GROWTH_SIMILARITY_MAX = 0.8
#: -4: 決まった言葉を除いた言い回しの重なりの上限 (直近 2 本と比べる。以上で書き直し)。
GROWTH_WORDING_WINDOW = 2
GROWTH_WORDING_MAX = 0.5
#: -4: 書き出し・結びの文が、直近 2 本の書き出し・結びとこれ以上重なれば同じとみなす。
GROWTH_EDGE_MAX = 0.8
#: -4: 文の組み立て (文ごとの意図の並び) が同じで、言い回しがこれ以上重なれば「数語だけ替えた」。
GROWTH_SAME_STRUCTURE_WORDING = 0.35
#: フォロワー数の観測が「新しい」とみなす長さ (控えめに。生成の直前に読む)。
FOLLOWER_OBSERVATION_MAX_AGE = timedelta(hours=6)

FOLLOWER_SOURCE = "threads_user_insights:followers_count"


class GrowthPolicyError(ValueError):
    pass


# -- 日付 ----------------------------------------------------------------------------------


def growth_date(now: datetime, tz: ZoneInfo) -> date:
    return _aware(now).astimezone(tz).date()


def day_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """その日の Growth Post が公開されてよい [開始, 終わり) (UTC)。終わりは翌日 00:00 JST。"""

    start = datetime.combine(day, GROWTH_ELIGIBLE_FROM, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(UTC)
    return start, end


def next_eligible_at(now: datetime, tz: ZoneInfo) -> datetime:
    """次に Growth Post を作ってよくなる時刻 (今日の 07:00 か、明日の 07:00)。"""

    today = growth_date(now, tz)
    start, _end = day_window(today, tz)
    if _aware(now) < start:
        return start
    return day_window(today + timedelta(days=1), tz)[0]


# -- 切り口 ---------------------------------------------------------------------------------


def select_angle(day: date, previous: str | None) -> str:
    """日付から決定的に選ぶ (SHA-256)。直前の Growth Post と同じ切り口は避ける。"""

    digest = hashlib.sha256(f"growth:{day.isoformat()}".encode()).digest()
    index = int.from_bytes(digest[:4], "big") % len(GROWTH_ANGLES)
    angle = GROWTH_ANGLES[index]
    if angle == previous:
        angle = GROWTH_ANGLES[(index + 1) % len(GROWTH_ANGLES)]
    return angle


# -- フォロワー数 ----------------------------------------------------------------------------


@dataclass(frozen=True)
class FollowerObservation:
    """Threads のアカウントの指標 (``followers_count``) を実際に読んだ記録。"""

    count: int
    observed_at: datetime
    source: str = FOLLOWER_SOURCE

    def fresh(self, now: datetime) -> bool:
        age = _aware(now) - _aware(self.observed_at)
        return timedelta(0) <= age <= FOLLOWER_OBSERVATION_MAX_AGE

    def as_dict(self) -> dict:
        return {
            "followers_count": self.count,
            "observed_at": _aware(self.observed_at).isoformat(),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Mapping | None) -> FollowerObservation | None:
        if not isinstance(data, Mapping):
            return None
        count = data.get("followers_count")
        observed = data.get("observed_at")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0 or not observed:
            return None
        try:
            moment = datetime.fromisoformat(str(observed))
        except ValueError:
            return None
        return cls(count=count, observed_at=_aware(moment), source=str(data.get("source") or ""))


def target_reached(observation: FollowerObservation | None, target: int) -> bool:
    """観測した記録が目標に届いているか (記録の古さは問わない: 届いたら人が次を決める)。"""

    return observation is not None and observation.count >= target


# -- 生成の依頼 -----------------------------------------------------------------------------


@dataclass(frozen=True)
class GrowthBrief:
    day: date
    angle: str
    follower_target: int
    #: 新しい観測があるときだけ (無ければ目標だけを書く)。
    observation: FollowerObservation | None = None
    recent_bodies: tuple[str, ...] = ()
    #: T6.3.3c: 今回の書き方 (``None`` は T6.3.3 の書き方: 目標とお願いが必須)。
    strategy: Strategy | None = None
    #: この書き方で使ってよい事実 (``app/config/threads_growth_facts.json`` の公開してよい文)。
    facts: tuple[str, ...] = ()
    #: 前の候補が似すぎていたときの、新しい方向 (言い換えではなく、別の書き方)。
    retry_direction: str | None = None
    #: 今回の中心の軸と結びの種類 (``growth_purpose.choose_framing``、弱い好み)。
    framing: gp.Framing | None = None
    #: 最近の Growth Post の軸と結び (新しい順。同じなら警告だけ)。
    recent_framings: tuple[Mapping, ...] = ()
    #: -4: その日に Growth Post に付くトピック (企画。例: インサイト祭り)。無ければ None。
    topic: str | None = None

    @property
    def goal_required(self) -> bool:
        return self.strategy is None or FAMILIES[self.strategy.family].goal_required

    @property
    def remaining(self) -> int | None:
        if self.observation is None:
            return None
        return max(0, self.follower_target - self.observation.count)

    def allowed_counts(self) -> set[int]:
        allowed = {self.follower_target}
        if self.observation is not None:
            allowed |= {self.observation.count, self.remaining or 0}
        return allowed

    def as_dict(self) -> dict:
        out = {
            "date_jst": self.day.isoformat(),
            "angle": self.angle,
            "follower_target": self.follower_target,
            "follower_observation": self.observation.as_dict() if self.observation else None,
            "uses_follower_count": self.observation is not None,
        }
        if self.strategy is not None:
            out["strategy"] = self.strategy.as_dict()
            out["facts_used"] = len(self.facts)
        if self.framing is not None:
            out["framing"] = self.framing.as_dict()
        if self.topic:
            out["topic"] = self.topic
        return out


def profile_hash() -> str:
    """事実の境界 (アカウントの紹介) の hash。変われば古い Growth Post は古くなる。"""

    payload = chr(31).join([GROWTH_POLICY_VERSION, ACCOUNT_IDENTITY, *PROJECT_AREAS])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_prompt(brief: GrowthBrief) -> str:
    """Growth Post 専用の prompt (記事は渡さない)。"""

    progress = (
        f"- 現在のフォロワー: {brief.observation.count} 人 (Threads の指標を "
        f"{_aware(brief.observation.observed_at).isoformat(timespec='minutes')} に読んだ値)。"
        f"目標まであと {brief.remaining} 人。この 2 つの数だけは書いてよい。"
        if brief.observation is not None
        else "- 現在のフォロワー数は分からない。**今の人数・残りの人数は書かない** (目標だけ)。"
    )
    recent = (
        "\n".join(f"  - {_one_line(b)[:90]}" for b in brief.recent_bodies[:3])
        if brief.recent_bodies
        else "  - (まだ無い)"
    )
    if brief.strategy is not None:
        return _strategy_prompt(brief, progress, recent)
    return "\n".join(
        [
            "あなたは Threads のアカウントの中の人として、"
            "アカウントを紹介する短い投稿を 1 本書く。",
            "記事の宣伝ではない。**下の事実だけ** を使う。",
            "",
            "## アカウント (事実の境界)",
            ACCOUNT_IDENTITY,
            "実際にあるもの: " + "、".join(PROJECT_AREAS),
            "",
            "## 今日の投稿",
            f"- 日付 (JST): {brief.day.isoformat()}",
            f"- 切り口: {brief.angle} ({GROWTH_ANGLE_INTENTS[brief.angle]})",
            f"- 目標: まずはフォロワー {brief.follower_target} 人",
            progress,
            "",
            "## 書き方",
            f"- 本文は {GROWTH_PROSE_TARGET[0]}〜{GROWTH_PROSE_TARGET[1]} 字くらい。"
            "1〜3 の短い段落。",
            "- 親しみやすく、会話のように。会社の告知のようにしない。",
            f"- 絵文字は使っても {GROWTH_MAX_EMOJI} 個まで。",
            "- 入れること: このアカウントが何をしているか / いまの目標 (1 つ) / "
            "つながり・フォローのお願い (1 つ、自然に)。",
            "- フォローを返すことは書いてよい (例: フォローいただけたら、こちらからも"
            "フォローします / フォロバします / 一緒に伸ばしていきたいです)。",
            "- 書かないこと: 収益・売上・報酬・金額 / 利用者や顧客の数 / 達成した・"
            "突破したなどの実績 / 家族・仕事・生活などの個人の話 / 上に無い数字。",
            "- URL・リンク・{link}・ハッシュタグは書かない。",
            "- 最近の Growth Post と同じ言い回しにしない:",
            recent,
            "",
            *gp.prompt_section(family=brief.angle, framing=brief.framing, topic=brief.topic),
            "",
            "## 出力",
            f'JSON: {{"proposals": [{{"angle": "{brief.angle}", "link_mode": "none", '
            '"body": "...", "growth_assessment": {...}}]}',
            gp.ASSESSMENT_PROMPT,
        ]
    )


def _strategy_prompt(brief: GrowthBrief, progress: str, recent: str) -> str:
    """T6.3.3c: 書き方 (family / hook / CTA / structure) を指定する prompt。"""

    strategy = brief.strategy
    family = FAMILIES[strategy.family]
    facts = [f"- {text}" for text in brief.facts] or ["- (この書き方で使う追加の事実は無い)"]
    goal = (f"- いまの目標 (まずはフォロワー {brief.follower_target} 人) を本文に書く。"
            if brief.goal_required
            else f"- 目標 (フォロワー {brief.follower_target} 人) は、書いても書かなくてもよい。"
                 "書かないときは、何をしているアカウントかで伝える。")  # fmt: skip
    lines = [
        "あなたは Threads のアカウントの中の人として、アカウントを育てる短い投稿を 1 本書く。",
        "記事の宣伝ではない。**下の事実だけ** を使う。出来事・数字・実績を作らない。",
        "",
        *gp.prompt_section(family=strategy.family, framing=brief.framing, topic=brief.topic),
        "",
        "## アカウント (事実の境界)",
        ACCOUNT_IDENTITY,
        "実際にあるもの: " + "、".join(PROJECT_AREAS),
        "",
        *(["## 今回の書き方で使ってよい事実", *facts, ""] if brief.facts else []),
        "## 今日の投稿",
        f"- 日付 (JST): {brief.day.isoformat()}",
        f"- 書き方の種類: {strategy.family} ({family.intent})",
        f"- 書き出し: {HOOKS[strategy.hook]}",
        f"- 組み立て: {STRUCTURES[strategy.structure]}",
        f"- 結び: {GROWTH_CTAS[strategy.cta]}",
        progress,
        goal,
    ]
    if brief.retry_direction:
        lines += ["", "## 今回は新しい方向で書く", brief.retry_direction]
    lines += [
        "",
        "## 書き方",
        f"- 本文は {GROWTH_PROSE_TARGET[0]}〜{GROWTH_PROSE_TARGET[1]} 字くらい。",
        "- 親しみやすく、会話のように。会社の告知のようにしない。箇条書きを並べない。",
        f"- 絵文字は使っても {GROWTH_MAX_EMOJI} 個まで。",
        "- 必ず入れること: 短い自己紹介 1 文 (AI と自動化について発信していること。"
        "例: AI自動化・Web運用を実際に試しながら発信しています)。",
        "- フォローを返すことは書いてよいが、必ず返す・全員に返信する、とは約束しない。",
        "- 書かないこと: 収益・売上・報酬・金額 / 利用者や顧客の数 / 達成した・"
        "突破したなどの実績 / 家族・仕事・生活などの個人の話 / 上に無い数字。",
        ("- URL・リンク・{link}・ハッシュタグ (「#」) は書かない "
         "(トピックは公開のときに自動で付く)。" if brief.topic
         else "- URL・リンク・{link}・ハッシュタグ・トピックの言葉は書かない。"),
        "- 最近の Growth Post と同じ言い回し・同じ書き出し・同じ結びにしない "
        "(目的が同じなのはよい):",
        recent,
        "",
        "## 出力",
        f'JSON: {{"proposals": [{{"angle": "{brief.angle}", "link_mode": "none", '
        '"body": "...", "growth_assessment": {...}}]}',
        gp.ASSESSMENT_PROMPT,
    ]
    return "\n".join(lines)


# -- 検査 ---------------------------------------------------------------------------------

_URL = re.compile(r"https?://|www\.|\{link\}|/go/|utm_|\.(?:com|jp|net|org)\b", re.I)
_PEOPLE = re.compile(r"(\d[\d,，]*)\s*(?:人|名)")
_MONEY = re.compile(r"\d[\d,，.]*\s*(?:円|万円|万|ドル|USD)|[$¥￥]\s*\d")
_MONEY_WORDS = re.compile(r"稼い|稼げ|月収|年収|売上|売り上げ|報酬|コミッション|利益|収入")
_MILESTONE = re.compile(r"達成|突破|到達|超えました|超えた|記念")
#: 「夫」「妻」は人を指す語として見る (「工夫」「丈夫」「大丈夫」「農夫」「漁夫」「稲妻」の中の
#: 1 文字では反応しない)。「夫婦」「夫人」などは人を指すので残る。
_PERSONAL = re.compile(r"家族|(?<!稲)妻|(?<![工丈農漁水])夫|子ども|子供|息子|娘|本業|会社員|"
                       r"脱サラ|副業で|育児|実家")  # fmt: skip
_CUSTOMERS = re.compile(r"\d[\d,，]*\s*(?:社|件)|利用者|顧客|お客様|ユーザー数")
_HASHTAG = re.compile(r"(?:^|\s)#\S")
_CTA = re.compile(r"フォロー|フォロバ|つなが|繋が")
_IDENTITY_AI = re.compile(r"AI|ＡＩ|Luna", re.I)
_IDENTITY_AUTO = re.compile(r"自動")
_GOAL = re.compile(r"目標|目指")
_QUESTION = re.compile(r"[？?]")
_SHARE = re.compile(r"[？?]|教えて|聞かせ|コメント|シェア")
#: 書き方の結び (CTA) → 本文に要る言葉 (``None`` は要らない)。
_RECIPROCAL = re.compile(r"こちらからも|見に行|見にいき|フォロバ|フォロー返|お返し")
_TOGETHER_SUPPORT = re.compile(r"一緒に|応援|お互い|支え合|励まし合")
_CTA_PATTERN = {"follow_connect": _CTA, "mutual_growth": _CTA, "soft_connection": _CTA,
                "question": _QUESTION, "experience_share": _SHARE, "none": None,
                # -4
                "follow_welcome": _CTA, "connect_peers": _CTA, "comment_welcome": _SHARE,
                "reciprocal_visit": _RECIPROCAL, "mutual_support": _TOGETHER_SUPPORT}  # fmt: skip


def emoji_count(text: str) -> int:
    return sum(
        1
        for ch in text
        if unicodedata.category(ch) == "So" and ord(ch) >= 0x2190 and ch not in "→←↑↓"
    )


def similarity(candidate: str, recent: str) -> float:
    """3-gram の包含 (候補の 3-gram のうち、最近の投稿にもある割合)。"""

    a, b = _grams(candidate), _grams(recent)
    return round(len(a & b) / len(a), 3) if a else 0.0


#: -4: Growth の目的として毎回出てよい決まった言葉 (言い回しの比較の前に除く)。
_STABLE_GROWTH_PHRASES = re.compile(
    r"インサイト祭り|フォロワー\s*\d*\s*人?|\d+\s*人|目標|目指(?:して|す|し)?|"
    r"つなが(?:り|って|れ|る|りたい|りましょう)?|繋が(?:り|って|れ|る)?|フォロー|フォロバ|"
    r"歓迎|大歓迎|コメント|こちらからも|見に行き(?:ます)?|遊びに行き(?:ます)?|"
    r"一緒に|頑張(?:り|ろ)?|がんば(?:り|ろ)?|応援|よろしく(?:お願いします)?|"
    r"AI\s*自動化|Web\s*運用|自動化|発信(?:して)?(?:います|中)?|参加(?:します|しています|中)?|"
    r"同じように|同じ目標|気軽に|ぜひ|まずは|です|ます|ました|ません"
)


def _strip_stable(text: str) -> str:
    return _STABLE_GROWTH_PHRASES.sub(" ", unicodedata.normalize("NFKC", text or ""))


def wording_similarity(candidate: str, recent: str) -> float:
    """決まった Growth の言葉を除いたあとの 3-gram の包含 (言い回しの近さ)。"""

    a, b = _grams(_strip_stable(candidate)), _grams(_strip_stable(recent))
    return round(len(a & b) / len(a), 3) if a else 0.0


def _structure(text: str) -> tuple[str, ...]:
    """文ごとの Growth の意図の並び (組み立ての形)。"""

    return tuple("+".join(sorted(gp.growth_intents(s))) or "-" for s in gp.sentences(text))


def _edge(text: str, *, last: bool) -> str:
    items = gp.sentences(text)
    return (items[-1] if last else items[0]) if items else ""


def purpose_similarity(candidate: str, recent: str) -> float:
    """Growth の意図の種類の重なり (Jaccard)。**Growth では高くてよい** (止める理由にしない)。"""

    a, b = gp.intent_set(candidate), gp.intent_set(recent)
    return round(len(a & b) / len(a | b), 3) if (a | b) else 0.0


def recent_similarity(body: str, recent: Iterable[Mapping]) -> dict:
    """最近の Growth Post との比較 (-4: Growth 専用の重複の方針)。

    ``recent`` は新しい順。止める (書き直す) のは:

    - ほぼ同じ全文: 本文全体の 3-gram の包含が ``GROWTH_SIMILARITY_MAX`` 以上 (最近 7 本の
      どれか。少し古くても止める)
    - 直近 ``GROWTH_WORDING_WINDOW`` 本と比べて: 決まった言葉を除いた言い回しの包含が
      ``GROWTH_WORDING_MAX`` 以上 / 書き出しの文が同じ / 結びの文が同じ / 文の組み立てが同じで
      言い回しが ``GROWTH_SAME_STRUCTURE_WORDING`` 以上

    目的の重なり (purpose similarity) は記録するだけで、止める理由にしない。
    """

    items = [dict(i) for i in recent]
    body_structure = _structure(body)
    compared = []
    for index, item in enumerate(items):
        text = item.get("text") or ""
        latest = index < GROWTH_WORDING_WINDOW
        entry = {"ref": item.get("ref"), "similarity": similarity(body, text),
                 "wording": wording_similarity(body, text),
                 "purpose": purpose_similarity(body, text), "latest": latest,
                 "opening": similarity(_edge(body, last=False), _edge(text, last=False)),
                 "closing": similarity(_edge(body, last=True), _edge(text, last=True)),
                 "same_structure": body_structure == _structure(text)}  # fmt: skip
        rules = []
        if entry["similarity"] >= GROWTH_SIMILARITY_MAX:
            rules.append("near_copy")
        if latest:
            if entry["wording"] >= GROWTH_WORDING_MAX:
                rules.append("wording")
            if entry["opening"] >= GROWTH_EDGE_MAX:
                rules.append("same_opening")
            if entry["closing"] >= GROWTH_EDGE_MAX:
                rules.append("same_closing")
            if entry["same_structure"] and entry["wording"] >= GROWTH_SAME_STRUCTURE_WORDING:
                rules.append("same_structure")
        entry["rules"] = rules
        compared.append(entry)
    severity = ("near_copy", "wording", "same_structure", "same_opening", "same_closing")

    def rank_of(c: dict) -> int:
        return min((severity.index(r) for r in c["rules"]), default=len(severity))

    ranked = sorted(compared, key=lambda c: (rank_of(c), -c["similarity"], str(c["ref"])))
    blocked = [c for c in ranked if c["rules"]]
    worst = blocked[0] if blocked else None
    all_rules = sorted({r for c in blocked for r in c["rules"]}, key=severity.index)
    latest = [c for c in compared if c["latest"]]
    return {
        "policy": "growth-duplication-2",
        "recent_window": len(compared),
        "wording_window": GROWTH_WORDING_WINDOW,
        "max_similarity": max((c["similarity"] for c in compared), default=0.0),
        "threshold": GROWTH_SIMILARITY_MAX,
        "max_wording_similarity": max((c["wording"] for c in latest), default=0.0),
        "wording_threshold": GROWTH_WORDING_MAX,
        "max_purpose_similarity": max((c["purpose"] for c in latest), default=0.0),
        "top": ranked[:3],
        "blocked": bool(blocked),
        "blocked_by": worst["ref"] if worst else None,
        "blocked_rules": all_rules,
    }


def validate(body: str, brief: GrowthBrief, recent: Iterable[Mapping] = (), *,
             self_assessment: Mapping | None = None) -> dict:  # fmt: skip
    """Growth Post の決定的な検査。``problems`` は書き直しの理由 (残れば保存しない)。

    目的の検査 (``growth_purpose.evaluate``) もここで行う。``self_assessment`` は Luna の
    自己評価 (否決にだけ使う)。
    """

    text = body or ""
    problems: list[str] = []
    warnings: list[str] = []
    if not text.strip():
        problems.append("empty body")
    if len(text) > GROWTH_HARD_LIMIT:
        problems.append(f"the post is {len(text)} characters; the Threads limit is 500")
    prose = prose_length(text)
    low, high = GROWTH_PROSE_BOUNDS
    if not (low <= prose <= high):
        problems.append(
            f"prose is {prose} chars; keep it about {GROWTH_PROSE_TARGET[0]}-"
            f"{GROWTH_PROSE_TARGET[1]}"
        )
    if _URL.search(text):
        problems.append("a Growth Post must not contain a URL, a link or {link}")
    if _HASHTAG.search(text):
        problems.append("no hashtags (the topic is not chosen by the post)")
    allowed = brief.allowed_counts()
    for raw in _PEOPLE.findall(text):
        value = int(re.sub(r"[,，]", "", raw))
        if value not in allowed:
            problems.append(
                f"unsupported follower number {value} (only {sorted(allowed)} are known)"
            )
    if brief.observation is None and re.search(r"(?:現在|いま|今|あと)\s*\d", text):
        problems.append("the current follower count is unknown; do not state progress")
    if _MONEY.search(text) or _MONEY_WORDS.search(text):
        problems.append("no revenue, income, commission or money amounts")
    if _MILESTONE.search(text):
        problems.append("no achievement or milestone claims")
    if _PERSONAL.search(text):
        problems.append("no personal or family anecdotes")
    if _CUSTOMERS.search(text):
        problems.append("no user, customer or company counts")
    emojis = emoji_count(text)
    if emojis > GROWTH_MAX_EMOJI:
        problems.append(f"{emojis} emoji; use at most {GROWTH_MAX_EMOJI}")
    if not (_IDENTITY_AI.search(text) and _IDENTITY_AUTO.search(text)):
        problems.append("say what the account does (AI and automation of the media)")
    if brief.goal_required and not (_GOAL.search(text) and str(brief.follower_target) in text):
        problems.append(f"state the current goal ({brief.follower_target} followers)")
    cta = brief.strategy.cta if brief.strategy is not None else "follow_connect"
    pattern = _CTA_PATTERN[cta]
    if pattern is not None and not pattern.search(text):
        wanted = f"end with the requested {cta} closing"
        problems.append("include one natural invitation to follow or connect"
                        if pattern is _CTA else wanted)  # fmt: skip
    if brief.strategy is not None and brief.strategy.hook == "question":
        first = text.strip().split("\n\n", 1)[0]
        if not _QUESTION.search(first):
            problems.append("the requested question hook is missing from the opening")
    audit = recent_similarity(text, recent)
    if audit["blocked"]:
        ref = audit["blocked_by"]
        messages = {
            "near_copy": f"near copy of the recent growth post {ref}; write a new post",
            "wording": f"wording too close to the recent growth post {ref} (the same purpose "
                       "is fine; change the expressions)",
            "same_opening": f"same opening as the recent growth post {ref}; change the hook",
            "same_closing": f"same closing call as the recent growth post {ref}; change the CTA",
            "same_structure": f"same structure with a few words changed as the recent growth "
                              f"post {ref}; change the structure",
        }  # fmt: skip
        problems += [messages[r] for r in audit["blocked_rules"]]
    purpose = gp.evaluate(text, self_assessment=self_assessment)
    problems += list(purpose.problems)
    if gp.repeated_framing(purpose.framing, brief.recent_framings):
        warnings.append(f"same growth framing as the previous growth post "
                        f"({purpose.framing.get('axis')}/{purpose.framing.get('cta_kind')}); "
                        "vary it next time")  # fmt: skip
    if not (GROWTH_PROSE_TARGET[0] <= prose <= GROWTH_PROSE_TARGET[1]) and low <= prose <= high:
        warnings.append(f"prose is {prose} chars (target {GROWTH_PROSE_TARGET[0]}-"
                        f"{GROWTH_PROSE_TARGET[1]})")  # fmt: skip
    return {
        "ok": not problems,
        "problems": sorted(set(problems)),
        "warnings": warnings,
        "prose_length": prose,
        "character_count": len(text),
        "emoji": emojis,
        "similarity": audit,
        "purpose": purpose.as_dict(),
    }


#: 検査の理由の文 → 決定的な ID (監査用)。
GROWTH_REASON_IDS = (
    ("growth_length", re.compile(r"prose is \d+ chars|the Threads limit is 500|empty body")),
    ("growth_link", re.compile(r"must not contain a URL")),
    ("growth_hashtag", re.compile(r"no hashtags")),
    ("growth_unsupported_follower_count", re.compile(r"unsupported follower number|do not "
                                                      r"state progress")),  # fmt: skip
    ("growth_money_claim", re.compile(r"no revenue")),
    ("growth_milestone_claim", re.compile(r"no achievement")),
    ("growth_personal_anecdote", re.compile(r"no personal")),
    ("growth_customer_claim", re.compile(r"no user, customer")),
    ("growth_emoji", re.compile(r"emoji; use at most")),
    ("growth_identity_missing", re.compile(r"say what the account does")),
    ("growth_goal_missing", re.compile(r"state the current goal")),
    ("growth_cta_missing", re.compile(r"invitation to follow|end with the requested")),
    ("growth_hook_mismatch", re.compile(r"requested question hook")),
    ("growth_duplicate", re.compile(r"too similar to the recent growth post")),
    # -4: Growth 専用の重複の方針
    ("growth_near_copy", re.compile(r"near copy of the recent growth post")),
    ("growth_wording_duplicate", re.compile(r"wording too close to the recent growth post")),
    ("growth_same_opening", re.compile(r"same opening as the recent growth post")),
    ("growth_same_closing", re.compile(r"same closing call as the recent growth post")),
    ("growth_same_structure", re.compile(r"same structure with a few words changed")),
    ("growth_development_centered", re.compile(r"growth purpose: the body is centered on "
                                               r"development")),  # fmt: skip
    ("growth_intents_missing", re.compile(r"growth purpose: not enough growth intents")),
    ("growth_tail_only", re.compile(r"growth purpose: growth appears only as a closing")),
    ("growth_low_growth_share", re.compile(r"growth purpose: most sentences are not about")),
    ("growth_purpose_who_missing", re.compile(r"growth purpose: say who the account is")),
    ("growth_follow_reason_missing", re.compile(r"growth purpose: give a reason to follow")),
    ("growth_development_diary_only", re.compile(r"growth purpose: reads as a development")),
    ("growth_article_summary_like", re.compile(r"growth purpose: reads like an article")),
    ("growth_generic_motivation_only", re.compile(r"growth purpose: generic motivation")),
    ("growth_excessive_cta", re.compile(r"growth purpose: too many follow requests")),
    ("growth_self_assessment_flag", re.compile(r"growth purpose: the generator's own")),
    ("malformed_output", re.compile(r"malformed output")),
)


def growth_reason_ids(text: str) -> list[str]:
    found = [rid for rid, pattern in GROWTH_REASON_IDS if pattern.search(text or "")]
    return found or (["other"] if (text or "").strip() else [])


# -- helpers ------------------------------------------------------------------------------


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _grams(text: str) -> set[str]:
    norm = re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", text or "").lower())
    return {norm[i : i + 3] for i in range(max(0, len(norm) - 2))}


__all__ = [
    "ACCOUNT_IDENTITY",
    "FOLLOWER_OBSERVATION_MAX_AGE",
    "GROWTH_ANGLES",
    "GROWTH_ANGLE_COLUMN",
    "GROWTH_FOLLOWER_TARGET",
    "GROWTH_POLICY_VERSION",
    "GROWTH_POSTS_ENABLED",
    "GROWTH_POST_TARGET_PER_JST_DAY",
    "FollowerObservation",
    "GrowthBrief",
    "build_prompt",
    "day_window",
    "growth_date",
    "growth_reason_ids",
    "next_eligible_at",
    "profile_hash",
    "recent_similarity",
    "select_angle",
    "target_reached",
    "validate",
]
