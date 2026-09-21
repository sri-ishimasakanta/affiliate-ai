"""Keyword expansion planner (pure・決定論的)。

keyword idea 候補を、既存 keyword / 既存・進行中の article / C2.2 制作キューと突き合わせて
``keep | merge | reject`` に振り分ける。DB / HTTP / LLM / SQLAlchemy に依存しない。

判定順 (最初に一致したものを採用):

1. ``reject``: 既存 keyword との完全一致 / 候補内の重複 / ``reject_rules`` (B2C・一般ノイズ /
   off-theme / 競合ブランド / 触らない製品 x intent など)。
2. ``merge`` (既存 article): non-archived な article の keyword と同じ SERP intent。
3. ``merge`` (curated): 意味的には同一だが語彙規則では表せない少数の対応 (``curated_merges``)。
4. ``merge`` (既存 keyword): 既存 keyword (C2.2 の slot / 吸収先 / article) と同じ SERP intent。
   吸収先は C2.2 キューの anchor に解決する。
5. ``merge`` (候補どうし): anchor 方式 (chain 併合しない)。anchor は intent の強さ、canonical な
   表記 (alias 適用が少ない)、検索ボリューム、文字列の順で決まる。
6. それ以外は ``keep``。

指標 (avg_monthly_searches 等) は API が返した場合だけ表示し、無ければ ``not_available``。
affiliate coverage は呼び出し側が live DB catalog から解決して渡す。scoring weight・cluster 定義は
変更しない。keyword の追加は一切行わない。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.article.cluster_plan import (
    AffiliateCoverage,
    AffiliateMatch,
    ArticleInput,
    ClusterConfig,
    ContentQueue,
    Intent,
    IntentProfile,
    KeywordInput,
    affiliate_coverage,
    compare_profiles,
    intent_profile,
    near_overlap_similarity,
)
from app.keyword.equivalence import CANONICAL_ACRONYMS, canonical_acronym, duplicate_key
from app.keyword.normalizers.site_relevance import normalize_keyword

RULES_VERSION = 1

DECISION_KEEP = "keep"
DECISION_MERGE = "merge"
DECISION_REJECT = "reject"

NOT_AVAILABLE = "not_available"

TARGET_CANDIDATE = "candidate"
TARGET_KEYWORD = "keyword"
TARGET_ARTICLE = "article"

# 同じ family の中で anchor になりやすい順 (小さいほど anchor)。
_ANCHOR_RANK: Mapping[Intent, int] = {
    Intent.SELECT: 0,
    Intent.ALTERNATIVE: 1,
    Intent.COMPARE: 2,
    Intent.PRICING: 3,
    Intent.HOWTO: 4,
    Intent.JP_SUPPORT: 5,
    Intent.CASE_STUDY: 6,
    Intent.SUBSIDY: 7,
    Intent.FREE: 8,
    Intent.DIFFERENCE: 9,
    Intent.DEFINITION: 10,
    Intent.HEAD: 11,
}

_RULE_KEYS = frozenset(
    {
        "id",
        "reason_code",
        "note",
        "products",
        "intents",
        "phrase",
        "exact_phrase",
        "ascii_only",
        "terms_any",
        "terms_all",
    }
)
_CURATED_KEYS = frozenset({"candidate", "target", "note"})
_TOP_KEYS = frozenset({"version", "reject_rules", "curated_merges"})
_INTENT_VALUES = frozenset(i.value for i in Intent)


class ExpansionRulesError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


# ==================================================================== rules
@dataclass(frozen=True)
class RejectRule:
    id: str
    reason_code: str
    note: str
    products: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()
    phrase: str = ""
    exact_phrase: str = ""  # 正規化後の phrase 全体がこれと一致する場合だけ
    ascii_only: bool = False  # 正規化後に ASCII だけの phrase に限る (日本語を含む phrase は対象外)
    terms_any: tuple[str, ...] = ()
    terms_all: tuple[str, ...] = ()


@dataclass(frozen=True)
class CuratedMerge:
    candidate: str
    target: str
    note: str


@dataclass(frozen=True)
class ExpansionRules:
    version: int = RULES_VERSION
    reject_rules: tuple[RejectRule, ...] = ()
    curated_merges: tuple[CuratedMerge, ...] = ()


def _texts(value: object) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value):
        return tuple(v.strip() for v in value)
    return None


def parse_expansion_rules(raw: object) -> ExpansionRules:
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise ExpansionRulesError(["expansion rules must be a JSON object"])
    unknown_top = sorted(set(raw) - _TOP_KEYS)
    if unknown_top:
        errors.append(f"unknown top-level keys: {unknown_top}")
    if raw.get("version") != RULES_VERSION or isinstance(raw.get("version"), bool):
        errors.append(f"version must be {RULES_VERSION}")

    rules: list[RejectRule] = []
    seen_ids: set[str] = set()
    raw_rules = raw.get("reject_rules", [])
    if not isinstance(raw_rules, list):
        errors.append("reject_rules must be a list")
        raw_rules = []
    for index, item in enumerate(raw_rules):
        label = f"reject_rules[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{label} must be an object")
            continue
        unknown = sorted(set(item) - _RULE_KEYS)
        if unknown:
            errors.append(f"{label} has unknown keys: {unknown}")
        rid, code = item.get("id"), item.get("reason_code")
        if not isinstance(rid, str) or not rid.strip():
            errors.append(f"{label}.id must be a non-empty string")
            continue
        if rid in seen_ids:
            errors.append(f"duplicate reject rule id {rid!r}")
        seen_ids.add(rid)
        if not isinstance(code, str) or not code.strip():
            errors.append(f"reject rule {rid!r}: reason_code must be a non-empty string")
            continue
        products, intents = _texts(item.get("products")), _texts(item.get("intents"))
        terms_any, terms_all = _texts(item.get("terms_any")), _texts(item.get("terms_all"))
        if None in (products, intents, terms_any, terms_all):
            errors.append(f"reject rule {rid!r}: list fields must be lists of non-empty strings")
            continue
        phrase, exact_phrase = item.get("phrase", ""), item.get("exact_phrase", "")
        if not isinstance(phrase, str) or not isinstance(exact_phrase, str):
            errors.append(f"reject rule {rid!r}: phrase and exact_phrase must be strings")
            continue
        ascii_only = item.get("ascii_only", False)
        if not isinstance(ascii_only, bool):
            errors.append(f"reject rule {rid!r}: ascii_only must be a boolean")
            continue
        bad_intents = sorted(set(intents) - _INTENT_VALUES)
        if bad_intents:
            errors.append(f"reject rule {rid!r}: unknown intents {bad_intents}")
        if not (products or phrase.strip() or exact_phrase.strip() or terms_any or terms_all):
            errors.append(
                f"reject rule {rid!r}: needs products, phrase, exact_phrase, terms_any or terms_all"
            )
            continue
        rules.append(
            RejectRule(
                id=rid.strip(),
                reason_code=code.strip(),
                note=str(item.get("note", "")),
                products=products,
                intents=intents,
                phrase=phrase.strip(),
                exact_phrase=exact_phrase.strip(),
                ascii_only=ascii_only,
                terms_any=terms_any,
                terms_all=terms_all,
            )
        )

    curated: list[CuratedMerge] = []
    seen_candidates: set[str] = set()
    raw_curated = raw.get("curated_merges", [])
    if not isinstance(raw_curated, list):
        errors.append("curated_merges must be a list")
        raw_curated = []
    for index, item in enumerate(raw_curated):
        label = f"curated_merges[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{label} must be an object")
            continue
        unknown = sorted(set(item) - _CURATED_KEYS)
        if unknown:
            errors.append(f"{label} has unknown keys: {unknown}")
        cand, target = item.get("candidate"), item.get("target")
        if not all(isinstance(v, str) and v.strip() for v in (cand, target)):
            errors.append(f"{label}: candidate and target must be non-empty strings")
            continue
        key = normalize_keyword(str(cand))
        if key == normalize_keyword(str(target)):
            errors.append(f"{label}: candidate and target must differ")
        if key in seen_candidates:
            errors.append(f"{label}: duplicate curated candidate {cand!r}")
        seen_candidates.add(key)
        curated.append(
            CuratedMerge(str(cand).strip(), str(target).strip(), str(item.get("note", "")))
        )

    if errors:
        raise ExpansionRulesError(errors)
    return ExpansionRules(
        version=RULES_VERSION, reject_rules=tuple(rules), curated_merges=tuple(curated)
    )


def load_expansion_rules(path: str | Path) -> ExpansionRules:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExpansionRulesError([f"expansion rules not found: {path}"]) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ExpansionRulesError([f"expansion rules are not readable JSON: {exc}"]) from exc
    return parse_expansion_rules(raw)


# ==================================================================== inputs / outputs
@dataclass(frozen=True)
class IdeaCandidate:
    keyword: str
    cluster: str | None = None
    metrics: Mapping[str, Any] | None = None
    affiliate_matches: tuple[AffiliateMatch, ...] = ()


@dataclass(frozen=True)
class ExpansionDecision:
    keyword: str
    cluster: str | None
    decision: str
    reason_code: str
    reason: str
    target: str | None
    target_kind: str | None
    target_article_id: int | None
    intent: str
    serp_family: str
    overlap_risk: str | None
    overlap_note: str
    affiliate: AffiliateCoverage
    metrics: Mapping[str, Any] | str
    matched_rule: str | None


@dataclass(frozen=True)
class ExpansionPlan:
    decisions: tuple[ExpansionDecision, ...]
    warnings: tuple[str, ...]
    summary: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Work:
    candidate: IdeaCandidate
    normalized: str
    profile: IntentProfile
    decision: str = DECISION_KEEP
    reason_code: str = "distinct_intent"
    reason: str = "distinct intent; no overlap with existing keywords, articles or other candidates"
    target: str | None = None
    target_kind: str | None = None
    target_article_id: int | None = None
    matched_rule: str | None = None
    absorbed: int = 0
    curated_target: str | None = None


# ==================================================================== helpers
def _volume(candidate: IdeaCandidate) -> int:
    metrics = candidate.metrics or {}
    value = metrics.get("avg_monthly_searches")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _rule_matches(rule: RejectRule, normalized: str, profile: IntentProfile) -> bool:
    tokens = set(normalized.split())
    padded = f" {normalized} "
    if rule.ascii_only and not normalized.isascii():
        return False
    if rule.exact_phrase and normalized != normalize_keyword(rule.exact_phrase):
        return False
    if rule.products:
        wanted = {normalize_keyword(p) for p in rule.products}
        if not wanted & profile.product_key:
            return False
    if rule.intents and profile.intent.value not in rule.intents:
        return False
    if rule.phrase and f" {normalize_keyword(rule.phrase)} " not in padded:
        return False
    if rule.terms_any and not any(normalize_keyword(t) in tokens for t in rule.terms_any):
        return False
    return all(normalize_keyword(t) in tokens for t in rule.terms_all)


def _work_sort_key(work: _Work) -> tuple:
    return (
        _ANCHOR_RANK[work.profile.intent],
        work.profile.alias_hits,
        -_volume(work.candidate),
        work.normalized,
    )


# ==================================================================== planner
def plan_expansion(
    config: ClusterConfig,
    rules: ExpansionRules,
    candidates: Sequence[IdeaCandidate],
    keywords: Sequence[KeywordInput],
    articles: Sequence[ArticleInput],
    queue: ContentQueue,
) -> ExpansionPlan:
    """候補を keep / merge / reject に振り分ける (read-only・入力順に依存しない)。"""

    vocab = config.vocabulary
    warnings: list[str] = []

    existing = sorted(keywords, key=lambda k: k.id)
    existing_by_norm = {normalize_keyword(k.keyword): k for k in existing}
    # 日本語 phrase の空白位置の差だけを吸収する fallback (完全一致 / 重複の判定専用)
    existing_by_equiv: dict[str, KeywordInput] = {}
    for k in existing:
        existing_by_equiv.setdefault(duplicate_key(k.keyword), k)
    existing_profiles = {k.id: intent_profile(k.keyword, vocab) for k in existing}

    # 既存 keyword -> C2.2 queue 上の吸収先 (anchor)
    anchor_of: dict[int, tuple[str, str, int | None]] = {}
    for entry in queue.slots:
        anchor_of[entry.keyword_id] = (TARGET_KEYWORD, entry.keyword, None)
    for entry in queue.merged:
        anchor_of[entry.keyword_id] = (
            TARGET_KEYWORD,
            entry.merge_target_keyword or entry.keyword,
            None,
        )
    article_keyword = {a.id: a.keyword for a in articles if a.keyword}
    for entry in queue.blocked:
        aid = entry.existing_article_id or entry.overlaps_article_id
        if aid is not None:
            anchor_of[entry.keyword_id] = (
                TARGET_ARTICLE,
                article_keyword.get(aid, entry.keyword),
                aid,
            )

    article_profiles = [
        (a, intent_profile(a.keyword, vocab))
        for a in sorted(articles, key=lambda a: a.id)
        if a.keyword
    ]
    for a in articles:
        if not a.keyword:
            warnings.append(f"article #{a.id} has no keyword; it cannot be checked for overlap")

    curated_by_norm = {normalize_keyword(c.candidate): c for c in rules.curated_merges}
    curated_by_equiv = {duplicate_key(c.candidate): c for c in rules.curated_merges}

    # ---- 入力の正規化と候補内の重複 (完全一致 + 日本語の空白差 + 列挙 acronym の分かち書き) ----
    # 表示テキストは書き換えない。代表は、検索ボリューム (あれば) -> 空白の少ない (プロジェクトの
    # 表記に近い) 正規化文字列 -> 文字列順で決め、入力順に依存しない。ただし列挙 acronym
    # (``c rm`` / ``cr m`` / ``crm``) は、正規綴り (``crm``) の候補があればボリュームに関係なく
    # それを代表にする。
    groups: dict[str, list[_Work]] = {}
    for candidate in sorted(candidates, key=lambda c: (normalize_keyword(c.keyword), c.keyword)):
        norm = normalize_keyword(candidate.keyword)
        if not norm:
            continue
        work = _Work(candidate, norm, intent_profile(candidate.keyword, vocab))
        groups.setdefault(duplicate_key(candidate.keyword), []).append(work)

    works: list[_Work] = []
    duplicates: list[_Work] = []
    candidate_by_equiv: dict[str, str] = {}
    for key, members in groups.items():
        if key in existing_by_equiv:
            # 既存 keyword と同一 (空白差を含む) の候補は、どれも「既存 keyword の重複」として
            # 個別に reject する (候補どうしの重複より、既存との重複を優先して報告する)。
            works.extend(members)
            continue
        is_acronym = key in CANONICAL_ACRONYMS
        rep_work = min(
            members,
            key=lambda w: (
                is_acronym and w.normalized != key,
                -_volume(w.candidate),
                w.normalized.count(" "),
                w.normalized,
                w.candidate.keyword,
            ),
        )
        works.append(rep_work)
        candidate_by_equiv[key] = rep_work.candidate.keyword
        for member in members:
            if member is rep_work:
                continue
            member.decision, member.reason_code = DECISION_REJECT, "duplicate_candidate"
            if member.normalized == rep_work.normalized:
                member.reason = (
                    "the same normalized keyword appears more than once in the candidates"
                )
            elif is_acronym:
                member.reason = (
                    f"split spelling of the acronym '{key}'; equivalent to candidate "
                    f"'{rep_work.candidate.keyword}'"
                )
            else:
                member.reason = (
                    "whitespace-insensitive equivalent (Japanese phrase) of candidate "
                    f"'{rep_work.candidate.keyword}'"
                )
            member.target, member.target_kind = rep_work.candidate.keyword, TARGET_CANDIDATE
            duplicates.append(member)
    works.sort(key=lambda w: (w.normalized, w.candidate.keyword))

    pending: list[_Work] = []
    for work in works:
        profile = work.profile
        # 1) reject
        exact_hit = existing_by_norm.get(work.normalized)
        hit = exact_hit or existing_by_equiv.get(duplicate_key(work.candidate.keyword))
        if hit is not None:
            work.decision, work.reason_code = DECISION_REJECT, "duplicate_existing_keyword"
            if exact_hit is not None:
                work.reason = f"identical to existing keyword '{hit.keyword}'"
            elif (acronym := canonical_acronym(work.candidate.keyword)) is not None:
                work.reason = (
                    f"split spelling of the acronym '{acronym}'; equivalent to existing keyword "
                    f"'{hit.keyword}'"
                )
            else:
                work.reason = (
                    "whitespace-insensitive equivalent (Japanese phrase) of existing keyword "
                    f"'{hit.keyword}'"
                )
            work.target, work.target_kind = hit.keyword, TARGET_KEYWORD
            continue
        rejected = next(
            (r for r in rules.reject_rules if _rule_matches(r, work.normalized, profile)), None
        )
        if rejected is not None:
            work.decision, work.reason_code = DECISION_REJECT, rejected.reason_code
            work.reason = rejected.note or f"matched reject rule '{rejected.id}'"
            work.matched_rule = rejected.id
            continue
        # 2) 既存 article
        article_hit = next(
            (
                (a, o)
                for a, ap in article_profiles
                if (o := compare_profiles(profile, ap)) is not None
            ),
            None,
        )
        if article_hit is not None:
            a, overlap = article_hit
            work.decision, work.reason_code = DECISION_MERGE, "overlaps_existing_article"
            work.reason = f"same SERP intent as article #{a.id} ({a.status}): {overlap.reason}"
            work.target, work.target_kind, work.target_article_id = a.keyword, TARGET_ARTICLE, a.id
            continue
        # 3) curated
        curated = curated_by_norm.get(work.normalized) or curated_by_equiv.get(
            duplicate_key(work.candidate.keyword)
        )
        if curated is not None:
            target_existing = existing_by_norm.get(
                normalize_keyword(curated.target)
            ) or existing_by_equiv.get(duplicate_key(curated.target))
            target_candidate = candidate_by_equiv.get(duplicate_key(curated.target))
            if target_existing is not None or target_candidate is not None:
                work.curated_target = curated.target
                work.decision, work.reason_code = DECISION_MERGE, "curated_merge"
                work.reason = curated.note or "curated semantic merge"
                if target_existing is not None:
                    _resolve_existing_target(work, target_existing, anchor_of, article_keyword)
                else:
                    work.target, work.target_kind = target_candidate, TARGET_CANDIDATE
                continue
        # 4) 既存 keyword
        existing_hit = next(
            (
                (k, o)
                for k in existing
                if (o := compare_profiles(profile, existing_profiles[k.id])) is not None
            ),
            None,
        )
        if existing_hit is not None:
            k, overlap = existing_hit
            work.decision, work.reason_code = DECISION_MERGE, "overlaps_existing_keyword"
            work.reason = f"same SERP intent as existing keyword '{k.keyword}': {overlap.reason}"
            _resolve_existing_target(work, k, anchor_of, article_keyword)
            continue
        pending.append(work)

    # 5) 候補どうし (anchor 方式)
    anchors: list[_Work] = []
    for work in sorted(pending, key=_work_sort_key):
        for anchor in anchors:
            overlap = compare_profiles(work.profile, anchor.profile)
            if overlap is not None:
                work.decision, work.reason_code = DECISION_MERGE, "intent_overlap_with_candidate"
                work.reason = (
                    f"same SERP intent as candidate '{anchor.candidate.keyword}': {overlap.reason}"
                )
                work.target, work.target_kind = anchor.candidate.keyword, TARGET_CANDIDATE
                anchor.absorbed += 1
                break
        else:
            anchors.append(work)

    # curated merge の対象が候補の場合は、その候補の最終的な行き先へ解決する
    by_text = {w.candidate.keyword: w for w in works}
    for work in works:
        _follow_candidate_chain(work, by_text)

    # ---- 出力 ----------------------------------------------------------------
    slot_profiles = [
        (e.keyword, existing_profiles[e.keyword_id])
        for e in queue.slots
        if e.keyword_id in existing_profiles
    ]
    keeps = [w for w in works if w.decision == DECISION_KEEP]
    decisions = [_freeze(w, keeps, slot_profiles) for w in [*works, *duplicates]]
    order = {DECISION_KEEP: 0, DECISION_MERGE: 1, DECISION_REJECT: 2}
    cluster_rank = {c.id: i for i, c in enumerate(config.clusters)}
    decisions.sort(
        key=lambda d: (
            order[d.decision],
            cluster_rank.get(d.cluster or "", 99),
            d.keyword,
        )
    )
    summary = {
        "candidates": len(candidates),
        "keep": sum(1 for d in decisions if d.decision == DECISION_KEEP),
        "merge": sum(1 for d in decisions if d.decision == DECISION_MERGE),
        "reject": sum(1 for d in decisions if d.decision == DECISION_REJECT),
        "metrics_available": sum(1 for d in decisions if d.metrics != NOT_AVAILABLE),
        "keep_by_cluster": _count_by_cluster(decisions, DECISION_KEEP),
    }
    return ExpansionPlan(tuple(decisions), tuple(warnings), summary)


def _count_by_cluster(decisions: Sequence[ExpansionDecision], kind: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in decisions:
        if d.decision == kind:
            key = d.cluster or "unassigned"
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _resolve_existing_target(
    work: _Work,
    keyword: KeywordInput,
    anchor_of: Mapping[int, tuple[str, str, int | None]],
    article_keyword: Mapping[int, str],
) -> None:
    kind, text, article_id = anchor_of.get(keyword.id, (TARGET_KEYWORD, keyword.keyword, None))
    work.target, work.target_kind, work.target_article_id = text, kind, article_id
    if kind == TARGET_ARTICLE and article_id is not None:
        work.reason_code = (
            "overlaps_existing_article" if work.reason_code != "curated_merge" else "curated_merge"
        )
    elif kind == TARGET_KEYWORD and keyword.id not in anchor_of:
        work.reason_code = (
            "overlaps_unassigned_keyword"
            if work.reason_code != "curated_merge"
            else "curated_merge"
        )


def _follow_candidate_chain(work: _Work, by_text: Mapping[str, _Work]) -> None:
    """merge の対象が (merge / reject 済みの) 候補なら、最終的な keep 候補まで辿る。"""

    depth = 0
    while (
        work.decision == DECISION_MERGE
        and work.target_kind == TARGET_CANDIDATE
        and work.target in by_text
        and depth < 5
    ):
        nxt = by_text[work.target]
        if nxt.decision == DECISION_KEEP:
            return
        if nxt.decision == DECISION_MERGE:
            work.target, work.target_kind = nxt.target, nxt.target_kind
            work.target_article_id = nxt.target_article_id
            depth += 1
            continue
        work.decision, work.reason_code = DECISION_REJECT, "merge_target_rejected"
        work.reason = f"its merge target '{nxt.candidate.keyword}' was rejected"
        work.target = work.target_kind = None
        return


def _freeze(
    work: _Work,
    keeps: Sequence[_Work],
    slot_profiles: Sequence[tuple[str, IntentProfile]],
) -> ExpansionDecision:
    profile = work.profile
    risk: str | None = None
    note = ""
    if work.decision == DECISION_KEEP:
        risk, note = _overlap_risk(work, keeps, slot_profiles)
    metrics: Mapping[str, Any] | str = (
        dict(work.candidate.metrics) if work.candidate.metrics else NOT_AVAILABLE
    )
    return ExpansionDecision(
        keyword=work.candidate.keyword,
        cluster=work.candidate.cluster,
        decision=work.decision,
        reason_code=work.reason_code,
        reason=work.reason,
        target=work.target,
        target_kind=work.target_kind,
        target_article_id=work.target_article_id,
        intent=profile.intent.value,
        serp_family=profile.family,
        overlap_risk=risk,
        overlap_note=note,
        affiliate=affiliate_coverage(work.candidate.affiliate_matches),
        metrics=metrics,
        matched_rule=work.matched_rule,
    )


def _overlap_risk(
    work: _Work,
    keeps: Sequence[_Work],
    slot_profiles: Sequence[tuple[str, IntentProfile]],
) -> tuple[str, str]:
    """keep 候補が他の slot / keep 候補と近すぎないか (high / medium / low)。"""

    profile = work.profile
    others = [(k.candidate.keyword, k.profile) for k in keeps if k is not work]
    worst, worst_with = 0.0, ""
    for text, other in [*others, *slot_profiles]:
        sim = near_overlap_similarity(profile, other) or 0.0
        if sim > worst:
            worst, worst_with = sim, text
    same_product = sorted(
        text
        for text, other in others
        if profile.product_specific and other.product_key == profile.product_key
    )
    bits: list[str] = []
    if worst >= 0.75:
        return "high", f"near-similar to '{worst_with}' ({worst:.2f})"
    if same_product:
        bits.append("same product as " + ", ".join(same_product))
    if work.absorbed:
        bits.append(f"absorbs {work.absorbed} variant(s)")
    if worst >= 0.6:
        bits.append(f"near '{worst_with}' ({worst:.2f})")
    if bits:
        return "medium", "; ".join(bits)
    return "low", ""
