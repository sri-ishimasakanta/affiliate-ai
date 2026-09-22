"""Content cluster plan と cannibalization guard (pure・決定論的)。

DB / SQLAlchemy / FastAPI / HTTP / LLM に一切依存しない。入力 (cluster 定義・keyword・既存
article・catalog match) から **read-only な制作キュー** を導出するだけで、何も書かない。

* cluster 定義は追跡ファイル (``app/config/content_clusters.json``) を :func:`parse_cluster_config`
  で検証する: 既知 keyword のみ / 1 keyword = 1 cluster / 1 cluster = 1 pillar / role は
  ``pillar`` / ``supporting`` のみ。
* cannibalization は文字類似だけでなく **意図 (modifier を除いた theme + SERP family)** で判定する。
  ``おすすめ`` / ``比較`` / (一般カテゴリの) ``料金`` は同じ SERP を狙う (= ``selection`` family)
  ので merge 候補になる。``無料`` / ``使い方・導入`` / ``とは`` は別 family。特定製品
  (``product_terms``) の料金・無料は製品ごとの ``product_plan`` family。
* 判定:
    - ``blocked`` : keyword が既に non-archived な Article を持つ / 別 keyword の non-archived
      Article (idea / planned / drafting / review / approved / published / rewrite) と意図が
      重なる / keyword が rejected。
    - ``merge``   : 他の (queue 内) keyword と意図がほぼ同一。primary に吸収し別記事にしない。
    - ``ok``      : 独立した制作 slot。
* 既存の scoring weight / signal は変更しない。score は入力として表示・順序付けにのみ使う。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.article.planning import ArticleType, classify_article_type
from app.keyword.affiliate_matching import term_matches
from app.keyword.affiliate_tiers import TieredMatch
from app.keyword.normalizers.site_relevance import normalize_keyword
from app.keyword.text_similarity import text_similarity

CONFIG_VERSION = 1

ROLE_PILLAR = "pillar"
ROLE_SUPPORTING = "supporting"
CLUSTER_ROLES = frozenset({ROLE_PILLAR, ROLE_SUPPORTING})

DECISION_OK = "ok"
DECISION_MERGE = "merge"
DECISION_BLOCKED = "blocked"

#: theme の文字類似がこの値以上で family も同じなら意図重複とみなす (表記ゆれ用の安全網)。
CHAR_OVERLAP_THRESHOLD = 0.9
#: 参考情報として near-overlap を列挙する下限 (decision には影響しない)。
NEAR_OVERLAP_THRESHOLD = 0.6

#: 1 つの prompt template が対応する article type。実 registry
#: (``app.article.draft_prompt_render._TEMPLATES``) との drift は test で検出する。
TEMPLATE_SCOPE: Mapping[str, tuple[ArticleType, ...]] = {
    "article_roundup_v1": (ArticleType.RECOMMENDATION_ROUNDUP,),
    # C3: portfolio が必要とする残りの記事タイプ (draft_prompt_templates.TEMPLATES と 1:1)
    "article_category_v1": (ArticleType.CATEGORY_LANDING,),
    "article_comparison_v1": (ArticleType.COMPARISON_LISTICLE,),
    "article_howto_v1": (ArticleType.HOW_TO,),
    "article_informational_v1": (ArticleType.INFORMATIONAL,),
    "article_pricing_v1": (ArticleType.PRICING,),
}

_TOP_KEYS = frozenset({"version", "clusters", "deferred_clusters", "vocabulary"})
_CLUSTER_KEYS = frozenset({"id", "name", "priority", "keywords"})
_KEYWORD_KEYS = frozenset({"keyword", "role"})
_DEFERRED_KEYS = frozenset({"id", "name", "reason"})
_VOCAB_KEYS = frozenset(
    {"product_terms", "generic_theme_tokens", "theme_aliases", "optional_qualifiers"}
)


class ClusterConfigError(ValueError):
    """cluster 定義 / keyword 照合の検証エラー (複数件をまとめて保持する)。"""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


# ==================================================================== config
@dataclass(frozen=True)
class ClusterKeyword:
    keyword: str
    role: str


@dataclass(frozen=True)
class Cluster:
    id: str
    name: str
    priority: int
    keywords: tuple[ClusterKeyword, ...]


@dataclass(frozen=True)
class DeferredCluster:
    id: str
    name: str
    reason: str


@dataclass(frozen=True)
class Vocabulary:
    product_terms: tuple[str, ...] = ()
    generic_theme_tokens: tuple[str, ...] = ()
    theme_aliases: tuple[tuple[str, str], ...] = ()
    #: theme に他の token が残る場合だけ無視できる語 (中小企業 / テレワーク 等)。
    optional_qualifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClusterConfig:
    version: int
    clusters: tuple[Cluster, ...]
    deferred: tuple[DeferredCluster, ...] = ()
    vocabulary: Vocabulary = field(default_factory=Vocabulary)


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_vocabulary(raw: object, errors: list[str]) -> Vocabulary:
    if raw is None:
        return Vocabulary()
    if not isinstance(raw, Mapping):
        errors.append("vocabulary must be an object")
        return Vocabulary()
    unknown = sorted(set(raw) - _VOCAB_KEYS)
    if unknown:
        errors.append(f"vocabulary has unknown keys: {unknown}")

    def _texts(key: str) -> tuple[str, ...]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(_is_text(v) for v in value):
            errors.append(f"vocabulary.{key} must be a list of non-empty strings")
            return ()
        return tuple(str(v) for v in value)

    aliases_raw = raw.get("theme_aliases", {})
    aliases: tuple[tuple[str, str], ...] = ()
    if not isinstance(aliases_raw, Mapping) or not all(
        _is_text(k) and _is_text(v) for k, v in aliases_raw.items()
    ):
        errors.append("vocabulary.theme_aliases must map non-empty strings to non-empty strings")
    else:
        aliases = tuple(sorted((str(k), str(v)) for k, v in aliases_raw.items()))
    return Vocabulary(
        product_terms=_texts("product_terms"),
        generic_theme_tokens=_texts("generic_theme_tokens"),
        theme_aliases=aliases,
        optional_qualifiers=_texts("optional_qualifiers"),
    )


def parse_cluster_config(raw: object) -> ClusterConfig:
    """cluster 定義 (JSON の dict) を構造検証して :class:`ClusterConfig` にする。

    重複 keyword / 複数 pillar / 不正 role / 不明キーなどを **まとめて** ``ClusterConfigError``
    で報告する。DB との照合 (既知 keyword か) は :func:`build_content_queue` が行う。
    """

    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise ClusterConfigError(["cluster config must be a JSON object"])

    unknown_top = sorted(set(raw) - _TOP_KEYS)
    if unknown_top:
        errors.append(f"unknown top-level keys: {unknown_top}")
    if raw.get("version") != CONFIG_VERSION or not _is_int(raw.get("version")):
        errors.append(f"version must be {CONFIG_VERSION}")

    vocabulary = _parse_vocabulary(raw.get("vocabulary"), errors)

    raw_clusters = raw.get("clusters")
    if not isinstance(raw_clusters, list) or not raw_clusters:
        errors.append("clusters must be a non-empty list")
        raw_clusters = []

    clusters: list[Cluster] = []
    seen_ids: set[str] = set()
    seen_priorities: set[int] = set()
    owner: dict[str, str] = {}  # normalized keyword -> cluster id

    for index, rc in enumerate(raw_clusters):
        label = f"clusters[{index}]"
        if not isinstance(rc, Mapping):
            errors.append(f"{label} must be an object")
            continue
        unknown = sorted(set(rc) - _CLUSTER_KEYS)
        if unknown:
            errors.append(f"{label} has unknown keys: {unknown}")
        cid = rc.get("id")
        if not _is_text(cid):
            errors.append(f"{label}.id must be a non-empty string")
            continue
        cid = str(cid).strip()
        label = f"cluster {cid!r}"
        if cid in seen_ids:
            errors.append(f"duplicate cluster id {cid!r}")
        seen_ids.add(cid)
        name = rc.get("name")
        if not _is_text(name):
            errors.append(f"{label}: name must be a non-empty string")
        priority = rc.get("priority")
        if not _is_int(priority) or priority < 1:
            errors.append(f"{label}: priority must be an integer >= 1")
            priority = 0
        elif priority in seen_priorities:
            errors.append(f"{label}: duplicate priority {priority}")
        seen_priorities.add(priority)

        raw_keywords = rc.get("keywords")
        if not isinstance(raw_keywords, list) or not raw_keywords:
            errors.append(f"{label}: keywords must be a non-empty list")
            raw_keywords = []
        members: list[ClusterKeyword] = []
        pillars = 0
        for k_index, rk in enumerate(raw_keywords):
            if not isinstance(rk, Mapping):
                errors.append(f"{label}: keywords[{k_index}] must be an object")
                continue
            unknown_k = sorted(set(rk) - _KEYWORD_KEYS)
            if unknown_k:
                errors.append(f"{label}: keywords[{k_index}] has unknown keys: {unknown_k}")
            text, role = rk.get("keyword"), rk.get("role")
            if not _is_text(text):
                errors.append(f"{label}: keywords[{k_index}].keyword must be a non-empty string")
                continue
            if role not in CLUSTER_ROLES:
                errors.append(f"{label}: keyword {text!r} has invalid role {role!r}")
                continue
            norm = normalize_keyword(str(text))
            if norm in owner:
                where = (
                    "twice in the same cluster"
                    if owner[norm] == cid
                    else (f"in clusters {owner[norm]!r} and {cid!r}")
                )
                errors.append(f"keyword {text!r} is assigned {where}")
                continue
            owner[norm] = cid
            pillars += role == ROLE_PILLAR
            members.append(ClusterKeyword(keyword=str(text).strip(), role=str(role)))
        if pillars != 1:
            errors.append(f"{label}: must have exactly one pillar (found {pillars})")
        clusters.append(
            Cluster(
                id=cid,
                name=str(name).strip() if _is_text(name) else "",
                priority=int(priority),
                keywords=tuple(members),
            )
        )

    deferred: list[DeferredCluster] = []
    raw_deferred = raw.get("deferred_clusters", [])
    if not isinstance(raw_deferred, list):
        errors.append("deferred_clusters must be a list")
        raw_deferred = []
    for d_index, rd in enumerate(raw_deferred):
        if not isinstance(rd, Mapping) or not all(_is_text(rd.get(k)) for k in _DEFERRED_KEYS):
            errors.append(f"deferred_clusters[{d_index}] needs non-empty id, name and reason")
            continue
        unknown_d = sorted(set(rd) - _DEFERRED_KEYS)
        if unknown_d:
            errors.append(f"deferred_clusters[{d_index}] has unknown keys: {unknown_d}")
        if str(rd["id"]) in seen_ids:
            errors.append(f"deferred cluster id {rd['id']!r} collides with an active cluster")
        deferred.append(
            DeferredCluster(id=str(rd["id"]), name=str(rd["name"]), reason=str(rd["reason"]))
        )

    if errors:
        raise ClusterConfigError(errors)
    return ClusterConfig(
        version=CONFIG_VERSION,
        clusters=tuple(sorted(clusters, key=lambda c: c.priority)),
        deferred=tuple(deferred),
        vocabulary=vocabulary,
    )


def load_cluster_config(path: str | Path) -> ClusterConfig:
    """追跡された cluster 定義ファイルを読み、検証して返す (DB / network には触れない)。"""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClusterConfigError([f"cluster config not found: {path}"]) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ClusterConfigError([f"cluster config is not readable JSON: {exc}"]) from exc
    return parse_cluster_config(raw)


# ==================================================================== intent
class Intent(StrEnum):
    SELECT = "select"
    COMPARE = "compare"
    PRICING = "pricing"
    FREE = "free"
    HOWTO = "howto"
    DEFINITION = "definition"
    HEAD = "head"
    ALTERNATIVE = "alternative"
    CASE_STUDY = "case_study"
    JP_SUPPORT = "jp_support"
    SUBSIDY = "subsidy"
    DIFFERENCE = "difference"


_MODIFIER_INTENT: Mapping[str, Intent] = {
    "おすすめ": Intent.SELECT,
    "オススメ": Intent.SELECT,
    "ランキング": Intent.SELECT,
    "人気": Intent.SELECT,
    "選び方": Intent.SELECT,
    "比較": Intent.COMPARE,
    "比べ": Intent.COMPARE,
    "違い": Intent.DIFFERENCE,
    "vs": Intent.COMPARE,
    "料金": Intent.PRICING,
    "価格": Intent.PRICING,
    "費用": Intent.PRICING,
    "値段": Intent.PRICING,
    "無料": Intent.FREE,
    "使い方": Intent.HOWTO,
    "導入": Intent.HOWTO,
    "やり方": Intent.HOWTO,
    "始め方": Intent.HOWTO,
    "設定方法": Intent.HOWTO,
    "手順": Intent.HOWTO,
    "とは": Intent.DEFINITION,
    "意味": Intent.DEFINITION,
    "種類": Intent.DEFINITION,
    "代替": Intent.ALTERNATIVE,
    "乗り換え": Intent.ALTERNATIVE,
    "alternative": Intent.ALTERNATIVE,
    "alternatives": Intent.ALTERNATIVE,
    "事例": Intent.CASE_STUDY,
    "導入事例": Intent.CASE_STUDY,
    "活用事例": Intent.CASE_STUDY,
    "日本語": Intent.JP_SUPPORT,
    "補助金": Intent.SUBSIDY,
    "助成金": Intent.SUBSIDY,
}

# 複数 modifier が共存するときの優先順位 (planning.classify_article_type と同じ思想)。
_INTENT_PRIORITY: tuple[Intent, ...] = (
    Intent.ALTERNATIVE,
    Intent.CASE_STUDY,
    Intent.SUBSIDY,
    Intent.JP_SUPPORT,
    Intent.HOWTO,
    Intent.DIFFERENCE,
    Intent.COMPARE,
    Intent.SELECT,
    Intent.PRICING,
    Intent.FREE,
    Intent.DEFINITION,
)

_SELECTION_FAMILY = frozenset({Intent.SELECT, Intent.COMPARE, Intent.PRICING, Intent.HEAD})
_PRODUCT_PLAN_FAMILY = frozenset(
    {Intent.SELECT, Intent.COMPARE, Intent.PRICING, Intent.FREE, Intent.HEAD}
)

_MIN_GLUED_STEM = 2

# 製品の有無に関わらず専用の SERP を持つ intent。
_DEDICATED_FAMILY: Mapping[Intent, str] = {
    Intent.HOWTO: "howto",
    Intent.DEFINITION: "definition",
    Intent.ALTERNATIVE: "alternatives",
    Intent.CASE_STUDY: "cases",
    Intent.JP_SUPPORT: "jp_support",
    Intent.SUBSIDY: "subsidy",
    Intent.DIFFERENCE: "difference",
}


def serp_family(intent: Intent, *, product_specific: bool) -> str:
    """同じ SERP を狙う intent の集合名。同じ family + 同じ theme は cannibalization 候補。"""

    dedicated = _DEDICATED_FAMILY.get(intent)
    if dedicated is not None:
        return dedicated
    if product_specific:
        return "product_plan" if intent in _PRODUCT_PLAN_FAMILY else "other"
    if intent in _SELECTION_FAMILY:
        return "selection"
    if intent is Intent.FREE:
        return "free"
    return "other"


@dataclass(frozen=True)
class IntentProfile:
    keyword: str
    theme_tokens: frozenset[str]
    theme_text: str
    intent: Intent
    product_specific: bool
    product_key: frozenset[str]
    family: str
    #: theme から製品 token を除いた残り (ClickUp 料金 と ClickUp 代替 を区別する qualifier)。
    residual_tokens: frozenset[str] = frozenset()
    #: 適用された alias の数 (canonical な表記を anchor に選ぶための tie-break)。
    alias_hits: int = 0


def _split_glued(token: str) -> list[str]:
    if token in _MODIFIER_INTENT:
        return [token]
    for modifier in _MODIFIER_INTENT:
        if modifier.isascii() or not token.endswith(modifier) or token == modifier:
            continue
        stem = token[: -len(modifier)]
        if len(stem) >= _MIN_GLUED_STEM:
            return [stem, modifier]
    return [token]


def _apply_aliases(tokens: list[str], aliases: Mapping[str, str]) -> tuple[list[str], int]:
    """token 列に (複数 token の) alias を最長一致で適用する。適用回数も返す。"""

    if not aliases:
        return tokens, 0
    phrases = sorted(
        ((tuple(k.split()), v) for k, v in aliases.items() if k),
        key=lambda item: -len(item[0]),
    )
    out: list[str] = []
    hits = 0
    i = 0
    while i < len(tokens):
        for phrase, value in phrases:
            n = len(phrase)
            if tuple(tokens[i : i + n]) == phrase:
                out.append(value)
                i += n
                hits += 1
                break
        else:
            out.append(tokens[i])
            i += 1
    return out, hits


def _longest_product_terms(matched: set[str]) -> frozenset[str]:
    """他の一致語の token 部分集合になる語 (Notion は Notion AI の部分) を除く。"""

    return frozenset(
        term
        for term in matched
        if not any(term != other and set(term.split()) < set(other.split()) for other in matched)
    )


def intent_profile(keyword: str, vocabulary: Vocabulary | None = None) -> IntentProfile:
    """keyword から (modifier を除いた theme, intent, SERP family) を決定論的に導く。"""

    vocab = vocabulary or Vocabulary()
    normalized = normalize_keyword(keyword)
    generic = {normalize_keyword(t) for t in vocab.generic_theme_tokens}
    aliases = {normalize_keyword(k): normalize_keyword(v) for k, v in vocab.theme_aliases}
    optional = {normalize_keyword(t) for t in vocab.optional_qualifiers}

    tokens: list[str] = []
    for raw in normalized.split():
        tokens.extend(_split_glued(raw))
    tokens, alias_hits = _apply_aliases(tokens, aliases)

    intents: set[Intent] = set()
    theme: list[str] = []
    for token in tokens:
        modifier_intent = _MODIFIER_INTENT.get(token)
        if modifier_intent is not None:
            intents.add(modifier_intent)
            continue
        if token in generic:
            continue
        theme.append(token)
    # 任意 qualifier は、他の token が残る場合だけ落とす (theme を空にしない)。
    core = [t for t in theme if t not in optional]
    if core:
        theme = core

    intent = next((i for i in _INTENT_PRIORITY if i in intents), Intent.HEAD)
    product_key = _longest_product_terms(
        {
            normalize_keyword(term)
            for term in vocab.product_terms
            if term_matches(normalize_keyword(term), normalized)
        }
    )
    product_specific = bool(product_key)
    covered = {token for term in product_key for token in term.split()}
    family = serp_family(intent, product_specific=product_specific)
    if product_specific and len(product_key) >= 2 and intent is Intent.COMPARE:
        family = "alternatives"  # A vs B の直接比較は、各製品の代替記事と同じ SERP を狙う
    return IntentProfile(
        keyword=keyword,
        theme_tokens=frozenset(theme),
        theme_text=" ".join(theme),
        intent=intent,
        product_specific=product_specific,
        product_key=product_key,
        family=family,
        residual_tokens=frozenset(t for t in theme if t not in covered),
        alias_hits=alias_hits,
    )


@dataclass(frozen=True)
class Overlap:
    kind: str  # "intent" | "char"
    similarity: float
    reason: str


def _covers(single: IntentProfile, multi: IntentProfile) -> bool:
    """1 製品の「代替」記事が、その製品を含む A vs B の直接比較を包含するか。"""

    return (
        single.intent is Intent.ALTERNATIVE
        and len(single.product_key) == 1
        and multi.intent is Intent.COMPARE
        and len(multi.product_key) >= 2
        and single.product_key < multi.product_key
    )


def _compare_product_profiles(a: IntentProfile, b: IntentProfile) -> Overlap | None:
    """製品固有 keyword の重複。同じ製品 + 同じ qualifier + 同じ SERP family のみ重複。

    ``ClickUp 料金`` (product_plan) と ``ClickUp 代替`` (alternatives) は family が違うので
    別記事。製品名だけの一致では決して重複としない (qualifier も比較する)。
    """

    if a.product_key == b.product_key and a.residual_tokens == b.residual_tokens:
        return Overlap(
            "intent",
            1.0,
            f"same product {sorted(a.product_key)} and same SERP family '{a.family}'",
        )
    if a.family == "alternatives" and a.residual_tokens == b.residual_tokens:
        if _covers(a, b) or _covers(b, a):
            return Overlap(
                "intent",
                1.0,
                "head-to-head comparison is covered by the alternatives page of a product",
            )
    return None


def compare_profiles(a: IntentProfile, b: IntentProfile) -> Overlap | None:
    """2 keyword が実質同じ SERP intent を狙うか。狙うなら理由付きで :class:`Overlap`。"""

    if a.family != b.family or a.product_specific != b.product_specific:
        return None
    if a.product_specific:
        return _compare_product_profiles(a, b)
    if a.theme_tokens and a.theme_tokens == b.theme_tokens:
        return Overlap(
            "intent",
            1.0,
            f"same theme '{a.theme_text}' and same SERP family '{a.family}' "
            f"({a.intent.value} vs {b.intent.value})",
        )
    if a.theme_text and b.theme_text:
        sim = text_similarity(a.theme_text, b.theme_text).similarity
        if sim >= CHAR_OVERLAP_THRESHOLD:
            return Overlap(
                "char",
                round(sim, 4),
                f"near-identical theme text ({sim:.2f}) in SERP family '{a.family}'",
            )
    return None


def near_overlap_similarity(a: IntentProfile, b: IntentProfile) -> float | None:
    """参考情報 (decision には使わない): 同 family で theme が似ているが overlap ではない類似度。"""

    if a.family != b.family or a.product_specific or b.product_specific:
        return None
    if not a.theme_text or not b.theme_text or a.theme_tokens == b.theme_tokens:
        return None
    sim = text_similarity(a.theme_text, b.theme_text).similarity
    if NEAR_OVERLAP_THRESHOLD <= sim < CHAR_OVERLAP_THRESHOLD:
        return round(sim, 2)
    return None


# ==================================================================== inputs
@dataclass(frozen=True)
class AffiliateMatch:
    program_id: int
    name: str
    provider: str | None
    # C2.5.4 (報告専用・追加): strong | weak。None = tier 未算出 (legacy の呼び出し)。
    tier: str | None = None
    # legacy の match_programs でも match したか。False = alias だけで strong になった program
    # (legacy の covered 集合には入れない)。
    legacy: bool = True
    # C2.5.7 (追加): brand tier とは独立した fit。core | loose | unreviewed、None = 該当なし
    # (strong の brand だけ) / tier 未算出。eligible = strong、または weak かつ core
    # (scoring / primary の対象)。
    fit: str | None = None
    eligible: bool | None = None


@dataclass(frozen=True)
class KeywordInput:
    id: int
    keyword: str
    status: str
    opportunity_score: float | None = None
    components: Mapping[str, float] | None = None
    missing_components: tuple[str, ...] = ()
    affiliate_matches: tuple[AffiliateMatch, ...] = ()


@dataclass(frozen=True)
class ArticleInput:
    id: int
    keyword_id: int | None
    keyword: str | None
    title: str
    status: str
    fact_count: int = 0


# =================================================================== outputs
@dataclass(frozen=True)
class AffiliateCoverage:
    """keyword の affiliate coverage。

    ``level`` / ``program_count`` / ``providers`` / ``program_names`` は **legacy の covered 意味**
    (legacy の ``match_programs`` で match した program) で、tier の有無・alias に関わらず不変。

    C2.5.4 の tier 項目は報告専用の追加:

    - ``strong_program_count`` / ``strong_program_names``: 自身の名前 / 明示 alias で match
    - ``weak_program_count`` / ``weak_program_names``: generic な term だけで match した program
      (legacy で match した program のみ。alias だけの program は必ず strong)
    - ``alias_only_strong_program_names``: 明示 alias **だけ** で strong になった program。
      legacy の covered には入らない (``program_names`` に無い) ので、ここで明示的に識別できる
    - ``no_strong_affiliate_match``: strong が 0 件
    - tier 未算出の match (legacy の呼び出し) を含む場合は None、match が 0 件なら 0 / True

    C2.5.7 の fit 項目 (追加): weak を fit で分ける。``eligible`` (= strong + weak かつ core) が
    「収益化の対象になり得る」program。loose / unreviewed は文脈・報告用で、eligible に入らない。

    - ``core_weak_program_names``: weak かつ core (brand 名は無いが、その category の本命)
    - ``loose_weak_program_names`` / ``unreviewed_weak_program_names``: weak かつ loose / unreviewed
    - ``eligible_program_names``: strong + core の weak (name 順)
    - ``no_eligible_affiliate_match``: eligible が 0 件
    """

    level: str  # none | single | multiple
    program_count: int
    providers: tuple[str, ...]
    program_names: tuple[str, ...]
    # ---- C2.5.4 (報告専用・追加)
    strong_program_count: int | None = None
    weak_program_count: int | None = None
    strong_program_names: tuple[str, ...] = ()
    weak_program_names: tuple[str, ...] = ()
    no_strong_affiliate_match: bool | None = None
    alias_only_strong_program_names: tuple[str, ...] = ()
    # ---- C2.5.7 (追加): weak の fit 内訳。tier 未算出なら空 / None
    core_weak_program_names: tuple[str, ...] = ()
    loose_weak_program_names: tuple[str, ...] = ()
    unreviewed_weak_program_names: tuple[str, ...] = ()
    eligible_program_names: tuple[str, ...] = ()
    no_eligible_affiliate_match: bool | None = None


@dataclass(frozen=True)
class FactResearch:
    required: bool
    requirement: str  # tool_facts | official_sources | unknown_type
    status: str  # present | missing
    existing_fact_count: int
    # research の対象 program (収益化の対象になり得るもの): strong + weak かつ core (C2.5.7)。
    # loose / unreviewed は含めない (収益化の subject 集合を膨らませない)。tier 未算出の呼び出し
    # では legacy の covered program。
    suggested_subjects: tuple[str, ...]
    # C2.5.6 / C2.5.7 (追加): suggested_subjects の内訳 (tier 未算出なら空)
    strong_subjects: tuple[str, ...] = ()
    core_subjects: tuple[str, ...] = ()  # weak かつ core
    # 文脈・報告用 (weak かつ loose / unreviewed)。suggested_subjects には入れないが research に
    # 残せるよう metadata として保持する
    contextual_subjects: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContentMonetization:
    """C2.5.8: queue entry の monetization mode と production readiness (報告のみ・順序は不変)。

    affiliate の有無は「作ってよいか」を決めない。

    - ``recommended_mode``: affiliate (strong か core の weak) | supporting。tier 未算出は None
    - ``production_readiness``:
      ``affiliate_ready`` (affiliate の primary を選べ、他の blocker が無い) /
      ``supporting_only`` (affiliate の primary は無いが supporting として作れる) /
      ``blocked`` (affiliate 以外の理由で止まる: ``blockers``) /
      ``not_a_slot`` (merge / blocked の entry。新しい記事にはならない)
    - ``blockers``: affiliate 以外の blocker (記事タイプ未確定 / template 無し / 比較対象なし)
    """

    recommended_mode: str | None
    production_readiness: str
    blockers: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class TemplateReadiness:
    ready: bool
    template_version: str | None
    reason: str


@dataclass(frozen=True)
class QueueEntry:
    keyword_id: int
    keyword: str
    cluster_id: str
    cluster_name: str
    cluster_priority: int
    role: str
    keyword_status: str
    opportunity_score: float | None
    components: Mapping[str, float] | None
    missing_components: tuple[str, ...]
    article_type: str | None
    intent: str
    serp_family: str
    decision: str
    reason_code: str
    reason: str
    position: int | None
    merge_group: str | None
    merge_target_keyword_id: int | None
    merge_target_keyword: str | None
    absorbed_keywords: tuple[str, ...]
    existing_article_id: int | None
    existing_article_status: str | None
    overlaps_article_id: int | None
    overlaps_article_status: str | None
    affiliate: AffiliateCoverage
    fact_research: FactResearch
    template: TemplateReadiness
    prerequisites: tuple[str, ...]
    notes: tuple[str, ...]
    # C2.5.8 (追加・末尾): monetization mode / production readiness (報告のみ)
    monetization: ContentMonetization | None = None


@dataclass(frozen=True)
class UnassignedKeyword:
    keyword_id: int
    keyword: str
    keyword_status: str
    opportunity_score: float | None
    reason: str


@dataclass(frozen=True)
class ContentQueue:
    config_version: int
    slots: tuple[QueueEntry, ...]
    merged: tuple[QueueEntry, ...]
    blocked: tuple[QueueEntry, ...]
    unassigned: tuple[UnassignedKeyword, ...]
    deferred: tuple[DeferredCluster, ...]
    warnings: tuple[str, ...]
    summary: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ================================================================== helpers
def template_readiness(article_type: ArticleType | None) -> TemplateReadiness:
    if article_type is None:
        return TemplateReadiness(False, None, "article_type_unclassified")
    for version in sorted(TEMPLATE_SCOPE):
        if article_type in TEMPLATE_SCOPE[version]:
            return TemplateReadiness(True, version, "template available")
    return TemplateReadiness(False, None, f"no prompt template for {article_type.value}")


def affiliate_coverage(matches: Iterable[AffiliateMatch]) -> AffiliateCoverage:
    everything = sorted(matches, key=lambda m: (m.name, m.program_id))
    # legacy の covered 意味: legacy の match_programs で match した program だけ (alias のみの
    # strong は含めない)。この 4 項目は tier の有無に関わらず従来と同一。
    ordered = [m for m in everything if m.legacy]
    providers = tuple(sorted({m.provider for m in ordered if m.provider}))
    count = len(ordered)
    level = "none" if count == 0 else "single" if count == 1 else "multiple"
    tiered = all(m.tier is not None for m in everything)  # 0 件も True (strong は無い)
    strong = tuple(m.name for m in everything if m.tier == "strong") if tiered else ()
    weak = tuple(m.name for m in everything if m.tier == "weak") if tiered else ()
    alias_only = (
        tuple(m.name for m in everything if m.tier == "strong" and not m.legacy) if tiered else ()
    )
    core_weak = (
        tuple(m.name for m in everything if m.tier == "weak" and m.fit == "core") if tiered else ()
    )
    loose_weak = (
        tuple(m.name for m in everything if m.tier == "weak" and m.fit == "loose") if tiered else ()
    )
    unreviewed_weak = (
        tuple(m.name for m in everything if m.tier == "weak" and m.fit not in ("core", "loose"))
        if tiered
        else ()
    )
    eligible = tuple(m.name for m in everything if m.eligible) if tiered else ()
    return AffiliateCoverage(
        level=level,
        program_count=count,
        providers=providers,
        program_names=tuple(m.name for m in ordered),
        strong_program_count=len(strong) if tiered else None,
        weak_program_count=len(weak) if tiered else None,
        strong_program_names=strong,
        weak_program_names=weak,
        no_strong_affiliate_match=(not strong) if tiered else None,
        alias_only_strong_program_names=alias_only,
        core_weak_program_names=core_weak,
        loose_weak_program_names=loose_weak,
        unreviewed_weak_program_names=unreviewed_weak,
        eligible_program_names=eligible,
        no_eligible_affiliate_match=(not eligible) if tiered else None,
    )


def affiliate_matches_from_tiered(tiered: Iterable[TieredMatch]) -> tuple[AffiliateMatch, ...]:
    """tier 付き match を報告用の :class:`AffiliateMatch` に写す (legacy かどうかも保持)。"""

    return tuple(
        AffiliateMatch(
            program_id=m.program_id,
            name=m.name,
            provider=m.provider,
            tier=m.tier,
            legacy=m.legacy_matched,
            fit=m.fit,
            eligible=m.scoring_eligible,
        )
        for m in tiered
    )


def fact_research(
    article_type: ArticleType | None,
    *,
    existing_fact_count: int,
    subjects: tuple[str, ...],
    strong_subjects: tuple[str, ...] = (),
    core_subjects: tuple[str, ...] = (),
    contextual_subjects: tuple[str, ...] = (),
) -> FactResearch:
    if article_type in (ArticleType.RECOMMENDATION_ROUNDUP, ArticleType.COMPARISON_LISTICLE):
        requirement = "tool_facts"
    elif article_type is None:
        requirement = "unknown_type"
    else:
        requirement = "official_sources"
    return FactResearch(
        required=True,
        requirement=requirement,
        status="present" if existing_fact_count > 0 else "missing",
        existing_fact_count=existing_fact_count,
        suggested_subjects=subjects,
        strong_subjects=strong_subjects,
        core_subjects=core_subjects,
        contextual_subjects=contextual_subjects,
    )


def content_monetization(
    *,
    decision: str,
    article_type: ArticleType | None,
    template: TemplateReadiness,
    coverage: AffiliateCoverage,
) -> ContentMonetization:
    """C2.5.8: entry の mode と production readiness。affiliate 無しは blocker にしない。"""

    from app.article import monetization  # 循環 import を避ける (monetization -> planning)

    tiered = coverage.strong_program_count is not None
    eligible = bool(coverage.eligible_program_names)
    recommended = (
        (monetization.MODE_AFFILIATE if eligible else monetization.MODE_SUPPORTING)
        if tiered
        else None
    )
    if decision != DECISION_OK:
        return ContentMonetization(
            recommended, "not_a_slot", (), f"decision={decision} (not a new article)"
        )
    blockers: list[str] = []
    if article_type is None:
        blockers.append("article_type_undetermined")
    elif not template.ready:
        blockers.append(f"no_prompt_template:{article_type.value}")
    any_candidate = (coverage.strong_program_count or 0) + (coverage.weak_program_count or 0) > 0
    if (
        not eligible
        and monetization.requires_comparison_subjects(article_type)
        and not any_candidate
    ):
        blockers.append(monetization.SUBJECTS_UNAVAILABLE)
    if blockers:
        return ContentMonetization(
            recommended, "blocked", tuple(blockers), "blocked for non-affiliate reasons"
        )
    if eligible:
        return ContentMonetization(
            recommended, "affiliate_ready", (), "primary-eligible affiliate available"
        )
    return ContentMonetization(
        recommended,
        "supporting_only",
        (),
        "no primary-eligible affiliate: produce as supporting content (not a blocker)",
    )


def _role_rank(role: str) -> int:
    return 0 if role == ROLE_PILLAR else 1


def _score_key(score: float | None) -> tuple[int, float]:
    return (0, -score) if score is not None else (1, 0.0)


@dataclass
class _Work:
    kw: KeywordInput
    cluster: Cluster
    role: str
    profile: IntentProfile
    article_type: ArticleType | None
    decision: str = DECISION_OK
    reason_code: str = "independent_slot"
    reason: str = "distinct intent; no overlap with existing articles or other queued keywords"
    existing_article: ArticleInput | None = None
    overlaps_article: ArticleInput | None = None
    merge_target: _Work | None = None
    absorbed: list[str] = field(default_factory=list)
    position: int | None = None
    near: list[str] = field(default_factory=list)


def _resolve_keywords(
    config: ClusterConfig, keywords: Sequence[KeywordInput]
) -> tuple[dict[str, KeywordInput], list[str]]:
    by_norm: dict[str, list[KeywordInput]] = {}
    for kw in keywords:
        by_norm.setdefault(normalize_keyword(kw.keyword), []).append(kw)

    errors: list[str] = []
    resolved: dict[str, KeywordInput] = {}
    for cluster in config.clusters:
        for member in cluster.keywords:
            norm = normalize_keyword(member.keyword)
            found = by_norm.get(norm, [])
            if not found:
                errors.append(f"unknown keyword {member.keyword!r} (cluster {cluster.id!r})")
            elif len(found) > 1:
                errors.append(f"ambiguous keyword {member.keyword!r} (cluster {cluster.id!r})")
            else:
                resolved[norm] = found[0]
    return resolved, errors


# ============================================================== queue builder
def build_content_queue(
    config: ClusterConfig,
    keywords: Sequence[KeywordInput],
    articles: Sequence[ArticleInput],
) -> ContentQueue:
    """cluster 定義 + keyword pool + 既存 article から read-only な制作キューを導出する。

    ``articles`` は呼び出し側が **non-archived のみ** を渡す契約 (archived は競合相手ではない)。
    入力順に依存しない: 同じ入力集合なら常に同一の結果を返す。
    """

    resolved, errors = _resolve_keywords(config, keywords)
    if errors:
        raise ClusterConfigError(errors)

    vocab = config.vocabulary
    warnings: list[str] = []

    works: list[_Work] = []
    for cluster in config.clusters:
        for member in cluster.keywords:
            kw = resolved[normalize_keyword(member.keyword)]
            works.append(
                _Work(
                    kw=kw,
                    cluster=cluster,
                    role=member.role,
                    profile=intent_profile(kw.keyword, vocab),
                    article_type=classify_article_type(kw.keyword).article_type,
                )
            )

    ordered_articles = sorted(articles, key=lambda a: a.id)
    article_profiles: list[tuple[ArticleInput, IntentProfile | None]] = []
    for article in ordered_articles:
        if article.keyword:
            article_profiles.append((article, intent_profile(article.keyword, vocab)))
        else:
            article_profiles.append((article, None))
            warnings.append(
                f"article #{article.id} ({article.status}) has no keyword; "
                "it cannot be checked for intent overlap"
            )

    # ---- 1) 既存 article との衝突 (idea/planned/drafting/review を含む全 non-archived) --------
    for work in works:
        own = [a for a in ordered_articles if a.keyword_id == work.kw.id]
        if own:
            work.decision = DECISION_BLOCKED
            work.reason_code = "already_has_article"
            work.reason = (
                f"keyword already has article #{own[0].id} (status={own[0].status}); "
                "do not create another"
            )
            work.existing_article = own[0]
            continue
        if work.kw.status == "rejected":
            work.decision = DECISION_BLOCKED
            work.reason_code = "keyword_rejected"
            work.reason = "keyword status is 'rejected'"
            continue
        for article, profile in article_profiles:
            if profile is None:
                continue
            overlap = compare_profiles(work.profile, profile)
            if overlap is not None:
                work.decision = DECISION_BLOCKED
                work.reason_code = "overlaps_existing_article"
                work.reason = (
                    f"same SERP intent as article #{article.id} "
                    f"(status={article.status}): {overlap.reason}"
                )
                work.overlaps_article = article
                break

    # ---- 2) queue 内 keyword どうしの意図重複 -> merge (anchor 方式で chain 併合を避ける) -----
    candidates = [w for w in works if w.decision == DECISION_OK]
    candidates.sort(
        key=lambda w: (
            _role_rank(w.role),
            w.cluster.priority,
            _score_key(w.kw.opportunity_score),
            w.kw.id,
        )
    )
    anchors: list[_Work] = []
    for work in candidates:
        for anchor in anchors:
            overlap = compare_profiles(work.profile, anchor.profile)
            if overlap is not None:
                work.decision = DECISION_MERGE
                work.reason_code = "intent_overlap_with_queued_keyword"
                work.reason = (
                    f"substantially the same SERP intent as queued '{anchor.kw.keyword}': "
                    f"{overlap.reason}"
                )
                work.merge_target = anchor
                anchor.absorbed.append(work.kw.keyword)
                break
        else:
            anchors.append(work)

    # ---- 3) queue 順序 (ok のみ位置を持つ) ---------------------------------------------
    slots = sorted(
        (w for w in works if w.decision == DECISION_OK),
        key=lambda w: (
            w.cluster.priority,
            _role_rank(w.role),
            _score_key(w.kw.opportunity_score),
            w.kw.id,
        ),
    )
    for position, work in enumerate(slots, start=1):
        work.position = position
        for other in slots:
            if other is work:
                continue
            sim = near_overlap_similarity(work.profile, other.profile)
            if sim is not None:
                work.near.append(f"near_overlap:{other.kw.keyword} ({sim:.2f})")

    facts_by_keyword: dict[int, int] = {}
    for article in ordered_articles:
        if article.keyword_id is not None:
            facts_by_keyword[article.keyword_id] = max(
                facts_by_keyword.get(article.keyword_id, 0), article.fact_count
            )

    def freeze(work: _Work) -> QueueEntry:
        kw = work.kw
        coverage = affiliate_coverage(kw.affiliate_matches)
        # research の対象 (収益化の対象になり得る program) は strong + weak かつ core。loose /
        # unreviewed は文脈用として別に保持し、収益化の subject 集合を膨らませない (C2.5.7)。
        # tier 未算出 (legacy の呼び出し) の入力では従来どおり legacy の covered program。
        if coverage.strong_program_count is not None:
            research_subjects = tuple(
                sorted({*coverage.strong_program_names, *coverage.core_weak_program_names})
            )
        else:
            research_subjects = coverage.program_names
        research = fact_research(
            work.article_type,
            existing_fact_count=facts_by_keyword.get(kw.id, 0),
            subjects=research_subjects,
            strong_subjects=coverage.strong_program_names,
            core_subjects=coverage.core_weak_program_names,
            contextual_subjects=tuple(
                sorted(
                    {*coverage.loose_weak_program_names, *coverage.unreviewed_weak_program_names}
                )
            ),
        )
        template = template_readiness(work.article_type)
        prerequisites: list[str] = []
        notes: list[str] = list(work.near)
        if work.decision == DECISION_OK:
            if kw.opportunity_score is None:
                if kw.missing_components:
                    prerequisites.append(
                        "signals_incomplete:" + "|".join(sorted(kw.missing_components))
                    )
                else:
                    prerequisites.append("unscored")
            if work.article_type is None:
                prerequisites.append("article_type_unclassified")
            elif not template.ready:
                prerequisites.append(f"no_prompt_template:{work.article_type.value}")
            if research.status == "missing":
                prerequisites.append(f"fact_research:{research.requirement}")
            if coverage.level == "none":
                notes.append("no_affiliate_match")
            if coverage.no_strong_affiliate_match:
                notes.append("no_strong_affiliate_match")  # C2.5.4: 報告のみ (順序に影響しない)
            if coverage.no_eligible_affiliate_match:
                # C2.5.7: strong も core の weak も無い (loose / unreviewed は文脈のみ)。報告のみ
                notes.append("no_eligible_affiliate_match")
        target = work.merge_target
        return QueueEntry(
            keyword_id=kw.id,
            keyword=kw.keyword,
            cluster_id=work.cluster.id,
            cluster_name=work.cluster.name,
            cluster_priority=work.cluster.priority,
            role=work.role,
            keyword_status=kw.status,
            opportunity_score=kw.opportunity_score,
            components=dict(kw.components) if kw.components is not None else None,
            missing_components=tuple(sorted(kw.missing_components)),
            article_type=work.article_type.value if work.article_type else None,
            intent=work.profile.intent.value,
            serp_family=work.profile.family,
            decision=work.decision,
            reason_code=work.reason_code,
            reason=work.reason,
            position=work.position,
            merge_group=(f"mg-{(target or work).kw.id}" if (target or work.absorbed) else None),
            merge_target_keyword_id=target.kw.id if target else None,
            merge_target_keyword=target.kw.keyword if target else None,
            absorbed_keywords=tuple(sorted(work.absorbed)),
            existing_article_id=work.existing_article.id if work.existing_article else None,
            existing_article_status=(
                work.existing_article.status if work.existing_article else None
            ),
            overlaps_article_id=work.overlaps_article.id if work.overlaps_article else None,
            overlaps_article_status=(
                work.overlaps_article.status if work.overlaps_article else None
            ),
            affiliate=coverage,
            fact_research=research,
            template=template,
            prerequisites=tuple(prerequisites),
            notes=tuple(notes),
            monetization=content_monetization(
                decision=work.decision,
                article_type=work.article_type,
                template=template,
                coverage=coverage,
            ),
        )

    merged_works = sorted(
        (w for w in works if w.decision == DECISION_MERGE),
        key=lambda w: (w.cluster.priority, _role_rank(w.role), w.kw.id),
    )
    blocked_works = sorted(
        (w for w in works if w.decision == DECISION_BLOCKED),
        key=lambda w: (w.cluster.priority, _role_rank(w.role), w.kw.id),
    )
    slot_entries = tuple(freeze(w) for w in slots)
    merged_entries = tuple(freeze(w) for w in merged_works)
    blocked_entries = tuple(freeze(w) for w in blocked_works)

    assigned_ids = {w.kw.id for w in works}
    unassigned = tuple(
        UnassignedKeyword(
            keyword_id=kw.id,
            keyword=kw.keyword,
            keyword_status=kw.status,
            opportunity_score=kw.opportunity_score,
            reason="not in any active cluster (out of scope or deferred)",
        )
        for kw in sorted(keywords, key=lambda k: k.id)
        if kw.id not in assigned_ids
    )

    by_cluster: dict[str, dict[str, int]] = {}
    for cluster in config.clusters:
        by_cluster[cluster.id] = {
            "new_slots": sum(1 for e in slot_entries if e.cluster_id == cluster.id),
            "merged": sum(1 for e in merged_entries if e.cluster_id == cluster.id),
            "blocked": sum(1 for e in blocked_entries if e.cluster_id == cluster.id),
        }
    summary: dict[str, Any] = {
        "config_version": config.version,
        "assigned_keywords": len(works),
        "unassigned_keywords": len(unassigned),
        "new_slots": len(slot_entries),
        "merged": len(merged_entries),
        "blocked": len(blocked_entries),
        "template_ready_slots": sum(1 for e in slot_entries if e.template.ready),
        "slots_with_prerequisites": sum(1 for e in slot_entries if e.prerequisites),
        "by_cluster": by_cluster,
        # C2.5.8 (追加): slot の production readiness (affiliate 無し != 作らない)
        "production_readiness": {
            state: sum(
                1
                for e in slot_entries
                if e.monetization is not None and e.monetization.production_readiness == state
            )
            for state in ("affiliate_ready", "supporting_only", "blocked")
        },
    }
    return ContentQueue(
        config_version=config.version,
        slots=slot_entries,
        merged=merged_entries,
        blocked=blocked_entries,
        unassigned=unassigned,
        deferred=config.deferred,
        warnings=tuple(warnings),
        summary=summary,
    )
