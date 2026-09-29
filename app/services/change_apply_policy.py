"""適用の経路の方針 (C10-3、``app/config/change_apply_policy.json``)。どちらも既定は無効。"""

from __future__ import annotations

import json
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "change_apply_policy.json"


def load_change_apply_policy(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or POLICY_PATH).read_text(encoding="utf-8"))


def text_edit_apply_enabled(policy: dict | None = None) -> bool:
    return (policy or load_change_apply_policy()).get("text_edit_apply_enabled") is True


def meta_description_apply_enabled(policy: dict | None = None) -> bool:
    return (policy or load_change_apply_policy()).get("meta_description_apply_enabled") is True


__all__ = ["load_change_apply_policy", "meta_description_apply_enabled",
           "text_edit_apply_enabled"]
