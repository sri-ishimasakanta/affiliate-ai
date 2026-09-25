"""週ごとの決定の記録 (``docs/decision-log/YYYY-Www.md``。git で追う、長く残す記録)。

- 1 週 1 ファイル (ISO 週、JST)。例: ``docs/decision-log/2026-W39.md``。
- 残すのは長く効く決定と出来事だけ (コマンドの記録ではない)。
- 各項目は ``<!-- decision:<id> -->`` の印を持つ。**すべての週のファイル** にその印が既に
  あれば書かない (同じ決定を 2 度書かない。週をまたいでも)。
- 外部には何も書かない (手元のファイルだけ)。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
LOG_DIR = Path("docs") / "decision-log"
_MARK = re.compile(r"<!-- decision:([A-Za-z0-9_.\-]+) -->")
FIELDS = ("area", "decision", "rationale", "evidence", "resulting_state", "follow_up")


def week_key(moment: datetime) -> str:
    year, week, _ = moment.astimezone(JST).isocalendar()
    return f"{year}-W{week:02d}"


def log_path(root: Path, moment: datetime) -> Path:
    return root / LOG_DIR / f"{week_key(moment)}.md"


def existing_ids(root: Path) -> set[str]:
    directory = root / LOG_DIR
    if not directory.is_dir():
        return set()
    found: set[str] = set()
    for path in sorted(directory.glob("*.md")):
        found.update(_MARK.findall(path.read_text(encoding="utf-8")))
    return found


def render_entry(entry: Mapping, *, moment: datetime) -> str:
    stamp = moment.astimezone(JST).isoformat(timespec="minutes")
    evidence = entry.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    lines = [
        f"<!-- decision:{entry['id']} -->",
        f"### {entry['decision']}",
        "",
        f"- 記録: {stamp} (JST)",
        f"- 領域: {entry['area']}",
        f"- 理由: {entry['rationale']}",
        f"- 根拠: {', '.join(f'`{e}`' for e in evidence) or '—'}",
        f"- 結果の状態: {entry['resulting_state']}",
        f"- 次にすること: {entry.get('follow_up') or '—'}",
        "",
    ]
    return "\n".join(lines)


def append_entries(root: Path, entries: Iterable[Mapping], *, moment: datetime) -> list[str]:
    """まだどの週にも無い項目だけを、今の週のファイルに足す。足した ID を返す。"""

    known = existing_ids(root)
    fresh = []
    for entry in entries:
        missing = [f for f in ("id", *FIELDS[:3], "resulting_state") if not entry.get(f)]
        if missing:
            raise ValueError(f"decision {entry.get('id')!r} is missing {missing}")
        if entry["id"] in known or entry["id"] in {e["id"] for e in fresh}:
            continue
        fresh.append(entry)
    if not fresh:
        return []
    path = log_path(root, moment)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text = path.read_text(encoding="utf-8").rstrip("\n") + "\n\n"
    else:
        text = (
            f"# 決定の記録 {week_key(moment)}\n\n"
            "長く効く決定と出来事だけを残す (`scripts/generate_project_state.py` が足す。"
            "同じ決定は 2 度書かない)。\n\n"
        )
    text += "\n".join(render_entry(e, moment=moment) for e in fresh)
    path.write_bytes(text.encode("utf-8"))
    return [e["id"] for e in fresh]
