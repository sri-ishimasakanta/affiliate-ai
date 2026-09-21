"""keyword-idea discovery 用の cluster ごとの seed 定義 (pure)。

1 cluster = 1 seed set = 最大 1 回の keyword-idea request。seed は追跡ファイル
(``app/config/keyword_idea_seeds.json``) で管理し、ここで構造検証する (DB / network なし)。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.keyword.normalizers.site_relevance import normalize_keyword

CONFIG_VERSION = 1
#: discovery 対象 cluster と実行順 (C2.2 の承認済み優先順 + 新規候補 E)。
TARGET_CLUSTERS: tuple[str, ...] = ("B", "C", "A", "D", "E")
MAX_SEEDS_PER_SET = 20
MAX_RESULTS_LIMIT = 1000

_TOP_KEYS = frozenset({"version", "default_max_results", "clusters"})
_CLUSTER_KEYS = frozenset({"name", "seeds"})


class IdeaSeedConfigError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class IdeaSeedSet:
    cluster_id: str
    name: str
    seeds: tuple[str, ...]


@dataclass(frozen=True)
class IdeaSeedConfig:
    version: int
    default_max_results: int
    sets: tuple[IdeaSeedSet, ...]

    def select(self, cluster_ids: Sequence[str] | None) -> tuple[IdeaSeedSet, ...]:
        """指定 cluster (未指定なら全 cluster) の seed set を定義順で返す。"""

        if not cluster_ids:
            return self.sets
        wanted = {c.strip().upper() for c in cluster_ids}
        unknown = sorted(wanted - {s.cluster_id for s in self.sets})
        if unknown:
            raise IdeaSeedConfigError([f"unknown cluster(s): {unknown}"])
        return tuple(s for s in self.sets if s.cluster_id in wanted)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_idea_seed_config(raw: object) -> IdeaSeedConfig:
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise IdeaSeedConfigError(["seed config must be a JSON object"])
    unknown_top = sorted(set(raw) - _TOP_KEYS)
    if unknown_top:
        errors.append(f"unknown top-level keys: {unknown_top}")
    if raw.get("version") != CONFIG_VERSION or not _is_int(raw.get("version")):
        errors.append(f"version must be {CONFIG_VERSION}")
    default_max = raw.get("default_max_results")
    if not _is_int(default_max) or not 1 <= default_max <= MAX_RESULTS_LIMIT:
        errors.append(f"default_max_results must be an integer between 1 and {MAX_RESULTS_LIMIT}")
        default_max = 0

    clusters = raw.get("clusters")
    sets: list[IdeaSeedSet] = []
    if not isinstance(clusters, Mapping) or not clusters:
        errors.append("clusters must be a non-empty object")
        clusters = {}
    for cid in clusters:
        if cid not in TARGET_CLUSTERS:
            errors.append(f"unknown cluster id {cid!r} (allowed: {list(TARGET_CLUSTERS)})")
    for cid in TARGET_CLUSTERS:
        if cid not in clusters:
            continue
        body = clusters[cid]
        label = f"cluster {cid!r}"
        if not isinstance(body, Mapping):
            errors.append(f"{label} must be an object")
            continue
        unknown = sorted(set(body) - _CLUSTER_KEYS)
        if unknown:
            errors.append(f"{label} has unknown keys: {unknown}")
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{label}: name must be a non-empty string")
        seeds = body.get("seeds")
        if not isinstance(seeds, list) or not seeds:
            errors.append(f"{label}: seeds must be a non-empty list")
            continue
        if not all(isinstance(s, str) and s.strip() for s in seeds):
            errors.append(f"{label}: every seed must be a non-empty string")
            continue
        if len(seeds) > MAX_SEEDS_PER_SET:
            errors.append(f"{label}: at most {MAX_SEEDS_PER_SET} seeds are allowed")
        normalized = [normalize_keyword(s) for s in seeds]
        if len(set(normalized)) != len(normalized):
            errors.append(f"{label}: duplicate seeds")
        sets.append(
            IdeaSeedSet(
                cluster_id=cid,
                name=str(name).strip() if isinstance(name, str) else "",
                seeds=tuple(s.strip() for s in seeds),
            )
        )
    if errors:
        raise IdeaSeedConfigError(errors)
    return IdeaSeedConfig(
        version=CONFIG_VERSION, default_max_results=int(default_max), sets=tuple(sets)
    )


def load_idea_seed_config(path: str | Path) -> IdeaSeedConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise IdeaSeedConfigError([f"seed config not found: {path}"]) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise IdeaSeedConfigError([f"seed config is not readable JSON: {exc}"]) from exc
    return parse_idea_seed_config(raw)
