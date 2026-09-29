"""Growth Action の方針 (``app/config/growth_action_policy.json``、git で管理する)。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "growth_action_policy.json"


@dataclass(frozen=True)
class GrowthActionPolicy:
    policy_version: str
    raw: dict[str, Any]

    @property
    def digest(self) -> dict[str, Any]:
        value = self.raw.get("digest")
        return value if isinstance(value, dict) else {}

    @property
    def sending_enabled(self) -> bool:
        """本物のメールで送ってよいか。**既定は false** (最初の本番の送信は人の判断)。"""

        return self.digest.get("sending_enabled") is True

    @property
    def max_items(self) -> int:
        return max(1, min(int(self.digest.get("max_items", 5)), 5))

    @property
    def cadence_days(self) -> int:
        return max(1, int(self.digest.get("cadence_days", 7)))

    @property
    def respect_window(self) -> bool:
        return self.digest.get("respect_approval_notification_window", True) is not False


def load_policy(path: Path | None = None) -> GrowthActionPolicy:
    raw = json.loads((path or POLICY_PATH).read_text(encoding="utf-8"))
    return GrowthActionPolicy(policy_version=str(raw.get("policy_version", "")), raw=raw)


__all__ = ["POLICY_PATH", "GrowthActionPolicy", "load_policy"]
