"""keyword の「表記上の空白差」だけを吸収する比較専用 key (pure)。

Google Ads は日本語 phrase を分かち書きし直して返すことがある
(``タスク 管理 ツール`` と ``タスク管理 ツール``、``AI 議事録 おすすめ`` と ``AI議事録おすすめ``)。
これらは同じ keyword なので、完全一致 / 重複の判定だけは空白位置の差を無視する。

- 既存の正規化 (:func:`~app.keyword.normalizers.site_relevance.normalize_keyword` =
  NFKC + casefold + 連続空白の単一化) はそのまま。
- **日本語 (ひらがな / カタカナ / 漢字) を含む phrase だけ** 空白をすべて除去した key を返す。
  英語だけの phrase (``google meet`` と ``googlemeet`` は別) は通常の空白の意味を保つ。
- **比較専用**: keyword の表示・保存テキストは書き換えない。トークン単位の意図判定
  (``intent_profile``) や採点には使わない。完全一致 / 重複の fallback としてのみ使う。
- 例外として、列挙した acronym (``CANONICAL_ACRONYMS``) の分かち書き (``c rm`` / ``cr m``) だけは
  :func:`duplicate_key` で正規綴り (``crm``) と同一視する。``equivalence_key`` は変えない。

先行例: ``keyword_metrics_collection_service.compact_keyword_match_key`` (Historical Metrics 応答の
照合) と同じ「空白位置の差だけを吸収し、fuzzy match はしない」方針。あちらは全 phrase に空白除去を
適用するが、ここでは英語 phrase を巻き込まないよう日本語を含む場合に限定する。
"""

from __future__ import annotations

import re

from app.keyword.normalizers.site_relevance import normalize_keyword

# NFKC 後の日本語文字: ひらがな / カタカナ (半角カナは NFKC で全角化済み) / 漢字 / 々。
_JAPANESE = re.compile(r"[぀-ヿ㐀-䶿一-鿿々]")


def contains_japanese(text: str) -> bool:
    """正規化後の ``text`` にひらがな / カタカナ / 漢字が含まれるか。"""

    return _JAPANESE.search(normalize_keyword(text)) is not None


def equivalence_key(text: str) -> str:
    """完全一致 / 重複判定用の key。日本語を含む phrase は空白を除去する。"""

    normalized = normalize_keyword(text)
    if _JAPANESE.search(normalized) is None:
        return normalized
    return re.sub(r"\s+", "", normalized)


# 分かち書きされた綴りを同一とみなす acronym。Google Ads は ``crm`` を ``c rm`` / ``cr m`` に
# 割って返すことがある。英語一般の空白無視にはせず、ここに列挙した acronym だけを対象にする。
CANONICAL_ACRONYMS = frozenset({"crm"})

_LATIN_WORDS = re.compile(r"[a-z]+(?: [a-z]+)*")


def canonical_acronym(text: str) -> str | None:
    """phrase 全体の空白を除いた形が ``CANONICAL_ACRONYMS`` と完全一致するなら、その acronym。

    ``c rm`` / ``cr m`` / ``crm`` -> ``crm``。他の語を含む phrase (``c rm software``)、英語一般
    (``make sure``)、acronym ではない語 (``goo gle``) は ``None``。
    """

    normalized = normalize_keyword(text)
    if _LATIN_WORDS.fullmatch(normalized) is None:
        return None
    compact = normalized.replace(" ", "")
    return compact if compact in CANONICAL_ACRONYMS else None


def duplicate_key(text: str) -> str:
    """planner の重複判定 key: 列挙 acronym の分かち書き、または :func:`equivalence_key`。

    ``equivalence_key`` 自体は変えない (英語 phrase の空白の意味を保つ)。比較専用で、表示・保存
    テキストは書き換えない。
    """

    return canonical_acronym(text) or equivalence_key(text)
