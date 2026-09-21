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
