"""Growth Post の書き方の方針 (T6.3.3c、``threads-growth-strategy-1``)。**決まった規則だけ。**

目的は「JST の 1 日に、**検査を通った** Growth Post の提案を 1 本」(1 日 1 回の呼び出し、では
ない)。候補が最近の Growth Post に似すぎていたら、同じ話を言い換えるのではなく、**別の書き方**
(family / hook / CTA / structure) で新しく書く。検査は弱めない (似ている度合いの上限も同じ)。

- 1 日の呼び出しは多くても ``MAX_GROWTH_MODEL_CALLS_PER_DAY`` 回 (最初の生成・書き直し・別の
  書き方の生成を合わせて)。数は日ごとの記録のファイルに残る (再起動しても戻らない)。
- 書き方の順は、JST の日付・方針の版・最近の履歴・使える書き方・試みの番号だけで決まる
  (乱数なし。同じ入力なら同じ順)。
- 事実が要る書き方 (失敗と改善・実験・節目・次の一歩・裏側・進み具合) は、その事実が
  ``app/config/threads_growth_facts.json`` (人が書く、公開してよい文だけ) か、観測した
  フォロワー数に **あるときだけ** 使う。多様さのために出来事を作らない。
- Luna はトピックを選ばない (トピックは ``topic.py`` の日付の方針)。
- T6.5 (外の観察) の結果は、まだここに戻さない (将来の相談役の設計だけ)。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from itertools import product
from pathlib import Path

GROWTH_STRATEGY_POLICY_VERSION = "threads-growth-strategy-1"
#: JST の 1 日の Growth の呼び出しの上限 (最初・書き直し・別の書き方を合わせて)。
MAX_GROWTH_MODEL_CALLS_PER_DAY = 4
#: 書き方の履歴を見る件数 (最近の Growth Post の提案)。
RECENT_STRATEGY_WINDOW = 7
#: 同じ書き方で書き直してよい回数 (形の検査に落ちたとき)。
MAX_REPAIRS_PER_STRATEGY = 1

FACTS_PATH = Path(__file__).resolve().parents[3] / "app" / "config" / "threads_growth_facts.json"
FACT_FOLLOWER_COUNT = "follower_count"

# -- 書き方の部品 ------------------------------------------------------------------------------

HOOKS: Mapping[str, str] = {
    "question": "問いかけで始める (最初の段落に「？」)",
    "experience": "実際にやってみたことから始める (下の事実の範囲で)",
    "progress": "いまの進み具合から始める (観測した事実だけ)",
    "observation": "気づいたこと・見えてきたことから始める",
    "opinion": "このアカウントの考え方をひとことで言ってから始める",
    "lesson": "分かったこと (学び) から始める (下の事実の範囲で)",
    "challenge": "いま取り組んでいる難しさから始める (下の事実の範囲で)",
    "direct_statement": "何をしているアカウントかを、はっきり言い切って始める",
}
CTAS: Mapping[str, str] = {
    "follow_connect": "最後に、フォロー・つながりのお願いを 1 つ (自然に)",
    "mutual_growth": "最後に、一緒に伸ばしていきたいこと・フォローを返すことを 1 つ",
    "question": "最後に、読んだ人への問いかけを 1 つ (「？」で終える)",
    "experience_share": "最後に、読んだ人の経験を聞かせてほしいとお願いする (コメントで)",
    "soft_connection": "最後に、同じことに取り組む人とつながれたら嬉しい、と軽く添える",
    "none": "お願いは書かない (内容だけで終える)",
}
STRUCTURES: Mapping[str, str] = {
    "single_short_point": "1 つの短い段落で、言いたいことを 1 つだけ",
    "two_paragraph": "2 つの段落 (1 つ目で内容、2 つ目で結び)",
    "progress_then_invite": "進み具合 → お願い、の 2 つの段落",
    "lesson_then_question": "分かったこと → 問いかけ、の 2 つの段落",
    "observation_then_connection": "気づいたこと → つながりのお願い、の 2 つの段落",
    "question_then_context": "問いかけ → 背景 (なぜ聞くか)、の 2 つの段落",
}


@dataclass(frozen=True)
class Family:
    name: str
    intent: str
    #: この書き方に要る事実の種類 (``None`` = アカウントの紹介だけで書ける)。
    requires: str | None
    #: 目標 (フォロワー 100 人) を本文に必ず書くか。
    goal_required: bool
    hooks: tuple[str, ...]
    ctas: tuple[str, ...]
    structures: tuple[str, ...]


FAMILIES: Mapping[str, Family] = {
    f.name: f
    for f in (
        Family("account_identity", "このアカウントが何を作り、何を検証しているか", None, True,
               ("direct_statement", "question", "observation"),
               ("follow_connect", "soft_connection"),
               ("two_paragraph", "single_short_point")),
        Family("goal_progress", "いまのフォロワーの目標と、観測した進み具合", FACT_FOLLOWER_COUNT,
               True, ("progress", "direct_statement"), ("follow_connect", "mutual_growth"),
               ("progress_then_invite", "two_paragraph")),
        Family("build_in_public", "自動化の仕組みを、作りながら公開していること", None, False,
               ("experience", "observation", "direct_statement"),
               ("follow_connect", "soft_connection", "experience_share"),
               ("two_paragraph", "observation_then_connection")),
        Family("behind_the_scenes", "実際の運用の裏側 (下の事実だけ)", "behind_the_scenes", False,
               ("experience", "observation"), ("soft_connection", "question"),
               ("two_paragraph", "observation_then_connection")),
        Family("lesson_learned", "実際に分かったこと (下の事実だけ)", "lesson", False,
               ("lesson", "experience"), ("question", "experience_share"),
               ("lesson_then_question", "two_paragraph")),
        Family("failure_improvement", "実際にあった失敗と、それをどう直したか (下の事実だけ)",
               "failure_improvement", False, ("challenge", "experience"),
               ("experience_share", "question"), ("lesson_then_question", "two_paragraph")),
        Family("experiment", "いま実際にしている試み (下の事実だけ)", "experiment", False,
               ("observation", "challenge", "question"), ("experience_share", "soft_connection"),
               ("observation_then_connection", "two_paragraph")),
        Family("community_question", "AI 活用・ブログ運営・自動化に取り組む人への問いかけ", None,
               False, ("question",), ("question", "experience_share"),
               ("question_then_context", "single_short_point")),
        Family("principle", "このアカウントが大事にしている考え方", None, False,
               ("opinion", "direct_statement"), ("soft_connection", "follow_connect", "none"),
               ("single_short_point", "two_paragraph")),
        Family("next_step", "実際に予定している次の取り組み (下の事実だけ)", "next_step", False,
               ("direct_statement", "progress"), ("follow_connect", "soft_connection"),
               ("progress_then_invite", "two_paragraph")),
        Family("milestone", "実際にあった節目 (下の事実だけ)", "milestone", True, ("progress",),
               ("follow_connect", "mutual_growth"), ("progress_then_invite",)),
        Family("mutual_growth", "一緒に伸ばしていきたいこと・フォローを返すこと", None, True,
               ("direct_statement", "question", "opinion"), ("mutual_growth", "follow_connect"),
               ("two_paragraph", "single_short_point")),
    )
}  # fmt: skip
FAMILY_NAMES = tuple(FAMILIES)
#: T6.3.3 の切り口 (``angle``) → 今の family (古い提案の履歴を読むため)。
LEGACY_ANGLE_FAMILY = {"account_identity": "account_identity", "goal_progress": "goal_progress",
                       "build_in_public": "build_in_public", "community": "community_question",
                       "mutual_growth": "mutual_growth"}  # fmt: skip
FACT_KINDS = ("behind_the_scenes", "lesson", "failure_improvement", "experiment", "next_step",
              "milestone")  # fmt: skip


class GrowthStrategyError(ValueError):
    pass


# -- 失敗の分類 ----------------------------------------------------------------------------------

VALIDATION_SIMILARITY = "validation_similarity"
VALIDATION_FORMAT = "validation_format"
VALIDATION_HOOK = "validation_hook"
VALIDATION_FACT = "validation_fact"
PROVIDER_TRANSIENT = "provider_transient"
PROVIDER_AUTH = "provider_auth"
FOLLOWER_TARGET_REACHED = "follower_target_reached"
STRATEGY_EXHAUSTED = "strategy_exhausted"
MODEL_CALL_BUDGET_EXHAUSTED = "model_call_budget_exhausted"
FAILURE_CLASSES = (VALIDATION_SIMILARITY, VALIDATION_FORMAT, VALIDATION_HOOK, VALIDATION_FACT,
                   PROVIDER_TRANSIENT, PROVIDER_AUTH, FOLLOWER_TARGET_REACHED, STRATEGY_EXHAUSTED,
                   MODEL_CALL_BUDGET_EXHAUSTED)  # fmt: skip
#: その日の最後の結果 (候補なし)。
GROWTH_GENERATION_EXHAUSTED = "growth_generation_exhausted"

_REASON_CLASS = {
    "growth_duplicate": VALIDATION_SIMILARITY,
    "growth_unsupported_follower_count": VALIDATION_FACT,
    "growth_money_claim": VALIDATION_FACT,
    "growth_milestone_claim": VALIDATION_FACT,
    "growth_personal_anecdote": VALIDATION_FACT,
    "growth_customer_claim": VALIDATION_FACT,
    "growth_link": VALIDATION_FACT,
    "growth_hook_mismatch": VALIDATION_HOOK,
}
#: 分類の強さ (複数に落ちたら強い方で決める)。似すぎは言い換えでは直らないので最優先。
_CLASS_RANK = (VALIDATION_SIMILARITY, VALIDATION_FACT, VALIDATION_HOOK, VALIDATION_FORMAT)
#: provider の失敗の分類 (``GenerationError.category``)。
_PROVIDER_AUTH = frozenset({"auth", "bad_request", "not_found"})
_PROVIDER_TRANSIENT = frozenset({"timeout", "network", "rate_limited", "server_error", "http",
                                 "exhausted"})  # fmt: skip


def classify_validation(reason_ids: Iterable[str]) -> str:
    """検査の理由の ID → 失敗の分類 (1 つ)。形・長さ・目標・お願いの欠け等は ``format``。"""

    found = {_REASON_CLASS.get(r, VALIDATION_FORMAT) for r in reason_ids}
    return next((c for c in _CLASS_RANK if c in found), VALIDATION_FORMAT)


def classify_provider(category: str | None) -> str:
    """生成の失敗の分類。形の壊れた出力・断り・未完は、呼び出しとしては ``format``。"""

    if category in _PROVIDER_AUTH:
        return PROVIDER_AUTH
    if category in _PROVIDER_TRANSIENT:
        return PROVIDER_TRANSIENT
    return VALIDATION_FORMAT


# -- 事実 ---------------------------------------------------------------------------------------

_UNSAFE_FACT = re.compile(r"https?://|www\.|[\\/]|[A-Za-z]:\\|\.(?:py|db|json|env|md)\b|token|"
                          r"secret|password|api[_ -]?key|@|\bpid\b|localhost|127\.0|sk-",
                          re.I)  # fmt: skip


@dataclass(frozen=True)
class GrowthFact:
    id: str
    kind: str
    text: str
    valid_from: date
    valid_until: date

    def active(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_until


def load_facts(path: Path | None = None) -> tuple[GrowthFact, ...]:
    """公開してよい事実の一覧 (人が書く)。安全でない文 (URL・path・秘密の言葉) は例外。"""

    raw = json.loads((path or FACTS_PATH).read_text(encoding="utf-8"))
    facts = []
    for item in raw.get("facts", []):
        fact = GrowthFact(id=str(item["id"]), kind=str(item["kind"]), text=str(item["text"]),
                          valid_from=date.fromisoformat(item["valid_from"]),
                          valid_until=date.fromisoformat(item["valid_until"]))  # fmt: skip
        if fact.kind not in FACT_KINDS:
            raise GrowthStrategyError(f"unknown growth fact kind {fact.kind!r} ({fact.id})")
        if _UNSAFE_FACT.search(fact.text) or not fact.text.strip():
            raise GrowthStrategyError(f"growth fact {fact.id} is not public-safe")
        facts.append(fact)
    return tuple(facts)


def active_facts(facts: Iterable[GrowthFact], day: date) -> dict[str, tuple[GrowthFact, ...]]:
    out: dict[str, list[GrowthFact]] = {}
    for fact in facts:
        if fact.active(day):
            out.setdefault(fact.kind, []).append(fact)
    return {k: tuple(v) for k, v in out.items()}


def eligible_families(fact_kinds: Iterable[str]) -> tuple[str, ...]:
    """事実がそろっている書き方だけ (事実の要らない書き方はいつも使える)。"""

    have = set(fact_kinds)
    return tuple(n for n, f in FAMILIES.items() if f.requires is None or f.requires in have)


# -- 書き方 (signature) ------------------------------------------------------------------------


@dataclass(frozen=True)
class Strategy:
    family: str
    hook: str
    cta: str
    structure: str

    @property
    def signature(self) -> str:
        return f"{self.family}+{self.hook}+{self.cta}+{self.structure}"

    def as_dict(self) -> dict:
        return {"family": self.family, "hook": self.hook, "cta": self.cta,
                "structure": self.structure, "signature": self.signature}  # fmt: skip

    @classmethod
    def from_dict(cls, data: Mapping | None) -> Strategy | None:
        if not isinstance(data, Mapping):
            return None
        try:
            return cls(str(data["family"]), str(data["hook"]), str(data["cta"]),
                       str(data["structure"]))  # fmt: skip
        except KeyError:
            return None

    def differs_in(self, other: Strategy) -> int:
        """hook・CTA・structure のうち違う数。"""

        return sum(getattr(self, k) != getattr(other, k) for k in ("hook", "cta", "structure"))


@dataclass(frozen=True)
class HistoryItem:
    """最近の Growth Post の提案の書き方 (新しい順に並べて渡す)。"""

    date_jst: str
    family: str
    hook: str | None = None
    cta: str | None = None
    structure: str | None = None

    @property
    def signature(self) -> str | None:
        if None in (self.hook, self.cta, self.structure):
            return None
        return f"{self.family}+{self.hook}+{self.cta}+{self.structure}"


def _rank(*parts: str) -> bytes:
    return hashlib.sha256(":".join((GROWTH_STRATEGY_POLICY_VERSION, *parts)).encode()).digest()


def family_order(day: date, families: Iterable[str], history: list[HistoryItem]) -> list[str]:
    """その日の family の順 (決定的)。前の日と同じ family・最近多い family は後ろへ。"""

    previous = history[0].family if history else None
    recent = [h.family for h in history[:3]]
    return sorted(
        families,
        key=lambda f: ((f == previous) * 2 + (f in recent), _rank(day.isoformat(), f)),
    )


def signature_order(day: date, family: str, history: list[HistoryItem]) -> list[Strategy]:
    """その family の書き方の順 (決定的)。最近と同じ signature・前の日と同じ family+CTA・
    前の日と同じ hook は後ろへ (選べるものが無ければ、それでも使う)。"""

    spec = FAMILIES[family]
    recent_sigs = {h.signature for h in history[:RECENT_STRATEGY_WINDOW] if h.signature}
    previous = history[0] if history else None
    options = [Strategy(family, h, c, s) for h, c, s in product(spec.hooks, spec.ctas,
                                                                 spec.structures)]  # fmt: skip

    def penalty(s: Strategy) -> int:
        score = 4 * (s.signature in recent_sigs)
        if previous is not None:
            score += 2 * (previous.family == s.family and previous.cta == s.cta)
            score += previous.hook == s.hook
        return score

    return sorted(options, key=lambda s: (penalty(s), _rank(day.isoformat(), s.signature)))


def next_strategy(
    day: date,
    *,
    families: Iterable[str],
    history: list[HistoryItem],
    tried: list[Strategy],
) -> Strategy | None:
    """次に試す書き方。今日まだ試していない **別の family** を先に。family が尽きたら、試した
    同じ family の中で hook・CTA・structure の **2 つ以上** が違うもの。無ければ ``None``。"""

    order = family_order(day, families, history)
    used_families = {s.family for s in tried}
    for family in order:
        if family not in used_families:
            return signature_order(day, family, history)[0]
    for family in order:
        mine = [s for s in tried if s.family == family]
        for option in signature_order(day, family, history):
            if option in tried:
                continue
            if all(option.differs_in(t) >= 2 for t in mine):
                return option
    return None


def history_from_meta(rows: Iterable[tuple[str | None, Mapping | None]]) -> list[HistoryItem]:
    """``(date_jst, growth の印)`` の並び (新しい順) → 履歴。T6.3.3 の ``angle`` も読む。"""

    out = []
    for day, meta in rows:
        meta = meta or {}
        strategy = Strategy.from_dict(meta.get("strategy"))
        if strategy is not None:
            out.append(HistoryItem(day or "", strategy.family, strategy.hook, strategy.cta,
                                   strategy.structure))  # fmt: skip
            continue
        family = LEGACY_ANGLE_FAMILY.get(str(meta.get("angle")))
        if family:
            out.append(HistoryItem(day or "", family))
    return out


__all__ = [
    "CTAS", "FACT_FOLLOWER_COUNT", "FACT_KINDS", "FAILURE_CLASSES", "FAMILIES", "FAMILY_NAMES",
    "FOLLOWER_TARGET_REACHED", "GROWTH_GENERATION_EXHAUSTED", "GROWTH_STRATEGY_POLICY_VERSION",
    "HOOKS", "LEGACY_ANGLE_FAMILY", "MAX_GROWTH_MODEL_CALLS_PER_DAY", "MAX_REPAIRS_PER_STRATEGY",
    "MODEL_CALL_BUDGET_EXHAUSTED", "PROVIDER_AUTH", "PROVIDER_TRANSIENT", "RECENT_STRATEGY_WINDOW",
    "STRATEGY_EXHAUSTED", "STRUCTURES", "VALIDATION_FACT", "VALIDATION_FORMAT", "VALIDATION_HOOK",
    "VALIDATION_SIMILARITY", "Family", "GrowthFact", "GrowthStrategyError", "HistoryItem",
    "Strategy", "active_facts", "classify_provider", "classify_validation", "eligible_families",
    "family_order", "history_from_meta", "load_facts", "next_strategy", "signature_order",
]  # fmt: skip
