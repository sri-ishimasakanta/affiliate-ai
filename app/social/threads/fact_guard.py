"""自動生成の投稿案に追加でかける、決定的な事実の検査 (T6.2。pure)。

manual の答えにはこれまでどおりの検査 (T2 の形・リンク・文体・重複、T5.5 の来歴) だけを
かける。自動生成 (LLM) の出力は信用しない入力なので、同じ検査に **加えて** 次を確かめる。
どれか 1 つでも当たれば、その案は保存しない (書き直し 1 回 → だめなら manual の依頼へ戻す)。

- 数字の境界: 価格・割合・単位つきの数 (``$20``・``44%``・``1,000件``・``14日間``・
  ``2026年9月`` など) は、記事の本文に同じ数が無ければ誤り。記事に無い数値・日付・条件を
  作らせない。単位の無い 0〜10 の数 (「3つ」など) は数えない。
- 収益・成果の主張 (「稼げる」「収益が伸びた」など) は誤り。
- 作られた体験 (「実際に使ってみた」「私は」など) は誤り。
- 秘密らしい値 (token・鍵・認証情報) は誤り。
"""

from __future__ import annotations

import re
import unicodedata

from app.social.threads.errors import redact

_NUMBER = re.compile(
    r"(?P<pre>[$¥￥])?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>%|％|円|ドル|件|人|名|日間|日|時間|分|か月|ヶ月|カ月|年|月|GB|MB|TB|倍|本|社|個|回|言語)?"
)
REVENUE = re.compile(
    r"稼げ|儲か|収益が(?:伸び|増え|出)|売上が(?:伸び|増え)|報酬が(?:発生|入)|月\s*\d+\s*万"
)
EXPERIENCE = re.compile(
    r"実際に使って|使ってみた|試してみた|試したところ|私は|僕は|うちの会社|弊社で"
)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _numbers(text: str) -> list[tuple[str, str]]:
    """(正規化した数, そのままの表記) — 単位の無い 0〜10 は除く。"""

    out = []
    for match in _NUMBER.finditer(_normalize(text)):
        raw = match.group(0).strip()
        num = match.group("num").replace(",", "")
        if not (match.group("pre") or match.group("unit")):
            try:
                if float(num) <= 10:
                    continue
            except ValueError:
                continue
        out.append((num, raw))
    return out


def fact_boundary_errors(body: str, article_text: str) -> list[str]:
    body_n = _normalize(body)
    article_numbers = {num for num, _raw in _numbers(article_text)}
    article_plain = _normalize(article_text).replace(",", "")
    errors = []
    for num, raw in _numbers(body_n.replace("{link}", "")):
        if num not in article_numbers and num not in article_plain:
            errors.append(f"number not found in the article: {raw}")
    if REVENUE.search(body_n):
        errors.append("revenue or earnings claim is not allowed in generated posts")
    if EXPERIENCE.search(body_n):
        errors.append("first-person experience is not allowed in generated posts")
    if redact(body) != body:
        errors.append("the post contains a secret-like value")
    return sorted(set(errors))
