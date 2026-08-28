"""site_relevance component の正規化ロジック (V1)。

**完全ローカル・決定論的・rule-based。** DB / FastAPI / 外部 API / LLM API /
Google Ads いずれにも依存しない。

評価するのは「この keyword は、このサイトで扱うテーマ (AI・生成AI・業務効率化・
業務自動化) として適切か」だけ。検索需要 (search_demand) / 購買意図
(commercial_intent) / 需要の増減 (trend) / SEO 競合 (competition_ease) /
案件有無 (affiliate_opportunity) は評価しない。

料金・価格・比較・おすすめ・ランキング・無料・使い方・とは・導入 といった
commercial intent 語は site_relevance の加点要素にしない
(``"ChatGPT 料金"`` と ``"ChatGPT 使い方"`` は原則同じ site_relevance)。

V1 formula
----------
matched topic group がある場合:

    base_score            = matched groups の base score の最大値
    multi_group_bonus     = 10 (distinct matched group >= 2) / それ以外 0
    business_context_bonus = 10 (business context 語あり) / それ以外 0
    site_relevance         = clamp(base_score + multi_group_bonus + business_context_bonus, 0, 100)

matched topic group なし:

    out-of-scope 語あり  -> 0
    それ以外             -> 20 (unknown / general)

``round(..., 2)``。

将来的に semantic relevance / Search Console データ / 既存記事 embedding /
複数サイト profile / DB 管理 profile へ拡張しうるが V1 では実装しない (YAGNI)。
計算ロジックと profile vocabulary は疎結合に保つ。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

NORMALIZER_NAME = "site_relevance"
NORMALIZER_VERSION = "v1"

# 現在のサイトテーマ profile。将来 site theme 変更・複数サイト・DB 管理へ広げられる
# よう、vocabulary は下の定数に集約し、計算ロジックからは group を走査するだけにする。
SITE_PROFILE_NAME = "ai_business_automation"
SITE_PROFILE_VERSION = "v1"

_NO_MATCH_SCORE = 20.0
_OUT_OF_SCOPE_SCORE = 0.0
_MULTI_GROUP_BONUS = 10.0
_BUSINESS_CONTEXT_BONUS = 10.0
_SCORE_MIN = 0.0
_SCORE_MAX = 100.0


@dataclass(frozen=True)
class TopicGroup:
    """関連語の意味グループと base score。語は正規化後 (NFKC + casefold) の表記。"""

    name: str
    base_score: float
    terms: tuple[str, ...]


CORE_THEME = TopicGroup(
    name="CORE_THEME",
    base_score=80.0,
    terms=(
        "ai",
        "生成ai",
        "人工知能",
        "chatgpt",
        "claude",
        "gemini",
        "copilot",
        "llm",
        "rpa",
        "自動化",
        "業務自動化",
        "自動作成",
    ),
)

RELEVANT_TOOL = TopicGroup(
    name="RELEVANT_TOOL",
    base_score=75.0,
    terms=(
        "notion",
        "zapier",
        "make",
        "n8n",
        "power automate",
        "uipath",
    ),
)

BUSINESS_PRODUCTIVITY = TopicGroup(
    name="BUSINESS_PRODUCTIVITY",
    base_score=70.0,
    terms=(
        "業務効率化",
        "生産性",
        "生産性向上",
        "バックオフィス",
        "営業効率化",
        "経理効率化",
        "人事効率化",
        "dx",
    ),
)

ADJACENT_USE_CASE = TopicGroup(
    name="ADJACENT_USE_CASE",
    base_score=60.0,
    terms=(
        "議事録",
        "文字起こし",
        "要約",
        "ocr",
        "ワークフロー",
        "ノーコード",
        "チャットボット",
        "文書作成",
        "メール自動化",
        "データ入力",
    ),
)

# 走査順 = base score 優先度の高い順 (max を取るので順序自体は結果に影響しないが、
# matched_groups / matched_terms の並びを決定論にするため固定する)。
TOPIC_GROUPS: tuple[TopicGroup, ...] = (
    CORE_THEME,
    RELEVANT_TOOL,
    BUSINESS_PRODUCTIVITY,
    ADJACENT_USE_CASE,
)

# 業務利用を示す語。topic group とは独立の +10 ボーナス。
BUSINESS_CONTEXT_TERMS: tuple[str, ...] = (
    "法人",
    "企業",
    "業務",
    "社内",
    "会社",
    "チーム",
    "会議",
    "営業",
    "経理",
    "人事",
    "バックオフィス",
    "カスタマーサポート",
    "業界",
)

# 明確にサイトテーマ外の語。topic group が 1 つも無い場合のみ 0 点判定に使う。
OUT_OF_SCOPE_TERMS: tuple[str, ...] = (
    "レシピ",
    "料理",
    "観光",
    "旅行",
    "ゲーム",
    "占い",
    "芸能",
    "スポーツ",
    "ダイエット",
    "恋愛",
)


@dataclass(frozen=True)
class SiteRelevanceResult:
    """site_relevance V1 の計算結果と根拠 (Signal.raw_data 保存用)。"""

    normalized_value: float
    base_score: float
    matched_groups: tuple[str, ...]
    matched_terms: tuple[str, ...]
    business_context_terms: tuple[str, ...]
    out_of_scope_terms: tuple[str, ...]
    multi_group_bonus: float
    business_context_bonus: float
    profile_name: str
    profile_version: str
    normalizer_name: str
    normalizer_version: str


def normalize_keyword(keyword: str) -> str:
    """keyword を照合用に正規化する (pure)。

    Unicode NFKC → casefold → 前後空白除去 → 連続空白の単一化。
    NFKC で全角英数字・全角スペース・半角カナ等の差を可能な範囲で吸収する。
    """

    text = unicodedata.normalize("NFKC", keyword)
    text = text.casefold()
    return " ".join(text.split())


def _term_matches(term: str, text: str) -> bool:
    """term が text 内に出現するか。

    ASCII 英数字語 (``ai`` / ``make`` / ``rpa`` / ``power automate`` 等) は
    英数字境界で照合し、``maker`` の中の ``make`` や別英単語中の ``ai`` を
    誤検知しない。日本語を含む語は実質的な substring 一致 (前後が英数字でない
    位置での一致) とする。
    """

    pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _matched_terms(terms: tuple[str, ...], text: str) -> list[str]:
    return [term for term in terms if _term_matches(term, text)]


def _clamp(value: float) -> float:
    return min(_SCORE_MAX, max(_SCORE_MIN, value))


def calculate_site_relevance(keyword: str) -> SiteRelevanceResult:
    """keyword とサイト profile ``ai_business_automation`` v1 から site_relevance を算出する。"""

    text = normalize_keyword(keyword)

    matched_by_group: dict[str, list[str]] = {}
    matched_terms: list[str] = []
    for group in TOPIC_GROUPS:
        hits = _matched_terms(group.terms, text)
        if hits:
            matched_by_group[group.name] = hits
            matched_terms.extend(hits)

    business_terms = _matched_terms(BUSINESS_CONTEXT_TERMS, text)
    out_of_scope_terms = _matched_terms(OUT_OF_SCOPE_TERMS, text)

    if matched_by_group:
        base_score = max(
            group.base_score
            for group in TOPIC_GROUPS
            if group.name in matched_by_group
        )
        multi_group_bonus = (
            _MULTI_GROUP_BONUS if len(matched_by_group) >= 2 else 0.0
        )
        business_context_bonus = _BUSINESS_CONTEXT_BONUS if business_terms else 0.0
        score = _clamp(base_score + multi_group_bonus + business_context_bonus)
    else:
        base_score = _OUT_OF_SCOPE_SCORE if out_of_scope_terms else _NO_MATCH_SCORE
        multi_group_bonus = 0.0
        business_context_bonus = 0.0
        score = base_score

    return SiteRelevanceResult(
        normalized_value=round(score, 2),
        base_score=round(base_score, 2),
        matched_groups=tuple(matched_by_group),
        matched_terms=tuple(matched_terms),
        business_context_terms=tuple(business_terms),
        out_of_scope_terms=tuple(out_of_scope_terms),
        multi_group_bonus=multi_group_bonus,
        business_context_bonus=business_context_bonus,
        profile_name=SITE_PROFILE_NAME,
        profile_version=SITE_PROFILE_VERSION,
        normalizer_name=NORMALIZER_NAME,
        normalizer_version=NORMALIZER_VERSION,
    )
