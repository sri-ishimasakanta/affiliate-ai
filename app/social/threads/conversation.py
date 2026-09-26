"""Threads の投稿案の「会話のきっかけ」(T6.3、pure・決定的)。

Threads は会話の場なので、投稿は **それだけで役に立つ** まま、自然に返事をしたくなる
入口を持つことがある。ただし、きっかけは任意であり、反応を釣る言い回し (engagement bait)
にはしない。これで表示や到達 (おすすめ) が増えるとは主張しない — 自分たちの結果を観測で
測るだけ。

きっかけの型 (``conversation_hook``):

- ``none``: 返事を求めない。自然に言い切って終わる。
- ``question``: 本文の内容からそのまま出てくる、具体的な問いを 1 つ。
- ``choice``: 本文の論点に沿った、意味のある二択・優先順位の選択。
- ``experience``: 読み手自身の導入・使い方で起きたことを聞く (自分たちの体験は作らない)。
- ``opinion``: 具体的な主張・トレードオフへの別の見方を歓迎する。

割り当て: 依頼の安定した入力の SHA-256 を 5 で割った余り (0→none、1→question、
2→choice、3→experience、4→opinion)。乱数は使わない。同じ依頼 (再試行・書き直しを含む) は
同じきっかけになる。隣 (1 回の計画の中の前の依頼、または DB にある直前の提案) と同じ型に
なるときは、次の型へずらす (決定的)。長く見れば約 5 本に 4 本がきっかけ付き、1 本が
``none``。**少ない観測から割合を自動で変えない。**
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping

BRIEF_VERSION = "t6.3"
HOOK_NONE = "none"
HOOKS = ("none", "question", "choice", "experience", "opinion")
ACTIVE_HOOKS = HOOKS[1:]
LEGACY = "legacy"

HOOK_BRIEFS = {
    "none": "返事を求めない。役に立つ結論と理由で自然に言い切って終わる。問いかけで終わらない。",
    "question": (
        "本文の内容からそのまま出てくる、具体的な問いを 1 つだけ置く。記事がそのまま答えて"
        "いる問いや、誰にでも言える問いにしない。"
    ),
    "choice": (
        "本文の論点に沿った、意味のある二択か優先順位の選択を 1 つ示す。どちらにも理由がある"
        "選び方だけにする。偽の二択にしない。"
    ),
    "experience": (
        "読み手自身の導入・使い方で起きたことを聞く。BizFluxLab の体験は作らない。個人情報や"
        "社内の機密を聞かない。"
    ),
    "opinion": (
        "本文の具体的な主張かトレードオフについて、別の見方や反対意見を歓迎する形にする。"
        "わざと対立を作らない。"
    ),
}

WRITING_RULES = (
    "Threads で詳しい人が普通に投稿するように書く。SEO 記事の要約にしない",
    "流れ: 役に立つ結論か観察 → 具体的な理由・違い・間違い・影響を 1 つ → "
    "(必要なら) 読み手への橋渡し → (必要なら) 自然なきっかけ",
    "誰も返信しなくても、それだけで役に立つ投稿にする",
    "きっかけは話題そのものから出す。反応を求める決まり文句で終わらない",
    "「どう思いますか？」「コメントで教えてください」「皆さんはどうですか？」「あなたはどっち派？」"
    "のような、中身の無い呼びかけを使わない",
    "いいね・フォロー・リポスト・シェア・コメントを頼まない",
    "「みんな困っている」のような根拠の無い読者像を書かない",
    "Threads のおすすめ・表示・アルゴリズムについて書かない",
    "link_mode=article のとき、リンクは補足。リンクを開くことを主な呼びかけにしない",
    "記事全体を要約しない。見出しや箇条書きを本文に入れない",
    "文の長さに少し変化をつける。少し口語でよい。「〜ではないでしょうか」を繰り返さない",
    "個人情報や社内の機密を聞かない",
)

_SOLICIT = re.compile(
    r"コメント(?:して|で教えて|ください|お願い|待って)|いいね(?:して|を|お願い|ください)"
    r"|フォロー(?:して|を|お願い|ください)|リポスト|再投稿して|シェア(?:して|お願い|ください)|拡散"
)
_AUDIENCE = re.compile(
    r"みんな(?:困って|悩んで|知らない)|誰もが(?:悩|困)|多くの人が(?:悩|困|知らない)"
)
_ALGORITHM = re.compile(r"アルゴリズム|おすすめに(?:載|出)|表示されやすく|伸びやすく|バズ")
_EXPERIENCE = re.compile(
    r"実際に使って|使ってみた|試してみた|試したところ|私は|僕は|弊社で|うちの会社"
)
_GENERIC = re.compile(
    r"どう思いますか|どう思う[?？]|皆さん(?:は|も)(?:どう|いかが)|あなたはどっち派|教えてください"
)
_FORMULAIC = re.compile(r"ではないでしょうか")


def select_hook(seed: str) -> str:
    """安定した入力 → きっかけの型 (SHA-256 の値を 5 で割った余り)。"""

    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return HOOKS[int.from_bytes(digest[:8], "big") % len(HOOKS)]


def plan_hooks(seeds: Iterable[str], *, previous: str | None = None) -> list[str]:
    """計画の依頼に順に割り当てる。隣 (直前に作った提案を含む) と同じ型なら次の型へずらす。

    ``previous`` は DB にある直前の提案のきっかけ (無ければ None)。同じ入力と同じ DB の状態
    からは同じ結果になる (決定的)。
    """

    out: list[str] = []
    before = previous if previous in HOOKS else None
    for seed in seeds:
        hook = select_hook(seed)
        if hook == before:
            hook = HOOKS[(HOOKS.index(hook) + 1) % len(HOOKS)]
        out.append(hook)
        before = hook
    return out


def hook_seed(*, article_id: int, angle: str, link_mode: str, as_of: str) -> str:
    return chr(31).join([str(article_id), angle, link_mode, as_of])


def _question_marks(text: str) -> int:
    return text.count("?") + text.count("？")


def conversation_errors(body: str, hook: str | None) -> tuple[list[str], list[str]]:
    """(errors, warnings)。errors があれば保存しない。好みの問題は warning にとどめる。"""

    text = unicodedata.normalize("NFKC", (body or "").replace("{link}", ""))
    errors: list[str] = []
    warnings: list[str] = []
    if hook is not None and hook not in HOOKS:
        errors.append(f"conversation_hook {hook!r} is not allowed")
        return errors, warnings
    if _SOLICIT.search(text):
        errors.append("the post asks for comments, likes, follows, reposts or shares")
    if _AUDIENCE.search(text):
        errors.append("the post claims an unsupported audience (e.g. 'みんな困っている')")
    if _ALGORITHM.search(text):
        errors.append("the post makes claims about Threads recommendation or reach")
    if _EXPERIENCE.search(text):
        errors.append("the post invents a first-person experience")
    if _question_marks(text) > 1:
        errors.append("the post asks more than one question")
    if hook == HOOK_NONE:
        if _GENERIC.search(text):
            errors.append("conversation_hook none must not solicit a response")
        elif _question_marks(text):
            warnings.append("conversation_hook none ends with a question; prefer a plain ending")
    elif hook in ACTIVE_HOOKS and _GENERIC.search(text):
        warnings.append("the conversation hook uses a generic phrase; tie it to the topic")
    if len(_FORMULAIC.findall(text)) > 1:
        warnings.append("「〜ではないでしょうか」 is repeated")
    return errors, warnings


def hook_from_provenance(provenance: Mapping | None) -> str:
    """保存した提案のきっかけ。T6.3 より前 (記録なし) は ``legacy`` (作り話で埋めない)。"""

    brief = (provenance or {}).get("generation_brief") or {}
    hook = brief.get("conversation_hook")
    return hook if hook in HOOKS else LEGACY


def engagement_rates(*, views, replies=0, quotes=0, shares=0) -> dict:
    """返信率・引用率・共有率 (views が 0 / 不明なら None)。**観測であって最適化ではない。**"""

    if not isinstance(views, int) or views <= 0:
        return {"reply_rate": None, "quote_rate": None, "share_rate": None}
    return {
        "reply_rate": round((replies or 0) / views, 6),
        "quote_rate": round((quotes or 0) / views, 6),
        "share_rate": round((shares or 0) / views, 6),
    }


__all__ = [
    "ACTIVE_HOOKS",
    "BRIEF_VERSION",
    "HOOKS",
    "HOOK_BRIEFS",
    "HOOK_NONE",
    "LEGACY",
    "WRITING_RULES",
    "conversation_errors",
    "engagement_rates",
    "hook_from_provenance",
    "hook_seed",
    "plan_hooks",
    "select_hook",
]
