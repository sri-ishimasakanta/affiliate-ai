"""記事の種類の戦略 (C10-2、pure)。

8 つの役割 (``STRATEGY_ROLES``) を、既存の記事の型 (``app.article.planning.ArticleType``、
テンプレート) に写す。practical_workflow と implementation は既存の ``how_to`` のテンプレートを
使う役割 (新しい型は作らない)。

選び方は keyword の文字列だけで決めない。使う証拠: 語の印 (既存の ``classify_article_type`` と
下の印)、クラスタの中の役割と既にある記事の役割、commercial_intent、アフィリエイトの対象、
検索の意図 (``intent_profile``)。どれで決めたかを ``reasons`` に残す。1 つの点数は作らない。
決められなければ ``undetermined`` (人が見る)。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.article.planning import ArticleType, classify_article_type

COMPARISON = "comparison"
ROUNDUP = "roundup"
HOW_TO = "how_to"
PRACTICAL_WORKFLOW = "practical_workflow"
IMPLEMENTATION = "implementation"
PRICING = "pricing"
INFORMATIONAL = "informational"
CATEGORY_LANDING = "category_landing"
UNDETERMINED = "undetermined"
STRATEGY_ROLES = (COMPARISON, ROUNDUP, HOW_TO, PRACTICAL_WORKFLOW, IMPLEMENTATION, PRICING,
                  INFORMATIONAL, CATEGORY_LANDING)  # fmt: skip

TEMPLATE_FOR_ROLE = {
    COMPARISON: ArticleType.COMPARISON_LISTICLE,
    ROUNDUP: ArticleType.RECOMMENDATION_ROUNDUP,
    HOW_TO: ArticleType.HOW_TO,
    PRACTICAL_WORKFLOW: ArticleType.HOW_TO,
    IMPLEMENTATION: ArticleType.HOW_TO,
    PRICING: ArticleType.PRICING,
    INFORMATIONAL: ArticleType.INFORMATIONAL,
    CATEGORY_LANDING: ArticleType.CATEGORY_LANDING,
}
ROLE_FOR_TEMPLATE = {
    ArticleType.COMPARISON_LISTICLE: COMPARISON,
    ArticleType.RECOMMENDATION_ROUNDUP: ROUNDUP,
    ArticleType.HOW_TO: HOW_TO,
    ArticleType.PRICING: PRICING,
    ArticleType.INFORMATIONAL: INFORMATIONAL,
    ArticleType.CATEGORY_LANDING: CATEGORY_LANDING,
}
#: 商用の役割 (比べる・選ぶ・料金)。それ以外は情報の役割。
COMMERCIAL_ROLES = frozenset({COMPARISON, ROUNDUP, PRICING})

#: how_to のうち、役割を分ける印 (``classify_article_type`` より先に見る。上から順)。
_ROLE_MARKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("導入", "構築", "連携", "移行", "設定方法"), IMPLEMENTATION),
    (("活用", "ワークフロー", "自動化 方法", "効率化 方法", "テンプレート"), PRACTICAL_WORKFLOW),
    # 料金の意図の言い方 (既存の型の印「料金・価格・費用・プラン」に無いもの)。
    (("いくら", "課金", "pricing", "plans", "plan"), PRICING),
)
#: commercial_intent がこれ以上で、アフィリエイトの対象があれば「選ぶ」記事が合う。
COMMERCIAL_INTENT_ROUNDUP_MIN = 60.0


def family_of(role: str) -> str:
    return "commercial" if role in COMMERCIAL_ROLES else (
        "undetermined" if role == UNDETERMINED else "informational")


def role_from_marker(keyword: str) -> tuple[str | None, str | None]:
    """語の印から役割 (無ければ None)。返り値: (役割, 印)。"""

    text = " ".join((keyword or "").split())
    lowered = text.casefold()
    for markers, role in _ROLE_MARKERS:
        if role == PRICING:
            for marker in markers:
                if marker in lowered.split() or (not marker.isascii() and marker in text):
                    return role, marker
            continue
        for marker in markers:
            if marker in text:
                return role, marker
    result = classify_article_type(text)
    if result.article_type is None:
        return None, None
    return ROLE_FOR_TEMPLATE[result.article_type], result.matched_marker


def role_of_article(keyword: str | None, article_type: str | None) -> str | None:
    """既にある記事の役割 (保存された型を優先。how_to は語の印で細かく分ける)。"""

    try:
        template = ArticleType(article_type) if article_type else None
    except ValueError:
        template = None
    marker_role, _marker = role_from_marker(keyword or "")
    if template is None:
        return marker_role
    role = ROLE_FOR_TEMPLATE[template]
    if role == HOW_TO and marker_role in (IMPLEMENTATION, PRACTICAL_WORKFLOW):
        return marker_role
    return role


@dataclass(frozen=True)
class ContentTypeRecommendation:
    role: str
    template_type: str | None
    family: str
    basis: str  # marker / cluster_role / commercial_evidence / default / undetermined
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {**asdict(self), "reasons": list(self.reasons)}


def recommend_content_type(keyword: str, *, head_term: bool = False,
                           cluster_has_landing: bool = True,
                           commercial_intent: float | None = None,
                           affiliate_eligible: bool = False,
                           covered_roles: frozenset[str] = frozenset()
                           ) -> ContentTypeRecommendation:  # fmt: skip
    """1 つの語に合う記事の役割 (決定論的。理由つき)。"""

    reasons: list[str] = []
    role, marker = role_from_marker(keyword)
    basis = "marker"
    if role is not None:
        reasons.append(f"keyword marker '{marker}' → {role}")
    elif head_term and not cluster_has_landing:
        role, basis = CATEGORY_LANDING, "cluster_role"
        reasons.append("head term and the cluster has no category landing yet")
    elif commercial_intent is None:
        role, basis = UNDETERMINED, "undetermined"
        reasons.append("no marker and no commercial_intent evidence: needs human review")
    elif commercial_intent >= COMMERCIAL_INTENT_ROUNDUP_MIN and affiliate_eligible:
        role, basis = ROUNDUP, "commercial_evidence"
        reasons.append(f"commercial_intent {commercial_intent:g} ≥ "
                       f"{COMMERCIAL_INTENT_ROUNDUP_MIN:g} and an eligible affiliate program")
    else:
        role, basis = INFORMATIONAL, "default"
        reasons.append("no marker; commercial evidence does not support a selection article")
    if role in covered_roles:
        reasons.append(f"the cluster already has a {role} article (check cannibalization)")
    template = TEMPLATE_FOR_ROLE.get(role)
    return ContentTypeRecommendation(role, template.value if template else None, family_of(role),
                                     basis, tuple(reasons))


__all__ = ["CATEGORY_LANDING", "COMMERCIAL_INTENT_ROUNDUP_MIN", "COMMERCIAL_ROLES", "COMPARISON",
           "ContentTypeRecommendation", "HOW_TO", "IMPLEMENTATION", "INFORMATIONAL",
           "PRACTICAL_WORKFLOW", "PRICING", "ROLE_FOR_TEMPLATE", "ROUNDUP", "STRATEGY_ROLES",
           "TEMPLATE_FOR_ROLE", "UNDETERMINED", "family_of", "recommend_content_type",
           "role_from_marker", "role_of_article"]
