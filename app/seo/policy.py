"""SEO 改善候補エンジンの閾値ポリシー (C6)。

閾値は **コードに埋めず** ``app/config/seo_policy.json`` に置く。理由は 2 つ:

1. 実データが貯まったあとに、コードを読まずに議論・改訂できるようにするため。
2. 出した推奨の根拠を ``policy_version`` で後から再現できるようにするため。

このポリシーは **運用上のヒューリスティック** であって、Google のランキング
アルゴリズムに関する主張ではない。重み付き合成スコアは作らない -- 名前の付いた
ゲートを個別に通すだけにする。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "seo_policy.json"

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITIES = (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)


@dataclass(frozen=True)
class SeoPolicy:
    """読み込み済みポリシー。値は読み取り専用として扱う。"""

    policy_version: str
    raw: dict[str, Any]

    # -- section accessors ---------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    def gate(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    # -- よく使うゲート (読みやすさのための薄いラッパ) -----------------------
    @property
    def minimum_article_age_days(self) -> int:
        return int(self.gate("evaluation", "minimum_article_age_days", 14))

    @property
    def search_data_lag_days(self) -> int:
        return int(self.gate("evaluation", "search_data_lag_days", 3))

    @property
    def inspection_max_age_days(self) -> int:
        return int(self.gate("evaluation", "inspection_max_age_days", 7))

    def freshness_days_for(self, article_type: str | None) -> int:
        by_type = self.section("freshness").get("by_article_type") or {}
        if article_type and article_type in by_type:
            return int(by_type[article_type])
        return int(self.gate("freshness", "default_days", 180))

    def base_priority(self, candidate_type: str) -> str:
        value = self.section("priority").get(candidate_type)
        return value if value in PRIORITIES else PRIORITY_LOW

    def escalations_for(self, candidate_type: str) -> list[dict[str, Any]]:
        rules = self.raw.get("priority_escalations")
        if not isinstance(rules, list):
            return []
        return [
            rule
            for rule in rules
            if isinstance(rule, dict) and rule.get("candidate_type") == candidate_type
        ]


def load_policy(path: Path | str | None = None) -> SeoPolicy:
    """ポリシーを読み込む (テストからは任意のパスを渡せる)。"""

    target = Path(path) if path is not None else _POLICY_PATH
    document = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("seo policy must be a JSON object")
    version = document.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("seo policy must declare a policy_version")
    return SeoPolicy(policy_version=version, raw=document)


@lru_cache(maxsize=1)
def get_policy() -> SeoPolicy:
    """既定のポリシー (プロセス内でキャッシュする)。"""

    return load_policy()
