"""Two-tier affiliate match semantics (pure・決定論的・追加的)。

既存の :func:`app.keyword.affiliate_matching.match_programs` を土台に、match した program を

- **strong**: program 自身の名前 (own-name) の綴り、または明示 alias による match
- **weak** : それ以外 (generic な category / feature / use-case の語) による match

に分類し、なぜその tier になったかを説明する。**scoring / queue 順序 / article 承認 / 展開判定は
一切変えない** (この module は報告用の追加メタデータを作るだけ)。legacy の「1 term でも match =
covered」は ``TieredMatch.legacy_matched`` として残る。

strong の導出 (schema 変更なし)
------------------------------
1. **own-name**: term の英数字 word のどれかが program 名の先頭 word と一致する
   (``Fireflies.ai`` -> ``fireflies``、``monday.com`` -> ``monday``、``n8n Cloud`` -> ``n8n``)。
   C2.5.2 の catalog 監査で、手作業の brand/alias ラベル 118 term と 118/118 一致した規則。
2. **explicit alias**: ``app/config/affiliate_match_tiers.json`` の ``aliases`` (program 名->綴り)。
   正規化では導けない綴り (``ハブスポット``、``make.com``) だけを置く。alias は match_terms に
   無くても strong になる (``legacy_matched=False``: legacy の match 集合は広げない)。

一般語 (ordinary word) の brand guard
------------------------------------
program 名の先頭 word が普通の英単語 (``make`` / ``monday`` / ``reclaim`` / ``fireflies``) の場合、
own-name の一致だけでは strong にしない。次のいずれかのときだけ strong:

- keyword が **brand 形** (``monday.com`` / ``monday com``、program 名が ``word.tld`` のとき) を含む
- keyword が **日本語の文脈** を含む (``Make 料金`` / ``monday タスク 管理``)。ただし
  ``deny_phrases`` (``make sense`` / ``make sure`` ...) を含む場合は除く
- **明示 alias** に一致する (``make.com``)

それ以外 (``make sense`` / ``make sure`` / 単独の ``make`` / 英語だけの ``make money``) は **weak**
(``ambiguity`` に理由を残す)。単独語の扱いは config の ``without_context`` で明示する。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from app.keyword.affiliate_matching import (
    MatchedProgram,
    ProgramFacts,
    match_programs,
    normalize_for_match,
    term_hit,
)
from app.keyword.equivalence import contains_japanese

TIER_CONFIG_VERSION = 1
DEFAULT_TIER_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "affiliate_match_tiers.json"
)

TIER_STRONG = "strong"
TIER_WEAK = "weak"

BASIS_OWN_NAME = "own_name"
BASIS_ALIAS = "explicit_alias"
BASIS_GENERIC = "generic_term"
BASIS_AMBIGUOUS_OWN_NAME = "ambiguous_own_name"

AMBIGUITY_BARE = "ambiguous_brand_bare"
AMBIGUITY_NO_CONTEXT = "ambiguous_brand_no_context"
AMBIGUITY_IDIOM = "ambiguous_brand_idiom"

_WITHOUT_CONTEXT_VALUES = frozenset({TIER_WEAK, TIER_STRONG})
_TOP_KEYS = frozenset(
    {"version", "description", "aliases", "ambiguous_brands", "reviewed_distinctive_brands"}
)
_AMBIGUOUS_KEYS = frozenset({"reason", "without_context", "deny_phrases"})
_WORD = re.compile(r"[a-z0-9]+")
_DOTTED_NAME = re.compile(r"^([a-z0-9]+)\.([a-z0-9]+)$")


class TierConfigError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


# ==================================================================== config
@dataclass(frozen=True)
class AmbiguousBrand:
    token: str
    reason: str
    without_context: str  # 文脈の無い単独語 / 英語だけの use の扱い: weak | strong
    deny_phrases: tuple[str, ...] = ()


@dataclass(frozen=True)
class TierConfig:
    version: int = TIER_CONFIG_VERSION
    aliases: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    ambiguous: Mapping[str, AmbiguousBrand] = field(default_factory=dict)
    reviewed: frozenset[str] = frozenset()

    def aliases_for(self, program_name: str) -> tuple[str, ...]:
        return self.aliases.get(normalize_for_match(program_name), ())


def _str_list(value: object) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value):
        return tuple(v.strip() for v in value)
    return None


def parse_tier_config(raw: object) -> TierConfig:
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise TierConfigError(["tier config must be a JSON object"])
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        errors.append(f"unknown top-level keys: {unknown}")
    if raw.get("version") != TIER_CONFIG_VERSION or isinstance(raw.get("version"), bool):
        errors.append(f"version must be {TIER_CONFIG_VERSION}")

    aliases: dict[str, tuple[str, ...]] = {}
    raw_aliases = raw.get("aliases", {})
    if not isinstance(raw_aliases, Mapping):
        errors.append("aliases must be an object")
        raw_aliases = {}
    for name, values in raw_aliases.items():
        listed = _str_list(values)
        if not isinstance(name, str) or not name.strip() or not listed:
            errors.append(f"aliases[{name!r}] must be a non-empty list of non-empty strings")
            continue
        key = normalize_for_match(name)
        if key in aliases:
            errors.append(f"duplicate alias program name {name!r}")
        aliases[key] = tuple(dict.fromkeys(listed))

    ambiguous: dict[str, AmbiguousBrand] = {}
    raw_ambiguous = raw.get("ambiguous_brands", {})
    if not isinstance(raw_ambiguous, Mapping):
        errors.append("ambiguous_brands must be an object")
        raw_ambiguous = {}
    for token, item in raw_ambiguous.items():
        label = f"ambiguous_brands[{token!r}]"
        if not isinstance(token, str) or _WORD.fullmatch(token) is None:
            errors.append(f"{label}: token must be a lowercase alphanumeric word")
            continue
        if not isinstance(item, Mapping):
            errors.append(f"{label} must be an object")
            continue
        extra = sorted(set(item) - _AMBIGUOUS_KEYS)
        if extra:
            errors.append(f"{label} has unknown keys: {extra}")
        reason = item.get("reason")
        without_context = item.get("without_context")
        deny = _str_list(item.get("deny_phrases"))
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{label}.reason must be a non-empty string")
            continue
        if without_context not in _WITHOUT_CONTEXT_VALUES:
            errors.append(
                f"{label}.without_context must be one of {sorted(_WITHOUT_CONTEXT_VALUES)}"
            )
            continue
        if deny is None:
            errors.append(f"{label}.deny_phrases must be a list of non-empty strings")
            continue
        ambiguous[token] = AmbiguousBrand(
            token=token,
            reason=reason.strip(),
            without_context=without_context,
            deny_phrases=deny,
        )

    reviewed_raw = _str_list(raw.get("reviewed_distinctive_brands"))
    if reviewed_raw is None:
        errors.append("reviewed_distinctive_brands must be a list of non-empty strings")
        reviewed_raw = ()
    overlap = sorted(set(reviewed_raw) & set(ambiguous))
    if overlap:
        errors.append(f"tokens cannot be both ambiguous and reviewed-distinctive: {overlap}")

    if errors:
        raise TierConfigError(errors)
    return TierConfig(
        version=TIER_CONFIG_VERSION,
        aliases=aliases,
        ambiguous=ambiguous,
        reviewed=frozenset(reviewed_raw),
    )


def load_tier_config(path: str | Path = DEFAULT_TIER_CONFIG_PATH) -> TierConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TierConfigError([f"tier config not found: {path}"]) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TierConfigError([f"tier config is not readable JSON: {exc}"]) from exc
    return parse_tier_config(raw)


@cache
def default_tier_config() -> TierConfig:
    return load_tier_config(DEFAULT_TIER_CONFIG_PATH)


# ==================================================================== results
@dataclass(frozen=True)
class TermTier:
    """1 つの term がどの tier で、なぜそうなったか。"""

    term: str
    tier: str  # strong | weak
    basis: str  # own_name | explicit_alias | generic_term | ambiguous_own_name
    note: str = ""


@dataclass(frozen=True)
class TieredMatch:
    """tier 付きの program match。``program`` は legacy の MatchedProgram をそのまま持つ。"""

    program: MatchedProgram
    tier: str  # strong | weak
    term_tiers: tuple[TermTier, ...]
    reason: str
    ambiguity: str | None
    # legacy の match_programs でも match したか。False = alias だけで strong になった program
    # (legacy の「covered」集合は広げない)。
    legacy_matched: bool

    @property
    def program_id(self) -> int:
        return self.program.program_id

    @property
    def name(self) -> str:
        return self.program.name

    @property
    def provider(self) -> str | None:
        return self.program.provider

    @property
    def matched_terms(self) -> tuple[str, ...]:
        return self.program.matched_terms

    @property
    def strong_terms(self) -> tuple[str, ...]:
        return tuple(t.term for t in self.term_tiers if t.tier == TIER_STRONG)

    @property
    def weak_terms(self) -> tuple[str, ...]:
        return tuple(t.term for t in self.term_tiers if t.tier == TIER_WEAK)


@dataclass(frozen=True)
class TierSummary:
    strong_count: int
    weak_count: int
    strong_names: tuple[str, ...]
    weak_names: tuple[str, ...]
    no_strong_affiliate_match: bool


def summarize_tiers(matches: Iterable[TieredMatch]) -> TierSummary:
    ordered = sorted(matches, key=lambda m: (m.name, m.program_id))
    strong = tuple(m.name for m in ordered if m.tier == TIER_STRONG)
    weak = tuple(m.name for m in ordered if m.tier == TIER_WEAK)
    return TierSummary(len(strong), len(weak), strong, weak, not strong)


# ==================================================================== derivation
def name_words(text: str) -> tuple[str, ...]:
    """正規化した text の英数字 word 列 (日本語や記号は区切り)。"""

    return tuple(_WORD.findall(normalize_for_match(text)))


def own_name_token(program_name: str) -> str:
    words = name_words(program_name)
    return words[0] if words else ""


def is_own_name_term(program_name: str, term: str) -> bool:
    """term が program 自身の名前の綴りか (先頭 word が term の word のどれかと一致)。"""

    token = own_name_token(program_name)
    return bool(token) and token in name_words(term)


def branded_forms(program_name: str) -> tuple[str, ...]:
    """program 名が ``word.tld`` のときの brand 形 (``monday.com`` / ``monday com``)。"""

    match = _DOTTED_NAME.match(normalize_for_match(program_name))
    if match is None:
        return ()
    word, tld = match.groups()
    return (f"{word}.{tld}", f"{word} {tld}")


def unreviewed_brand_tokens(
    programs: Iterable[ProgramFacts], config: TierConfig | None = None
) -> list[tuple[str, str]]:
    """own-name token が「一般語として guard 済み」でも「distinctive と確認済み」でもない program。

    catalog に新しい program が入ったとき、own-name の規則が安全か人が確認する hook。
    """

    cfg = config or default_tier_config()
    out: list[tuple[str, str]] = []
    for program in programs:
        token = own_name_token(program.name)
        if token and token not in cfg.ambiguous and token not in cfg.reviewed:
            out.append((program.name, token))
    return out


def _ambiguity_verdict(
    *,
    keyword: str,
    program_name: str,
    amb: AmbiguousBrand,
    ignore_japanese_spacing: bool,
) -> tuple[bool, str, str | None]:
    """own-name term が一般語の brand のとき strong にしてよいか: (accepted, note, ambiguity)。"""

    padded = f" {keyword} "
    for phrase in amb.deny_phrases:
        if f" {normalize_for_match(phrase)} " in padded:
            return (
                False,
                f"'{amb.token}' is used in the non-brand phrase '{phrase}'",
                AMBIGUITY_IDIOM,
            )
    for form in branded_forms(program_name):
        if term_hit(form, keyword, ignore_japanese_spacing=ignore_japanese_spacing):
            return True, f"accepted: brand form '{form}'", None
    if contains_japanese(keyword):
        return True, "accepted: Japanese-language query context", None
    bare = name_words(keyword) == (amb.token,)
    ambiguity = AMBIGUITY_BARE if bare else AMBIGUITY_NO_CONTEXT
    detail = "bare token" if bare else "English-only use without a brand form"
    accepted = amb.without_context == TIER_STRONG
    verdict = "accepted by config" if accepted else "not accepted as strong"
    return accepted, f"{detail}; '{amb.token}' is {amb.reason} ({verdict})", ambiguity


def _classify_program(
    *,
    keyword: str,
    facts: ProgramFacts,
    legacy: MatchedProgram | None,
    config: TierConfig,
    ignore_japanese_spacing: bool,
) -> TieredMatch | None:
    term_tiers: list[TermTier] = []
    ambiguity: str | None = None
    seen: set[str] = set()

    for term in legacy.matched_terms if legacy is not None else ():
        seen.add(normalize_for_match(term))
        if not is_own_name_term(facts.name, term):
            term_tiers.append(
                TermTier(term, TIER_WEAK, BASIS_GENERIC, "generic category/feature/use-case term")
            )
            continue
        amb = config.ambiguous.get(own_name_token(facts.name))
        if amb is None:
            term_tiers.append(
                TermTier(term, TIER_STRONG, BASIS_OWN_NAME, "spelling of the program's own name")
            )
            continue
        accepted, note, term_ambiguity = _ambiguity_verdict(
            keyword=keyword,
            program_name=facts.name,
            amb=amb,
            ignore_japanese_spacing=ignore_japanese_spacing,
        )
        if accepted:
            term_tiers.append(TermTier(term, TIER_STRONG, BASIS_OWN_NAME, note))
        else:
            ambiguity = ambiguity or term_ambiguity
            term_tiers.append(TermTier(term, TIER_WEAK, BASIS_AMBIGUOUS_OWN_NAME, note))

    for alias in config.aliases_for(facts.name):
        normalized_alias = normalize_for_match(alias)
        if normalized_alias in seen:
            continue
        if term_hit(normalized_alias, keyword, ignore_japanese_spacing=ignore_japanese_spacing):
            seen.add(normalized_alias)
            term_tiers.append(
                TermTier(alias, TIER_STRONG, BASIS_ALIAS, "explicit alias of the program")
            )

    if not term_tiers:
        return None

    strong = [t for t in term_tiers if t.tier == TIER_STRONG]
    tier = TIER_STRONG if strong else TIER_WEAK
    if strong:
        reason = "strong: " + "; ".join(f"{t.basis} '{t.term}'" for t in strong)
        ambiguity = None
    else:
        ambiguous_hits = [t for t in term_tiers if t.basis == BASIS_AMBIGUOUS_OWN_NAME]
        if ambiguous_hits:
            reason = "weak: own-name term not accepted as strong: " + "; ".join(
                f"'{t.term}' ({t.note})" for t in ambiguous_hits
            )
        else:
            reason = "weak: generic term(s) only: " + ", ".join(f"'{t.term}'" for t in term_tiers)

    program = legacy
    if program is None:
        # alias だけで strong になった program (legacy の match には含まれない)
        program = MatchedProgram(
            program_id=facts.program_id,
            name=facts.name,
            provider=facts.provider,
            category=facts.category,
            matched_terms=tuple(t.term for t in term_tiers),
            commission_type=facts.commission_type,
            commission_value=facts.commission_value,
            currency=facts.currency,
        )
    return TieredMatch(
        program=program,
        tier=tier,
        term_tiers=tuple(term_tiers),
        reason=reason,
        ambiguity=ambiguity,
        legacy_matched=legacy is not None,
    )


def match_programs_tiered(
    keyword: str,
    programs: Sequence[ProgramFacts],
    *,
    config: TierConfig | None = None,
    ignore_japanese_spacing: bool = False,
) -> list[TieredMatch]:
    """:func:`match_programs` の結果に strong / weak の tier を付けて返す。

    ``[m.program for m in result if m.legacy_matched]`` は ``match_programs`` の結果と (順序も含め)
    完全に一致する。alias だけで strong になった program は ``legacy_matched=False`` で catalog 順に
    加わる (legacy の covered 集合は広げない)。``ignore_japanese_spacing`` は legacy と同じ opt-in。
    """

    cfg = config or default_tier_config()
    normalized_keyword = normalize_for_match(keyword)
    legacy_by_id = {
        m.program_id: m
        for m in match_programs(keyword, programs, ignore_japanese_spacing=ignore_japanese_spacing)
    }
    out: list[TieredMatch] = []
    for facts in programs:
        tiered = _classify_program(
            keyword=normalized_keyword,
            facts=facts,
            legacy=legacy_by_id.get(facts.program_id),
            config=cfg,
            ignore_japanese_spacing=ignore_japanese_spacing,
        )
        if tiered is not None:
            out.append(tiered)
    return out
