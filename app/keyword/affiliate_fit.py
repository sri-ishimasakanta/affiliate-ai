"""Affiliate match-term FIT (program/query fit) config (pure・決定論的)。

brand tier (``affiliate_tiers``: strong = 自身の名前 / 明示 alias、weak = それ以外) とは
**独立した** もう 1 つの軸。fit は「その program が、その term で探されている category /
主要 use-case に本当に属するか」を表す:

- ``core``      : program がその category / 主要 use-case の本命 (例: HubSpot × ``CRM``)
- ``loose``     : 広い goal / 周辺の use-case / 弱い話題上の関連 (例: 全 program × ``業務効率化``)
- ``unreviewed``: 監査の根拠が足りず判断していない (``loose`` と同じ扱い。core と決めつけない)

方針 (C2.5.7): scoring / primary の対象 = strong、または weak かつ core。
weak + loose / unreviewed は文脈・報告用の metadata に残るが score 0・primary 不可。
config に無い term は ``unreviewed``。

fit は **term ごと** (program × term) で持つ: 同じ ``文字起こし`` でも Fireflies では core、
Descript では loose。``kind: brand`` の term (own-name / alias) の適格性は brand tier で
決まり、fit に依存しない。
一般語の brand term (``Make`` / ``monday`` ...) が guard で weak になった場合だけ
``fit_when_weak`` を使う (未指定は ``unreviewed``)。データは ``app/config/affiliate_match_fit.json``
(version 管理、schema 変更なし)。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from app.keyword.affiliate_matching import normalize_for_match

FIT_CONFIG_VERSION = 1
DEFAULT_FIT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "affiliate_match_fit.json"
)

FIT_CORE = "core"
FIT_LOOSE = "loose"
FIT_UNREVIEWED = "unreviewed"
FIT_VALUES = frozenset({FIT_CORE, FIT_LOOSE, FIT_UNREVIEWED})

KIND_BRAND = "brand"
KIND_GENERIC = "generic"

_TOP_KEYS = frozenset({"version", "description", "source", "programs"})
_PROGRAM_KEYS = frozenset({"program", "provider", "terms"})
_TERM_KEYS = frozenset(
    {
        "term",
        "kind",
        "fit",
        "audit_fit",
        "evidence",
        "live_hits",
        "reason",
        "fit_when_weak",
        "fit_reason",
    }
)


class FitConfigError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class TermFit:
    """1 つの (program, term) の fit と、その根拠。"""

    fit: str  # core | loose | unreviewed
    reason: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class _Entry:
    kind: str
    fit: str | None  # generic の fit。brand は None
    fit_when_weak: str | None  # brand の weak fallback。generic は None
    reason: str
    fit_reason: str
    evidence: str


@dataclass(frozen=True)
class FitConfig:
    version: int = FIT_CONFIG_VERSION
    entries: Mapping[tuple[str, str], _Entry] = field(default_factory=dict)

    def fit_for(self, program_name: str, term: str) -> TermFit:
        """weak になった term の fit。config に無ければ ``unreviewed`` (core と決めつけない)。"""

        entry = self.entries.get((normalize_for_match(program_name), normalize_for_match(term)))
        if entry is None:
            return TermFit(FIT_UNREVIEWED, "term is not in the fit config (no audit evidence)")
        if entry.kind == KIND_BRAND:
            if entry.fit_when_weak is None:
                return TermFit(
                    FIT_UNREVIEWED,
                    "brand term used as a weak match; no fit decided",
                    entry.evidence,
                )
            return TermFit(entry.fit_when_weak, entry.fit_reason or entry.reason, entry.evidence)
        assert entry.fit is not None
        return TermFit(entry.fit, entry.reason, entry.evidence)

    def known_terms(self, program_name: str) -> tuple[str, ...]:
        key = normalize_for_match(program_name)
        return tuple(term for (name, term) in self.entries if name == key)

    def has_program(self, program_name: str) -> bool:
        """program 名 (正規化) が config にあるか。config は program 名で引く (schema 変更なし)。"""

        key = normalize_for_match(program_name)
        return any(name == key for (name, _term) in self.entries)

    def classifies(self, program_name: str, term: str) -> bool:
        """(program, term) が config に載っているか。載っていなければ fit は ``unreviewed``。"""

        return (normalize_for_match(program_name), normalize_for_match(term)) in self.entries


def aggregate_fit(fits: Iterable[str]) -> str | None:
    """複数 term の fit を 1 つに: 1 つでも core なら core。次に unreviewed、無ければ loose。"""

    seen = set(fits)
    if not seen:
        return None
    for value in (FIT_CORE, FIT_UNREVIEWED, FIT_LOOSE):
        if value in seen:
            return value
    return FIT_UNREVIEWED


def parse_fit_config(raw: object) -> FitConfig:
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise FitConfigError(["fit config must be a JSON object"])
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        errors.append(f"unknown top-level keys: {unknown}")
    if raw.get("version") != FIT_CONFIG_VERSION or isinstance(raw.get("version"), bool):
        errors.append(f"version must be {FIT_CONFIG_VERSION}")
    programs = raw.get("programs")
    if not isinstance(programs, list) or not programs:
        raise FitConfigError([*errors, "programs must be a non-empty list"])

    entries: dict[tuple[str, str], _Entry] = {}
    seen_programs: set[str] = set()
    for index, program in enumerate(programs):
        label = f"programs[{index}]"
        if not isinstance(program, Mapping):
            errors.append(f"{label} must be an object")
            continue
        extra = sorted(set(program) - _PROGRAM_KEYS)
        if extra:
            errors.append(f"{label} has unknown keys: {extra}")
        name = program.get("program")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{label}.program must be a non-empty string")
            continue
        label = f"programs[{name!r}]"
        pkey = normalize_for_match(name)
        if pkey in seen_programs:
            errors.append(f"{label}: duplicate program")
            continue
        seen_programs.add(pkey)
        terms = program.get("terms")
        if not isinstance(terms, list) or not terms:
            errors.append(f"{label}.terms must be a non-empty list")
            continue
        for term_index, item in enumerate(terms):
            tlabel = f"{label}.terms[{term_index}]"
            if not isinstance(item, Mapping):
                errors.append(f"{tlabel} must be an object")
                continue
            extra = sorted(set(item) - _TERM_KEYS)
            if extra:
                errors.append(f"{tlabel} has unknown keys: {extra}")
            term = item.get("term")
            if not isinstance(term, str) or not term.strip():
                errors.append(f"{tlabel}.term must be a non-empty string")
                continue
            tlabel = f"{label}.terms[{term!r}]"
            kind = item.get("kind", KIND_GENERIC)
            if kind not in (KIND_BRAND, KIND_GENERIC):
                errors.append(f"{tlabel}.kind must be 'brand' or 'generic'")
                continue
            reason = item.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{tlabel}.reason is required (a classification needs its evidence)")
                continue
            key = (pkey, normalize_for_match(term))
            if key in entries:
                errors.append(f"{tlabel}: duplicate term")
                continue
            evidence = item.get("evidence", "")
            if not isinstance(evidence, str):
                errors.append(f"{tlabel}.evidence must be a string")
                continue
            if kind == KIND_BRAND:
                if "fit" in item:
                    errors.append(f"{tlabel}: brand terms use the brand tier; set fit_when_weak")
                    continue
                weak = item.get("fit_when_weak")
                if weak is not None and weak not in FIT_VALUES:
                    errors.append(f"{tlabel}.fit_when_weak must be one of {sorted(FIT_VALUES)}")
                    continue
                entries[key] = _Entry(
                    KIND_BRAND,
                    None,
                    weak,
                    reason.strip(),
                    str(item.get("fit_reason", "")),
                    evidence,
                )
            else:
                fit = item.get("fit")
                if fit not in FIT_VALUES:
                    errors.append(f"{tlabel}.fit must be one of {sorted(FIT_VALUES)}")
                    continue
                if "fit_when_weak" in item:
                    errors.append(f"{tlabel}: fit_when_weak applies to brand terms only")
                    continue
                entries[key] = _Entry(KIND_GENERIC, fit, None, reason.strip(), "", evidence)
    if errors:
        raise FitConfigError(errors)
    return FitConfig(version=FIT_CONFIG_VERSION, entries=entries)


def load_fit_config(path: str | Path = DEFAULT_FIT_CONFIG_PATH) -> FitConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FitConfigError([f"fit config not found: {path}"]) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise FitConfigError([f"fit config is not readable JSON: {exc}"]) from exc
    return parse_fit_config(raw)


@cache
def default_fit_config() -> FitConfig:
    return load_fit_config(DEFAULT_FIT_CONFIG_PATH)
