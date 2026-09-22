"""運用ポリシー (C8)。

閾値・ウィンドウ・通知の条件を ``app/config/operations_policy.json`` に置く。
C6/C7 と同じ理由: 運用の判断基準をコードから切り離し、``policy_version`` で
「その日なぜそう動いたか」を後から再現できるようにするため。

ここにある lag / staleness は **こちらの運用上の許容値** であって、provider が
データ到着を保証した値ではない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "operations_policy.json"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITIES = (SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_ERROR)
#: 重大度の順序 (通知閾値の比較に使う)。
SEVERITY_ORDER = {SEVERITY_INFO: 0, SEVERITY_WARNING: 1, SEVERITY_ERROR: 2}

PRIORITY_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class OperationsPolicy:
    policy_version: str
    raw: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    def gate(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    def import_config(self, source: str) -> dict[str, Any]:
        value = self.section("imports").get(source)
        return value if isinstance(value, dict) else {}

    @property
    def timezone_name(self) -> str:
        value = self.raw.get("timezone")
        return value if isinstance(value, str) and value else "UTC"

    @property
    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except Exception:  # noqa: BLE001 - 不正な tz 名は UTC にフォールバック
            return ZoneInfo("UTC")

    def severity_for(self, alert_type: str) -> str:
        value = self.section("severity").get(alert_type)
        return value if value in SEVERITIES else SEVERITY_WARNING

    @property
    def notify_min_severity(self) -> str:
        value = self.gate("alerts", "notify_min_severity", SEVERITY_WARNING)
        return value if value in SEVERITIES else SEVERITY_WARNING

    @property
    def cooldown_hours(self) -> int:
        return int(self.gate("alerts", "cooldown_hours", 24))

    @property
    def stale_lock_after_minutes(self) -> int:
        return int(self.gate("locking", "stale_lock_after_minutes", 120))


def load_policy(path: Path | str | None = None) -> OperationsPolicy:
    target = Path(path) if path is not None else _POLICY_PATH
    document = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("operations policy must be a JSON object")
    version = document.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("operations policy must declare a policy_version")
    return OperationsPolicy(policy_version=version, raw=document)


@lru_cache(maxsize=1)
def get_policy() -> OperationsPolicy:
    return load_policy()
