"""Threads の投稿案の質の検査 (T6.3.1。pure・決定的)。

T6.3 の最初の本番の案 (#12) は、本文 322 字 (+ 追跡 URL 約 147 字 = 469 字) に 4 つの金額と
いくつもの比較の軸を詰め、最近の投稿と同じ Krisp / Fireflies の料金 ($8・$18・$10) を
繰り返した。完全一致の重複の検査では見つからない。ここではそれを決定的に扱う。

- **長さ** は読み手が読む本文 (追跡 URL と ``{link}`` を除く) で測る: 目安 280〜360 字、
  420 字まで可、420 字を超えたら書き直しの対象 (1 回)。例外は決定的: 「〜年〜月時点」の
  ように日付で条件を残している本文は、書き直しの後も 420 字を超えてよい (警告)。投稿全体の
  500 字 (URL 込み) の上限は変えない (T2 の検査)。
- **1 投稿 1 要点**: 金額が 3 つ以上なら書き直しの対象。比較の軸が 3 つ以上・数値の事実が
  5 つ以上は警告 (意味を完全には判定しない)。
- **きっかけの形**: 最後の問いが二択・優先 (「どちら」「A と B、先に〜」) なら ``choice``。
  ``question`` を求めたのに二択なら書き直しの対象 (逆も同じ)。experience / opinion は印が
  無ければ警告だけ。
- **最近の話題の重なり**: 最近の投稿・提案 (最大 ``RECENT_WINDOW`` 件) と、製品名・金額などの
  数値・比較の軸・文字 3-gram を比べる。同じ記事というだけでは重なりにしない。

どの判定も、事実の正しさ (fact_guard・T2 の検査) より弱い。短くするために事実を落とさない。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping

QUALITY_VERSION = "t6.3.1"
PREFERRED_RANGE = (280, 360)
QUALITY_CEILING = 420
RECENT_WINDOW = 12
MAX_MONEY_FIGURES = 2
MAX_AXES = 2
MAX_NUMERIC_FACTS = 4
MAX_RECENT_TOPIC_LINES = 8

_URL = re.compile(r"https?://\S+")
_MONEY = re.compile(r"(?:[$¥￥]\s*\d[\d,]*(?:\.\d+)?)|(?:\d[\d,]*(?:\.\d+)?\s*(?:円|ドル))")
_NUMBER = re.compile(
    r"(?:[$¥￥]\s*)?\d[\d,]*(?:\.\d+)?\s*"
    r"(?:%|円|ドル|件|人|名|日間|日|時間|分|か月|ヶ月|カ月|年|月|GB|MB|TB|倍|社|言語)?"
)
_ENTITY = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.\-][A-Za-z0-9]+)*")
# 製品名ではない一般の語・プランの名前 (重なりを水増ししない)。
_ENTITY_STOP = frozenset(
    {"ai", "api", "url", "seo", "sns", "pc", "id", "link", "threads", "bizfluxlab",
     "per", "seat", "user", "https", "http", "www", "com", "utm",
     "free", "forever", "trial", "pro", "core", "plan", "plans", "pricing", "starter", "basic",
     "business", "enterprise", "premium", "plus", "lite", "growth", "ultimate", "hub",
     "standard", "advanced", "tools", "sales", "customer", "platform", "credits", "onboarding",
     "smart", "crm", "sfa", "month", "year"}
)  # fmt: skip
#: 日付の条件 (「2026年9月時点」) は事実の数にも重なりにも数えない (どの投稿にもあるため)。
_DATE_UNIT = frozenset({"年", "月"})
AXES = {
    "pricing": re.compile(r"料金|価格|月額|年払い|月払い|有料|プラン|[$¥￥]\d|\d\s*円"),
    "free": re.compile(r"無料|Free|トライアル|Trial|お試し"),
    "accuracy": re.compile(r"精度|正確"),
    "audio": re.compile(r"音声品質|ノイズ|雑音|聞き取"),
    "integration": re.compile(r"連携|API|Slack|Notion|Zoom|Teams|Meet"),
    "setup": re.compile(r"導入|設定|始め|最初の一歩|初期"),
    "security": re.compile(r"セキュリティ|権限|情報漏"),
    "workflow": re.compile(r"運用|手順|業務|使い方|進め方"),
}
_DATED = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*時点|時点で")
_CHOICE = re.compile(
    r"どちら|どっち|どれを(?:先|優先)|優先するなら|先に(?:見る|決める|確認|選ぶ)の?は"
    r"|(?:と|か)[^。？?]{1,30}(?:なら|、)[^。？?]*(?:どちら|どっち|どれ)"
)
_EXPERIENCE = re.compile(
    r"ありましたか|経験|導入のとき|使ったとき|使っていて|どこで(?:止ま|引っかか|詰ま|迷)"
    r"|やってみて|入れてみて|困ったこと"
)
_OPINION = re.compile(r"見方|反対|異論|派の理由|意見|別の考え|違う考え|そう思わない")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def visible_text(body: str) -> str:
    """読み手が読む本文 (追跡 URL と ``{link}`` を除く)。"""

    return _URL.sub("", _norm(body).replace("{link}", "")).strip()


def prose_length(body: str) -> int:
    return len(visible_text(body))


def money_figures(text: str) -> list[str]:
    return sorted({re.sub(r"\s+", "", m) for m in _MONEY.findall(_norm(text))})


def numeric_facts(text: str) -> list[str]:
    out = set()
    for match in _NUMBER.finditer(visible_text(text)):
        token = re.sub(r"\s+", "", match.group(0))
        digits = re.sub(r"[^\d.]", "", token)
        if not digits:
            continue
        unit = token.replace(digits, "", 1).strip(",")
        if unit in _DATE_UNIT:
            continue  # 日付の条件は数えない
        if not unit and float(digits.replace(",", "") or 0) <= 10:
            continue  # 単位の無い小さな数 (「3つ」など) は事実として数えない
        out.add(token)
    return sorted(out)


def entities(text: str) -> list[str]:
    found = set()
    for token in _ENTITY.findall(visible_text(text)):
        if len(token) < 3 or token.lower() in _ENTITY_STOP:
            continue
        found.add(token.rstrip(".").lower())
    return sorted(found)


def axes(text: str) -> list[str]:
    visible = visible_text(text)
    return sorted(name for name, pattern in AXES.items() if pattern.search(visible))


def _final_question(text: str) -> str | None:
    visible = visible_text(text)
    sentences = [s for s in re.split(r"(?<=[。！!？?])", visible) if s.strip()]
    questions = [s for s in sentences if "？" in s or "?" in s]
    return questions[-1].strip() if questions else None


def hook_forms(text: str) -> set[str]:
    """本文の呼びかけの形 (``choice`` / ``question`` / ``experience`` / ``opinion`` / ``none``)。"""

    visible = visible_text(text)
    question = _final_question(visible)
    forms = set()
    if question and _CHOICE.search(question):
        forms.add("choice")
    elif question:
        forms.add("question")
    elif _CHOICE.search(visible):
        forms.add("choice")
    if _EXPERIENCE.search(visible):
        forms.add("experience")
    if _OPINION.search(visible):
        forms.add("opinion")
    return forms or {"none"}


def quality_findings(body: str, hook: str | None) -> tuple[list[str], list[str]]:
    """(書き直しの対象, 警告)。書き直しの対象は、書き直しの後は警告として残す (呼び出し側)。"""

    soft: list[str] = []
    warnings: list[str] = []
    length = prose_length(body)
    if length > QUALITY_CEILING:
        if _DATED.search(visible_text(body)):
            warnings.append(
                f"length exception: {length} chars (> {QUALITY_CEILING}) kept for a dated "
                "price/condition"
            )
        else:
            soft.append(
                f"the post is {length} chars; keep it within {QUALITY_CEILING} "
                f"(aim for {PREFERRED_RANGE[0]}-{PREFERRED_RANGE[1]}) with one main point"
            )
    elif length > PREFERRED_RANGE[1]:
        warnings.append(f"{length} chars is above the preferred {PREFERRED_RANGE[1]}")
    money = money_figures(body)
    if len(money) > MAX_MONEY_FIGURES:
        soft.append(
            f"{len(money)} prices ({', '.join(money)}) in one post; keep one main point and at "
            f"most {MAX_MONEY_FIGURES} prices"
        )
    found_axes = axes(body)
    if len(found_axes) > MAX_AXES:
        warnings.append(f"several comparison axes ({', '.join(found_axes)}); prefer one")
    facts = numeric_facts(body)
    if len(facts) > MAX_NUMERIC_FACTS:
        warnings.append(f"{len(facts)} numeric facts; the post may read like a summary")
    forms = hook_forms(body)
    if hook == "question" and "choice" in forms:
        soft.append(
            "the requested hook is question but the ending is an A/B choice; ask an open "
            "question that needs an explanation"
        )
    elif hook == "choice" and "choice" not in forms:
        soft.append(
            "the requested hook is choice but no alternatives are offered; ask the reader to "
            "choose or prioritise between two concrete options from the post"
        )
    elif hook == "experience" and "experience" not in forms:
        warnings.append("the experience hook does not clearly ask about the reader's own use")
    elif hook == "opinion" and "opinion" not in forms:
        warnings.append("the opinion hook does not clearly invite another view")
    return soft, warnings


def topic_signature(*, article_id: int | None, angle: str | None, link_mode: str | None,
                    body: str) -> dict:  # fmt: skip
    """決定的な話題の指紋 (監査用。同じ入力は同じ値)。"""

    data = {
        "version": QUALITY_VERSION,
        "article_id": article_id,
        "angle": angle,
        "link_mode": link_mode,
        "entities": entities(body),
        "axes": axes(body),
        "numbers": numeric_facts(body),
        "prose_length": prose_length(body),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            {k: data[k] for k in ("article_id", "entities", "axes", "numbers")},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")  # fmt: skip
    ).hexdigest()[:16]
    return {**data, "fingerprint": fingerprint}


def _shingles(text: str) -> set[str]:
    norm = re.sub(r"[\s\W_]+", "", visible_text(text).lower())
    return {norm[i : i + 3] for i in range(max(0, len(norm) - 2))}


def overlap(candidate: str, recent: str) -> dict:
    """1 件の最近の投稿 / 提案との重なり。同じ記事というだけでは高くならない。"""

    ent_a, ent_b = set(entities(candidate)), set(entities(recent))
    num_a, num_b = set(numeric_facts(candidate)), set(numeric_facts(recent))
    axes_a, axes_b = set(axes(candidate)), set(axes(recent))
    grams_a, grams_b = _shingles(candidate), _shingles(recent)
    shared_entities = sorted(ent_a & ent_b)
    shared_numbers = sorted(num_a & num_b)
    shared_axes = sorted(axes_a & axes_b)
    containment = round(len(grams_a & grams_b) / len(grams_a), 3) if grams_a else 0.0
    high = (
        containment >= 0.6
        or (
            len(shared_entities) >= 2
            and (len(shared_numbers) >= 2 or (bool(shared_axes) and containment >= 0.3))
        )
        or (len(shared_entities) >= 1 and len(shared_numbers) >= 2 and bool(shared_axes))
    )
    return {
        "high": high,
        "shared_entities": shared_entities,
        "shared_numbers": shared_numbers,
        "shared_axes": shared_axes,
        "containment": containment,
    }


def recent_overlap(candidate: str, recent: Iterable[Mapping]) -> dict | None:
    """最近の項目 (``{"ref": ..., "text": ...}``) のうち、重なりが高い最初のもの。"""

    for item in recent:
        result = overlap(candidate, item.get("text") or "")
        if result["high"]:
            return {**result, "ref": item.get("ref")}
    return None


def recent_topic_lines(recent: Iterable[Mapping]) -> list[str]:
    """prompt に入れる短い「最近の話題」(製品 / 軸 / 数値)。全文は入れない。"""

    lines: list[str] = []
    for item in recent:
        text = item.get("text") or ""
        ents = entities(text)[:3]
        found_axes = axes(text)[:2]
        nums = numeric_facts(text)[:4]
        if not (ents or nums):
            continue
        line = " / ".join(
            part for part in (", ".join(ents), "・".join(found_axes), ", ".join(nums)) if part
        )
        if line not in lines:
            lines.append(line)
        if len(lines) >= MAX_RECENT_TOPIC_LINES:
            break
    return lines


QUALITY_RULES = (
    "Threads は記事の圧縮ではない。記事は証拠であって、要約のチェックリストではない",
    "要点は 1 つだけ (間違い 1 つ・違い 1 つ・最初の一歩 1 つ・トレードオフ 1 つ・意外な事実 1 つ)",
    "無料の比較・有料の料金・機能の比較・最初の一歩を 1 本に詰めない。2 つ目の事実は、要点を"
    "説明するのに必要なときだけ",
    f"本文は普通 {PREFERRED_RANGE[0]}〜{PREFERRED_RANGE[1]} 字 (リンクを除く)。"
    f"{QUALITY_CEILING} 字を超えない。上限まで埋めようとしない。細かい事実は省く",
    "比較の軸は 1 つ。金額は多くても 2 つまで",
    "きっかけは考えの続きとして書く。最後に付け足した呼びかけにしない",
)


__all__ = [
    "PREFERRED_RANGE",
    "QUALITY_CEILING",
    "QUALITY_RULES",
    "QUALITY_VERSION",
    "RECENT_WINDOW",
    "axes",
    "entities",
    "hook_forms",
    "money_figures",
    "numeric_facts",
    "overlap",
    "prose_length",
    "quality_findings",
    "recent_overlap",
    "recent_topic_lines",
    "topic_signature",
    "visible_text",
]
