"""Article Plan の純粋ロジック (DB / SQLAlchemy / FastAPI / 外部 API 非依存)。

Opportunity Score で選ばれた 1 keyword から、記事企画に必要な構造 (記事タイプ /
working title / slug 案 / 想定読者 / outline / 比較軸 / compliance / guardrail /
出典要件 / カニバリ guidance) を **決定論的** に生成する。

- LLM を呼ばない。本文・見出しの文章そのものは生成しない (構造と要件のみ)。
- primary affiliate を自動確定しない (候補の rank / grouping まで)。
- keyword 固有の長文を hard-code せず、一般化できる template / rule を使う。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from app.keyword.affiliate_matching import normalize_for_match


class ArticleType(StrEnum):
    RECOMMENDATION_ROUNDUP = "recommendation_roundup"
    COMPARISON_LISTICLE = "comparison_listicle"
    HOW_TO = "how_to"
    CATEGORY_LANDING = "category_landing"


# intent marker -> ArticleType。上から順に判定し、最初に一致したものを採用する
# (複数 marker が共存する場合の優先順位。テストで固定する)。
_ARTICLE_TYPE_RULES: tuple[tuple[ArticleType, tuple[str, ...]], ...] = (
    (ArticleType.HOW_TO, ("使い方", "導入", "やり方", "始め方", "設定方法", "手順")),
    (ArticleType.COMPARISON_LISTICLE, ("比較", "比べ", "違い", "vs")),
    (
        ArticleType.RECOMMENDATION_ROUNDUP,
        ("おすすめ", "オススメ", "ランキング", "人気", "選び方"),
    ),
    (ArticleType.CATEGORY_LANDING, ("とは", "意味", "一覧", "まとめ", "種類")),
)

# tail から取り除いて「テーマ本体」を得るための修飾語 (最低 1 token は残す)。
_TRAILING_MODIFIERS = frozenset(
    {
        "おすすめ", "オススメ", "ランキング", "人気", "選び方",
        "比較", "比べ方", "違い", "vs",
        "使い方", "導入", "やり方", "始め方", "手順", "方法",
        "とは", "意味", "一覧", "まとめ", "種類",
        "無料", "料金", "価格", "費用",
    }
)

# guidance 用の軽量な modifier -> intent 表現 (カニバリ差別化の説明にのみ使う)。
_MODIFIER_INTENT: tuple[tuple[tuple[str, ...], str], ...] = (
    (("無料",), "無料プラン・無料で完結する範囲の深掘り"),
    (("料金", "価格", "費用"), "料金・費用の比較"),
    (("比較", "違い", "vs"), "候補どうしの違いの比較"),
    (("おすすめ", "オススメ", "ランキング", "人気"), "目的別の総合的な推薦"),
    (("使い方", "導入", "やり方", "手順", "設定"), "導入・操作手順の解説"),
    (("とは", "意味", "種類"), "基礎知識・全体像の解説"),
)

_ARTICLE_TYPE_INTENT: dict[ArticleType, str] = {
    ArticleType.RECOMMENDATION_ROUNDUP: "目的別の総合的な推薦",
    ArticleType.COMPARISON_LISTICLE: "候補どうしの違いの比較",
    ArticleType.HOW_TO: "導入・操作手順の解説",
    ArticleType.CATEGORY_LANDING: "基礎知識・全体像の解説",
}

_SLUG_TYPE_TOKEN: dict[ArticleType, str] = {
    ArticleType.RECOMMENDATION_ROUNDUP: "roundup",
    ArticleType.COMPARISON_LISTICLE: "comparison",
    ArticleType.HOW_TO: "howto",
    ArticleType.CATEGORY_LANDING: "category",
}

_TITLE_TEMPLATE: dict[ArticleType, str] = {
    ArticleType.RECOMMENDATION_ROUNDUP: "{kw}｜選び方と目的別おすすめ比較",
    ArticleType.COMPARISON_LISTICLE: "{kw}｜違い・料金・選び方を比較",
    ArticleType.HOW_TO: "{kw}｜手順と注意点をわかりやすく解説",
    ArticleType.CATEGORY_LANDING: "{kw}｜意味・種類・選び方の基礎知識",
}

_TARGET_READER: dict[ArticleType, str] = {
    ArticleType.RECOMMENDATION_ROUNDUP: (
        "対象カテゴリを選定・比較検討している担当者。目的・料金・機能の違いを把握し、"
        "自分の状況に合う候補を数個に絞りたい読者。"
    ),
    ArticleType.COMPARISON_LISTICLE: (
        "複数の選択肢の違いを知りたい読者。料金・機能・向き不向きを横並びで確認し、"
        "自分に合うものを判断したい。"
    ),
    ArticleType.HOW_TO: (
        "対象を導入・利用しようとしている読者。手順・前提条件・つまずきやすい点を"
        "順を追って知りたい。"
    ),
    ArticleType.CATEGORY_LANDING: (
        "テーマの全体像をこれから把握する読者。定義・種類・選ぶ観点を一通り知りたい。"
    ),
}

_PRIMARY_GOAL: dict[ArticleType, str] = {
    ArticleType.RECOMMENDATION_ROUNDUP: (
        "読者が自分の目的に合う候補を 1〜3 個に絞り込める状態にする。"
    ),
    ArticleType.COMPARISON_LISTICLE: (
        "読者が各選択肢の違いを理解し、自分に合うものを 1 つ選べる状態にする。"
    ),
    ArticleType.HOW_TO: "読者が手順どおりに実行して目的を達成できる状態にする。",
    ArticleType.CATEGORY_LANDING: (
        "読者がテーマの全体像と、次に読むべき詳細トピックを把握できる状態にする。"
    ),
}

# catalog で埋まらない比較軸は future_research_required (推測値で埋めない)。
COMPARISON_AXES: tuple[tuple[str, str], ...] = (
    ("用途・解決できる課題", "future_research_required"),
    ("料金（月額 / 年額）", "future_research_required"),
    ("無料プランの有無", "future_research_required"),
    ("対象ユーザー規模（個人 / 中小 / 法人）", "future_research_required"),
    ("自動化範囲", "future_research_required"),
    ("AI 機能の有無", "future_research_required"),
    ("外部サービス連携", "future_research_required"),
    ("導入難易度", "future_research_required"),
    ("日本語対応・日本法人", "future_research_required"),
    ("法人契約・請求書払い", "future_research_required"),
    ("カテゴリ（カタログ分類）", "catalog"),
    ("提携 ASP / 収益条件（内部判断用・記事非掲載）", "catalog"),
)

COMPLIANCE_CHECKLIST: tuple[str, ...] = (
    "記事冒頭に PR / 広告（アフィリエイト）を含む旨を表示する（ステマ規制・景表法を想定）",
    "アフィリエイトリンクが紹介リンクである旨を明示する",
    "すべての料金情報に「◯年◯月時点」と出典（公式）を併記する",
    "各ツールの事実（料金・機能・連携）は公式ページを Source として紐付け、checked_at を記録する",
    "「No.1」「必ず」「劇的」などの誇大・断定表現を使わない／効果を保証しない",
    "アフィリエイト対象外のツールも必要に応じて比較に含め、公平性を保つ",
)

QUALITY_GUARDRAILS: tuple[str, ...] = (
    "1 keyword = 1 明確な intent（article_type が一意に決まらない場合は human review）",
    "公開前に cannibalization（既存 Keyword / Article との重複）をレビューする",
    "事実主張・各項目に最低 1 件の primary source（公式）を要求する",
    "公開時点で料金系 Source の checked_at が新しいこと（pricing freshness）",
    "review→approved の status 遷移は human のみ（自動遷移を作らない）",
    "affiliate link の本文挿入は approved 後に限る（planned 段階は relation 登録のみ）",
    "plan 一括生成・記事一括生成をしない（keyword ごと個別・1 承認 1 記事）",
)

_SOURCE_REQUIREMENTS: dict[ArticleType, tuple[str, ...]] = {
    ArticleType.RECOMMENDATION_ROUNDUP: (
        "各候補ツールの公式料金ページ",
        "各候補ツールの公式機能一覧",
        "各候補ツールの公式無料プラン / トライアル情報",
        "各候補ツールの公式連携（インテグレーション）情報",
        "（あれば）各ツールの公式日本語対応・日本法人情報",
    ),
    ArticleType.COMPARISON_LISTICLE: (
        "比較対象それぞれの公式料金ページ",
        "比較対象それぞれの公式機能一覧",
        "比較対象それぞれの公式無料プラン情報",
    ),
    ArticleType.HOW_TO: (
        "対象サービスの公式ドキュメント / ヘルプ",
        "公式の対応環境・前提条件",
    ),
    ArticleType.CATEGORY_LANDING: (
        "対象テーマの一次情報（公式・業界団体等）",
        "主要製品・サービスの公式概要",
    ),
}

CANNIBALIZATION_THRESHOLD = 40.0


@dataclass(frozen=True)
class PlanSection:
    level: str  # "H1" / "intro" / "H2" / "H3"
    heading: str
    purpose: str
    required_elements: tuple[str, ...]


@dataclass(frozen=True)
class ArticleTypeResult:
    article_type: ArticleType | None
    matched_marker: str | None


def _is_ascii_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def display_text(text: str) -> str:
    """表示用に token 間の空白を自然化する (casefold しない)。

    日本語 (や記号) が絡む token 境界の空白は詰め、**ASCII/Latin 英数字どうし**の
    境界の空白だけを 1 つ残す。``"業務効率化 ツール おすすめ"`` -> ``"業務効率化ツールおすすめ"``、
    ``"ChatGPT Plus 料金"`` -> ``"ChatGPT Plus料金"`` (``ChatGPT Plus`` は分離維持)。
    新しい依存は追加しない (NFKC + str.split のみ)。
    """

    tokens = unicodedata.normalize("NFKC", text).split()
    if not tokens:
        return ""
    out = tokens[0]
    for tok in tokens[1:]:
        if out and tok and _is_ascii_alnum(out[-1]) and _is_ascii_alnum(tok[0]):
            out += " " + tok
        else:
            out += tok
    return out


def classify_article_type(keyword: str) -> ArticleTypeResult:
    """keyword 文字列の明示 intent marker から記事タイプを決定論的に判定する。

    marker が 1 つも無ければ ``article_type=None`` を返す (unknown で誤魔化さず、
    呼び出し側で human review を要求させる)。
    """

    normalized = normalize_for_match(keyword)
    for article_type, markers in _ARTICLE_TYPE_RULES:
        for marker in markers:
            if normalize_for_match(marker) in normalized:
                return ArticleTypeResult(article_type=article_type, matched_marker=marker)
    return ArticleTypeResult(article_type=None, matched_marker=None)


def theme_of(keyword: str) -> str:
    """tail の修飾語を取り除いた表示用「テーマ本体」(最低 1 token は残す)。

    修飾語判定は casefold して行うが、返す文字列は :func:`display_text` で
    日本語 token 間の不要な空白を詰めたもの。
    """

    tokens = unicodedata.normalize("NFKC", keyword).split()
    while len(tokens) > 1 and tokens[-1].casefold() in _TRAILING_MODIFIERS:
        tokens.pop()
    return display_text(" ".join(tokens))


def working_title(keyword: str, article_type: ArticleType | None) -> str:
    display = display_text(keyword)
    if article_type is None:
        return f"{display}｜（記事タイプ未確定・要 human review）"
    return _TITLE_TEMPLATE[article_type].format(kw=display)


def _slug_base(keyword: str, article_type: ArticleType | None) -> str:
    normalized = unicodedata.normalize("NFKC", keyword).casefold()
    # 空白類 -> ハイフン
    normalized = re.sub(r"\s+", "-", normalized.strip())
    # slug に使う文字だけ残す: ラテン英数字 / ひらがな / カタカナ / 漢字 / 長音 / ハイフン
    normalized = re.sub(
        r"[^0-9a-zぁ-ゖァ-ヺー一-鿿\-]",
        "",
        normalized,
    )
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    token = _SLUG_TYPE_TOKEN.get(article_type, "plan") if article_type else "plan"
    return f"{normalized}-{token}" if normalized else token


def suggest_slug(
    keyword: str,
    article_type: ArticleType | None,
    *,
    is_taken: object = None,
) -> str:
    """決定論的な slug 案。``is_taken(slug) -> bool`` が与えられれば衝突を避けて連番付与。

    新しい romanization 依存は追加しない (Unicode-safe / NFKC + casefold ベース)。
    最終的な slug は approve request で human が override 可能。
    """

    base = _slug_base(keyword, article_type)
    if is_taken is None:
        return base
    if not is_taken(base):  # type: ignore[operator]
        return base
    for suffix in range(2, 50):
        candidate = f"{base}-{suffix}"
        if not is_taken(candidate):  # type: ignore[operator]
            return candidate
    return base  # 事実上到達しない


def target_reader(article_type: ArticleType | None) -> str:
    if article_type is None:
        return "記事タイプ未確定のため要 human review。"
    return _TARGET_READER[article_type]


_SEARCH_INTENT: dict[ArticleType, str] = {
    ArticleType.RECOMMENDATION_ROUNDUP: (
        "推薦・比較（顕在）。複数候補から自分に合うものを選びたい。"
    ),
    ArticleType.COMPARISON_LISTICLE: (
        "比較（顕在）。選択肢の違いを知って 1 つ選びたい。"
    ),
    ArticleType.HOW_TO: "手順・操作（顕在）。実行方法を知りたい。",
    ArticleType.CATEGORY_LANDING: (
        "情報収集（準顕在）。テーマの全体像を把握したい。"
    ),
}


def search_intent_summary(keyword: str, article_type: ArticleType | None) -> str:
    if article_type is None:
        return "keyword の明示 intent marker が無く、検索意図を機械的に確定できない。"
    return _SEARCH_INTENT[article_type]


def goals(article_type: ArticleType | None) -> tuple[str, tuple[str, ...]]:
    if article_type is None:
        return ("記事タイプ未確定のため要 human review。", ())
    primary = _PRIMARY_GOAL[article_type]
    secondary = (
        "アフィリエイト対象候補への無料登録 / トライアル導線を自然に配置する（承認後にリンク挿入）",
        "関連 keyword で扱う別記事への内部リンクでカニバリを回避しつつ回遊を作る",
    )
    return primary, secondary


def _roundup_outline(theme: str) -> tuple[PlanSection, ...]:
    return (
        PlanSection(
            "H1",
            f"{theme}おすすめ｜選び方と目的別比較",
            "記事全体のテーマと提供価値を提示する",
            ("対象 keyword を含む", "主要ベネフィット", "◯選の件数は human 確定"),
        ),
        PlanSection(
            "intro",
            "導入",
            "読者の課題・記事の狙いを提示し、PR 表記を行う",
            (
                "想定読者の課題",
                "この記事で分かること",
                "PR / 広告表記",
                "料金は「◯年◯月時点」注記",
            ),
        ),
        PlanSection(
            "H2",
            f"{theme}とは / 何を解決できるか",
            "前提知識と対象範囲を揃える",
            ("定義", "主なカテゴリ / タイプ"),
        ),
        PlanSection(
            "H2",
            f"{theme}の選び方",
            "結論の前に判断基準（比較軸）を与える",
            ("比較軸の一覧", "目的別に重視すべきポイント"),
        ),
        PlanSection(
            "H2",
            f"おすすめ{theme}比較",
            "主要候補を横並びで比較する",
            ("比較表（選び方の軸に対応）", "各候補の概要"),
        ),
        PlanSection(
            "H3",
            "各候補（候補数だけ繰り返し）",
            "候補ごとの詳細を一定フォーマットで示す",
            (
                "概要",
                "料金・無料プラン",
                "主な機能・自動化範囲",
                "外部連携",
                "こんな人におすすめ",
                "CTA（リンク挿入は承認後）",
            ),
        ),
        PlanSection(
            "H2",
            "目的別おすすめ",
            "ユースケース別に推薦を整理する",
            ("ユースケース区分", "各区分の推し候補"),
        ),
        PlanSection(
            "H2",
            "導入時の注意点",
            "失敗を避けるための実務的注意",
            ("スモールスタート", "料金改定の可能性", "社内定着", "セキュリティ・契約形態"),
        ),
        PlanSection(
            "H2",
            "よくある質問",
            "検索意図の残余を回収する",
            ("3 件以上の Q&A",),
        ),
        PlanSection(
            "H2",
            "まとめ",
            "結論を再提示する",
            ("目的別の最終推薦（1〜3 個）", "主要 CTA"),
        ),
    )


def _comparison_outline(theme: str) -> tuple[PlanSection, ...]:
    return (
        PlanSection(
            "H1",
            f"{theme}を比較｜違い・料金・選び方",
            "比較記事のテーマを提示",
            ("対象 keyword",),
        ),
        PlanSection(
            "intro", "導入", "比較の観点と PR 表記",
            ("何を比較するか", "PR / 広告表記", "料金は「◯年◯月時点」注記"),
        ),
        PlanSection("H2", "比較の前提", "対象と評価軸を定義", ("対象の一覧", "比較軸")),
        PlanSection(
            "H2", "比較表", "横並びで違いを一覧化",
            ("比較表（全軸）", "軸ごとの補足"),
        ),
        PlanSection(
            "H3", "各対象の詳細（対象数だけ繰り返し）", "対象ごとの掘り下げ",
            ("概要", "料金・無料プラン", "向いている人 / 向かない人", "CTA（承認後）"),
        ),
        PlanSection("H2", "目的別の選び方", "読者状況ごとの結論", ("状況区分", "推奨")),
        PlanSection("H2", "よくある質問", "残余意図の回収", ("3 件以上の Q&A",)),
        PlanSection("H2", "まとめ", "結論の再提示", ("状況別の最終結論", "主要 CTA")),
    )


def _howto_outline(theme: str) -> tuple[PlanSection, ...]:
    return (
        PlanSection("H1", f"{theme}の手順と注意点", "手順記事のテーマを提示", ("対象 keyword",)),
        PlanSection(
            "intro", "導入", "前提・ゴール・PR 表記",
            ("前提条件", "この手順で達成できること", "PR / 広告表記（該当時）"),
        ),
        PlanSection(
            "H2",
            "事前準備",
            "着手前に必要なもの",
            ("必要なアカウント / 環境", "所要時間の目安"),
        ),
        PlanSection(
            "H2", "手順（ステップ形式）", "順を追って実行できるようにする",
            ("番号付きステップ", "各ステップのスクリーンショット要否", "つまずきやすい点"),
        ),
        PlanSection(
            "H2",
            "うまくいかないとき",
            "トラブルシューティング",
            ("代表的なエラーと対処",),
        ),
        PlanSection("H2", "よくある質問", "残余意図の回収", ("3 件以上の Q&A",)),
        PlanSection("H2", "まとめ", "要点の再確認", ("次にやること",)),
    )


def _category_outline(theme: str) -> tuple[PlanSection, ...]:
    return (
        PlanSection(
            "H1",
            f"{theme}とは｜意味・種類・選び方",
            "テーマの入口を提示",
            ("対象 keyword",),
        ),
        PlanSection("intro", "導入", "この記事の範囲", ("誰向けか", "この記事で分かること")),
        PlanSection("H2", f"{theme}とは", "定義と背景", ("定義", "なぜ重要か")),
        PlanSection("H2", "種類・分類", "全体像の地図を与える", ("主要な種類", "各種類の特徴")),
        PlanSection("H2", "選ぶ観点", "判断軸の提示", ("比較軸", "目的別の重視点")),
        PlanSection(
            "H2", "代表的な選択肢", "具体例への橋渡し",
            ("主要な選択肢の概要", "詳細記事への内部リンク"),
        ),
        PlanSection("H2", "よくある質問", "残余意図の回収", ("3 件以上の Q&A",)),
        PlanSection("H2", "まとめ", "次に読むべきトピック", ("関連記事への導線",)),
    )


def build_outline(keyword: str, article_type: ArticleType | None) -> tuple[PlanSection, ...]:
    if article_type is None:
        return (
            PlanSection(
                "H1",
                keyword.strip(),
                "記事タイプ未確定。human が intent を確定してから outline を作る",
                ("article_type の確定",),
            ),
        )
    theme = theme_of(keyword)
    builders = {
        ArticleType.RECOMMENDATION_ROUNDUP: _roundup_outline,
        ArticleType.COMPARISON_LISTICLE: _comparison_outline,
        ArticleType.HOW_TO: _howto_outline,
        ArticleType.CATEGORY_LANDING: _category_outline,
    }
    return builders[article_type](theme)


def comparison_axes() -> tuple[tuple[str, str], ...]:
    return COMPARISON_AXES


def cta_strategy(article_type: ArticleType | None) -> str:
    if article_type is None:
        return "記事タイプ未確定のため要 human review。"
    if article_type in (
        ArticleType.RECOMMENDATION_ROUNDUP,
        ArticleType.COMPARISON_LISTICLE,
    ):
        return (
            "各候補の節末に個別 CTA、比較表内に行動導線、目的別まとめに総合 CTA を置く。"
            "affiliate link の実挿入は Article approved 後に行う（planned 段階は未挿入）。"
        )
    if article_type is ArticleType.HOW_TO:
        return (
            "手順完了地点と記事末に関連ツールの CTA を控えめに配置する。"
            "affiliate link の実挿入は approved 後。"
        )
    return (
        "各詳細トピックへの内部リンクを主要 CTA とし、商用リンクは最小限。"
        "affiliate link の実挿入は approved 後。"
    )


def source_requirements(article_type: ArticleType | None) -> tuple[str, ...]:
    if article_type is None:
        return ("記事タイプ未確定のため要 human review。",)
    return _SOURCE_REQUIREMENTS[article_type]


def _modifier_intent(keyword: str) -> str | None:
    normalized = normalize_for_match(keyword)
    for markers, phrase in _MODIFIER_INTENT:
        if any(normalize_for_match(m) in normalized for m in markers):
            return phrase
    return None


def cannibalization_guidance(
    keyword: str,
    article_type: ArticleType | None,
    originality: float | None,
    most_similar_keyword_text: str | None,
) -> str:
    """一般化した差別化 guidance。keyword 固有文の hard-code は避ける。"""

    if originality is None:
        return (
            "originality Signal が無いためカニバリ評価は未実施。"
            "企画前に derive_originality を実行すること。"
        )

    this_intent = (
        _ARTICLE_TYPE_INTENT.get(article_type) if article_type else None
    ) or _modifier_intent(keyword) or "本記事の intent"

    if originality >= CANNIBALIZATION_THRESHOLD or not most_similar_keyword_text:
        return (
            f"originality={originality:.2f}。"
            "既存 Keyword / Article との重大な重複は検出されていないが、"
            "公開前に編集者が最終確認する。"
        )

    other_intent = _modifier_intent(most_similar_keyword_text) or "別の切り口"
    return (
        f"originality={originality:.2f}（低）。最も近い既存 keyword は"
        f"『{most_similar_keyword_text}』。本記事は「{this_intent}」に内容を寄せ、"
        f"『{most_similar_keyword_text}』は「{other_intent}」を担当する形で棲み分け、"
        "重複領域は要点のみに絞って相互に内部リンクする。"
        "文字類似度は語幹の共有を検出するが intent 重複の可否は判定できないため、"
        "最終判断は編集者が行う。"
    )
