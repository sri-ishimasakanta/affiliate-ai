"""2 つの報告の違い (``--compare <older.json>``。pure)。

毎回変わるだけの値 (作った時刻・観測の時刻・heartbeat の経過秒・スケジューラの前回 / 次回の
時刻など) は比べない。意味のある違いだけを出す: 事実の値・食い違い・不変条件の結果・警告・
次の行動・出どころの状態・時間で決まる状態、とその他の値の変化 (パス付き)。
"""

from __future__ import annotations

from collections.abc import Mapping

NOISE_KEYS = frozenset(
    {
        "generated_at",
        "observed_at",
        "recorded_at",
        "as_of",
        "heartbeat_at",
        "heartbeat_age_seconds",
        "last_run",
        "next_run",
        "next_expected_run",
        "check_after",
        "latest_observed_at",
        "latest_sync_at",
        "duration_seconds",
    }
)
NOISE_SECTIONS = frozenset({"source_freshness"})


def _has_id(value) -> bool:
    return isinstance(value, Mapping) and "id" in value


def _flatten(node, prefix="") -> dict:
    out = {}
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in NOISE_KEYS or (not prefix and key in NOISE_SECTIONS):
                continue
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else key))
    elif isinstance(node, list) and node and all(_has_id(v) for v in node):
        for value in node:
            out.update(_flatten(value, f"{prefix}[{value['id']}]"))
    else:
        out[prefix] = node
    return out


def _ids(report: Mapping, key: str) -> dict:
    items = report.get(key) or []
    if isinstance(items, Mapping):
        items = items.get("results") or []
    return {i["id"]: i for i in items}


def _by_id(old: Mapping, new: Mapping, key: str, field: str) -> dict:
    a, b = _ids(old, key), _ids(new, key)
    return {
        "added": sorted(set(b) - set(a)),
        "removed": sorted(set(a) - set(b)),
        "changed": {
            i: {"old": a[i].get(field), "new": b[i].get(field)}
            for i in sorted(set(a) & set(b))
            if a[i].get(field) != b[i].get(field)
        },
    }


PARTS = ("facts", "drift", "invariants", "warnings", "next_actions", "timing", "other_changes")


def _nonempty(part: Mapping) -> bool:
    if "added" in part:
        return bool(part["added"] or part["removed"] or part["changed"])
    return bool(part)


def compare(old: Mapping, new: Mapping) -> dict:
    facts_old, facts_new = old.get("facts") or {}, new.get("facts") or {}
    facts = {
        name: {"old": (facts_old.get(name) or {}).get("value"), "new": fact.get("value")}
        for name, fact in facts_new.items()
        if (facts_old.get(name) or {}).get("value") != fact.get("value")
    }
    flat_old, flat_new = _flatten(old), _flatten(new)
    other = {
        path: {"old": flat_old.get(path), "new": flat_new.get(path)}
        for path in sorted(set(flat_old) | set(flat_new))
        if flat_old.get(path) != flat_new.get(path)
        and not path.startswith(("facts.", "drift[", "invariants.", "warnings[", "next_actions["))
    }
    timing = {
        name: {
            "old": ((old.get("timing") or {}).get(name) or {}).get("state"),
            "new": ((new.get("timing") or {}).get(name) or {}).get("state"),
        }
        for name in ("daily", "weekly", "diagnostic")
    }
    diff = {
        "old_generated_at": old.get("generated_at"),
        "new_generated_at": new.get("generated_at"),
        "facts": facts,
        "drift": _by_id(old, new, "drift", "severity"),
        "invariants": _by_id(old, new, "invariants", "result"),
        "warnings": _by_id(old, new, "warnings", "severity"),
        "next_actions": _by_id(old, new, "next_actions", "priority"),
        "timing": {k: v for k, v in timing.items() if v["old"] != v["new"]},
        "other_changes": other,
    }
    diff["changed"] = any(_nonempty(diff[k]) for k in PARTS)
    return diff


def render(diff: Mapping) -> str:
    lines = [f"# Project state diff: {diff['old_generated_at']} → {diff['new_generated_at']}", ""]
    if not diff["changed"]:
        return "\n".join([*lines, "No meaningful change (timestamps ignored).", ""])
    for name, change in diff["facts"].items():
        lines.append(f"- fact {name}: {change['old']} → {change['new']}")
    for key in ("drift", "invariants", "warnings", "next_actions"):
        part = diff[key]
        lines += [f"- {key} added: {i}" for i in part["added"]]
        lines += [f"- {key} removed: {i}" for i in part["removed"]]
        lines += [f"- {key} {i}: {c['old']} → {c['new']}" for i, c in part["changed"].items()]
    for name, change in diff["timing"].items():
        lines.append(f"- timing {name}: {change['old']} → {change['new']}")
    for path, change in diff["other_changes"].items():
        lines.append(f"- {path}: {change['old']} → {change['new']}")
    return "\n".join([*lines, ""])
