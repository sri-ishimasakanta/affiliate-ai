"""Affiliate catalog の match_terms hygiene を宣言的に表す (pure・DB / HTTP 非依存)。

**なぜ必要か**: catalog (``affiliate_programs.match_terms``) の正本は production DB であり、
version 管理された source (CSV / JSON) は無い (CSV importer は insert 専用で既存行を更新しない)。
そこで「どの program から、どの term を、なぜ外すか」を version 管理された config
(``app/config/affiliate_catalog_hygiene.json``) に宣言し、PLAN / EXECUTE で適用する。matcher に
個別の特例を足して catalog の問題を隠すことはしない。

安全性 (この module が検証すること):

- 1 program につき変更は 1 件。``before`` は **現在の match_terms の完全な期待値** (順序・表記まで
  一致) で、適用前の compare-and-set に使う。``after = before - remove`` は導出値なので不整合に
  ならない。
- 外せるのは ``before`` にある term だけ。term の追加 / 書き換えは表現できない (削減専用)。
- **strong な term は外せない**: program 自身の名前の綴り (own-name) と明示 alias を ``remove``
  に含める config は読み込み時に拒否する (hygiene が strong match を失わせないための不変条件)。
- 変更は冪等: 現在値が ``after`` と一致すれば ``already_applied`` (何もしない)。``before`` でも
  ``after`` でもなければ ``drift`` (誰かが別途変更した)。適用しない。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.keyword.affiliate_tiers import default_tier_config, is_own_name_term

HYGIENE_VERSION = 1
DEFAULT_HYGIENE_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "affiliate_catalog_hygiene.json"
)

STATUS_PENDING = "pending"  # 現在値 == before: 適用できる
STATUS_APPLIED = "already_applied"  # 現在値 == after: 何もしない (冪等)
STATUS_DRIFT = "drift"  # before でも after でもない: 適用しない
STATUS_NOT_FOUND = "program_not_found"

_TOP_KEYS = frozenset({"version", "description", "changes"})
_CHANGE_KEYS = frozenset({"id", "program", "provider", "before", "remove", "reason", "evidence"})


class HygieneConfigError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class TermRemoval:
    """1 program に対する term 削除 (compare-and-set の単位)。"""

    id: str
    program: str
    provider: str | None
    before: tuple[str, ...]
    remove: tuple[str, ...]
    reason: str
    evidence: str

    @property
    def after(self) -> tuple[str, ...]:
        dropped = set(self.remove)
        return tuple(term for term in self.before if term not in dropped)


@dataclass(frozen=True)
class HygieneSpec:
    version: int
    changes: tuple[TermRemoval, ...]

    @property
    def removal_count(self) -> int:
        return sum(len(c.remove) for c in self.changes)


def _text_list(value: object) -> tuple[str, ...] | None:
    """空でない文字列だけのリスト (trim も書き換えもしない: 完全一致の比較に使うため)。"""

    if isinstance(value, list) and value and all(isinstance(v, str) and v != "" for v in value):
        return tuple(value)
    return None


def parse_hygiene_config(raw: object) -> HygieneSpec:
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise HygieneConfigError(["hygiene config must be a JSON object"])
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        errors.append(f"unknown top-level keys: {unknown}")
    if raw.get("version") != HYGIENE_VERSION or isinstance(raw.get("version"), bool):
        errors.append(f"version must be {HYGIENE_VERSION}")
    raw_changes = raw.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        errors.append("changes must be a non-empty list")
        raw_changes = []

    aliases = default_tier_config()
    changes: list[TermRemoval] = []
    seen_ids: set[str] = set()
    seen_programs: set[tuple[str, str | None]] = set()
    for index, item in enumerate(raw_changes):
        label = f"changes[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{label} must be an object")
            continue
        extra = sorted(set(item) - _CHANGE_KEYS)
        if extra:
            errors.append(f"{label} has unknown keys: {extra}")
        missing = sorted(_CHANGE_KEYS - set(item))
        if missing:
            errors.append(f"{label} is missing keys: {missing}")
            continue
        cid, program, provider = item["id"], item["program"], item["provider"]
        if not isinstance(cid, str) or not cid.strip():
            errors.append(f"{label}.id must be a non-empty string")
            continue
        label = f"change {cid!r}"
        if cid in seen_ids:
            errors.append(f"duplicate change id {cid!r}")
        seen_ids.add(cid)
        if not isinstance(program, str) or not program.strip():
            errors.append(f"{label}: program must be a non-empty string")
            continue
        if provider is not None and (not isinstance(provider, str) or not provider.strip()):
            errors.append(f"{label}: provider must be a non-empty string or null")
            continue
        if (program, provider) in seen_programs:
            errors.append(f"{label}: (program, provider) appears in more than one change")
        seen_programs.add((program, provider))
        before, remove = _text_list(item["before"]), _text_list(item["remove"])
        if before is None or len(set(before)) != len(before):
            errors.append(f"{label}: before must be a non-empty list of unique non-empty strings")
            continue
        if remove is None or len(set(remove)) != len(remove):
            errors.append(f"{label}: remove must be a non-empty list of unique non-empty strings")
            continue
        not_present = [t for t in remove if t not in before]
        if not_present:
            errors.append(f"{label}: remove terms are not in before: {not_present}")
            continue
        if len(remove) >= len(before):
            errors.append(f"{label}: a program must keep at least one match term")
        strong = [
            t for t in remove if is_own_name_term(program, t) or t in aliases.aliases_for(program)
        ]
        if strong:
            errors.append(f"{label}: cannot remove strong terms (own name / alias): {strong}")
        reason, evidence = item["reason"], item["evidence"]
        if not all(isinstance(v, str) and v.strip() for v in (reason, evidence)):
            errors.append(f"{label}: reason and evidence must be non-empty strings")
            continue
        changes.append(TermRemoval(cid, program, provider, before, remove, reason, evidence))

    if errors:
        raise HygieneConfigError(errors)
    return HygieneSpec(version=HYGIENE_VERSION, changes=tuple(changes))


def load_hygiene_config(path: str | Path = DEFAULT_HYGIENE_PATH) -> HygieneSpec:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HygieneConfigError([f"hygiene config not found: {path}"]) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HygieneConfigError([f"hygiene config is not readable JSON: {exc}"]) from exc
    return parse_hygiene_config(raw)


def evaluate_change(change: TermRemoval, current: Sequence[str] | None) -> str:
    """現在の match_terms (None = program が無い) に対する状態。完全一致だけを受け入れる。"""

    if current is None:
        return STATUS_NOT_FOUND
    current_terms = tuple(current)
    if current_terms == change.before:
        return STATUS_PENDING
    if current_terms == change.after:
        return STATUS_APPLIED
    return STATUS_DRIFT
