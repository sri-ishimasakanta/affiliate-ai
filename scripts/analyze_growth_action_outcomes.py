"""変換した Growth Action の追跡の観測 (C9 Batch 3 / C9-C)。**読むだけ。**

    uv run python scripts/analyze_growth_action_outcomes.py list
    uv run python scripts/analyze_growth_action_outcomes.py list --due
    uv run python scripts/analyze_growth_action_outcomes.py summary
    uv run python scripts/analyze_growth_action_outcomes.py show <growth_action_id>
    uv run python scripts/analyze_growth_action_outcomes.py show <growth_action_id> --checkpoint 7d
    uv run python scripts/analyze_growth_action_outcomes.py list --format json

変更の依頼の適用・Threads の公開・記事の公開 (実際に外に見える状態が変わった時刻) から、
チェックポイント (24h / 72h / 7d / 14d / 28d) ごとに、出所 (GSC / GA4 / 信頼できるクリック /
Threads の観測) が届いた範囲だけで、変わる前と後に観測したものを並べる。届いていない出所は 0 に
しない。因果を言わない・1 つの成功の点数を作らない・勝ち負けを決めない。
DB に書かない・外に問い合わせない (worker も同じ観測をメモリだけで持つ)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.growth import measurement as gm  # noqa: E402
from app.growth.outcome import CHECKPOINTS  # noqa: E402
from app.services.growth_measurement_service import (  # noqa: E402
    GrowthMeasurementService,
    measurement_ready,
    summarize,
)


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("list", "show", "summary"))
    parser.add_argument("growth_action_id", type=int, nargs="?")
    parser.add_argument("--checkpoint", choices=[c[0] for c in CHECKPOINTS])
    parser.add_argument("--due", action="store_true",
                        help="次の観測の時刻が来たもの (効果が始まったものだけ)")
    parser.add_argument("--as-of", dest="as_of")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    if args.command == "show" and args.growth_action_id is None:
        parser.error("show needs a growth_action_id")
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    with session_factory() as session:
        ready = measurement_ready(session)
        service = GrowthMeasurementService(session, settings=settings)
        items = service.anchors() if ready else []
        if args.command == "show":
            items = [i for i in items if i["anchor"]["growth_action_id"] == args.growth_action_id]
        measured = [service.measure(i, now=now, checkpoint=args.checkpoint) for i in items]
        session.rollback()
    if args.due:
        # 期日: 効果が始まり、まだ終わっていないチェックポイントの時刻が来たもの。
        measured = [m for m in measured if m.lifecycle.effective_at and any(
            c["state"] not in gm.FINAL_STATES and c.get("due_at")
            and datetime.fromisoformat(c["due_at"]) <= now for c in m.checkpoints)]
    payload = {"conversion_table_ready": ready, "as_of": now.isoformat(),
               "summary": summarize(measured, now=now),
               "outcomes": [m.as_dict() for m in measured]}  # fmt: skip
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif args.command == "summary":
        print(render_summary(payload))
    else:
        print(render(payload))
    print("read-only: database writes = 0, external calls = 0")
    return 0


def render_summary(payload: dict) -> str:
    s = payload["summary"]
    lines = [f"Growth Action follow-up — as of {payload['as_of']}",
             f"anchors {s['anchors']} (effective {s['effective']}); by state "
             + (", ".join(f"{k}={v}" for k, v in s["by_state"].items()) or "none"),
             "waiting:"]
    lines += [f"  - {k} ({v})" for k, v in s["waiting"].items()] or ["  (nothing)"]
    lines.append("completed:")
    lines += [f"  - {k} ({v})" for k, v in s["completed"].items()] or ["  (nothing)"]
    lines.append(f"due now: {s['due_now'] or 'none'}")
    lines.append("no single success score; no winner/loser; no cause is claimed")
    return "\n".join(lines)


def render(payload: dict) -> str:
    if not payload["conversion_table_ready"]:
        return "no conversion table yet (migration 74dbecaa4bb2 not applied): nothing to measure"
    if not payload["outcomes"]:
        return "no converted growth action matches: nothing to measure"
    lines = []
    for o in payload["outcomes"]:
        a, life = o["anchor"], o["lifecycle"]
        lines.append(f"growth action {a['growth_action_id']} {a['action_type']} {a['subject_id']} "
                     f"→ {a['downstream_type']} #{a['downstream_id']} "
                     f"({a.get('downstream_state')}); effective_at "
                     f"{life['effective_at'] or '— (not in effect)'}"
                     + (f" [{life['effective_event']}]" if life["effective_event"] else "")
                     + f"; state {o['measurement_state']}")  # fmt: skip
        lines.append("  lifecycle: " + " → ".join(
            f"{s['name']}={s['state'] or '—'}" for s in life["stages"]))
        for c in o["checkpoints"]:
            lines.append(f"  {c['name']}: {c['state']}"
                         + (f" ({'; '.join(c['reasons'])})" if c["reasons"] else ""))
            lines += [f"    - {obs}" for obs in c["observations"]]
        lines.append("  waiting: " + ("; ".join(o["waiting"]) or "nothing"))
        lines.append(f"  next measurement: {o['next_measurement_at'] or '—'}")
    lines.append("no cause is claimed; no success score")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
