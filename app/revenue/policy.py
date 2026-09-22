"""収益最適化エンジンの閾値ポリシー (C7)。

C6 の :mod:`app.seo.policy` と同じ方針: 閾値は **コードに埋めず**
``app/config/revenue_policy.json`` に置く。実データが貯まったあとにコードを読まずに
議論・改訂でき、出した推奨の根拠を ``policy_version`` で再現できるようにするため。

ここにあるのは **運用上のヒューリスティック** であり、業界の転換率ベンチマークでも
「何が売れるか」の主張でもない。重み付き合成スコア (「収益スコア 72/100」) は
作らない -- 名前の付いたゲートを個別に通すだけにする。

もっとも重要な設定は :meth:`trusted_measurement_start_at` -- 自分たちの計測用
リクエストが作ったクリックを、読者の行動として読まないための境界である。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "revenue_policy.json"

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITIES = (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)


@dataclass(frozen=True)
class RevenuePolicy:
    policy_version: str
    raw: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    def gate(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    @property
    def trusted_measurement_start_at(self) -> datetime | None:
        """これ以降のクリックだけを読者行動として扱う (未設定なら全期間を信頼)。"""

        raw = self.gate("trusted_click_measurement", "trusted_measurement_start_at")
        if not isinstance(raw, str) or not raw.strip():
            return None
        moment = datetime.fromisoformat(raw)
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)

    @property
    def trusted_measurement_rationale(self) -> list[str]:
        value = self.gate("trusted_click_measurement", "rationale")
        return [str(line) for line in value] if isinstance(value, list) else []

    @property
    def minimum_article_age_days(self) -> int:
        return int(self.gate("evaluation", "minimum_article_age_days", 14))

    @property
    def require_overlapping_windows(self) -> bool:
        return bool(self.gate("evaluation", "require_overlapping_windows", True))

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


def load_policy(path: Path | str | None = None) -> RevenuePolicy:
    target = Path(path) if path is not None else _POLICY_PATH
    document = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("revenue policy must be a JSON object")
    version = document.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("revenue policy must declare a policy_version")
    return RevenuePolicy(policy_version=version, raw=document)


@lru_cache(maxsize=1)
def get_policy() -> RevenuePolicy:
    return load_policy()
