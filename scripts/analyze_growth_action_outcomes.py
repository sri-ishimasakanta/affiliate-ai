"""変換した Growth Action の効果の観測 (C9 Batch 3)。**読むだけ。**

    uv run python scripts/analyze_growth_action_outcomes.py list
    uv run python scripts/analyze_growth_action_outcomes.py show <growth_action_id>
    uv run python scripts/analyze_growth_action_outcomes.py show <growth_action_id> --checkpoint 7d
    uv run python scripts/analyze_growth_action_outcomes.py list --format json

実際に変わった時刻 (適用の成功・公開) から、同じ長さの窓で、変わる前と後に観測したものを並べる。
因果を言わない・1 つの成功の点数を作らない。DB に書かない・外に問い合わせない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.growth.outcome import CHECKPOINTS  # noqa: E402
from app.services.growth_action_conversion_service import (  # noqa: E402
    GrowthActionOutcomeService,
    conversions_ready,
)


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("list", "show"))
    parser.add_argument("growth_action_id", type=int, nargs="?")
    parser.add_argument("--checkpoint", choices=[c[0] for c in CHECKPOINTS])
    parser.add_argument("--as-of", dest="as_of")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    now = datetime.fromisoformat(args.as_of) if args.as_of else None
    with session_factory() as session:
        ready = conversions_ready(session)
        service = GrowthActionOutcomeService(session, settings=settings)
        anchors = service.anchors() if ready else []
        if args.command == "show":
            anchors = [a for a in anchors if a.growth_action_id == args.growth_action_id]
        outcomes = [service.outcome(a, now=now, checkpoint=args.checkpoint).as_dict()
                    for a in anchors]  # fmt: skip
        session.rollback()
    payload = {"conversion_table_ready": ready, "outcomes": outcomes}
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(payload))
    print("read-only: database writes = 0, external calls = 0")
    return 0


def render(payload: dict) -> str:
    if not payload["conversion_table_ready"]:
        return "no conversion table yet (migration 74dbecaa4bb2 not applied): nothing to measure"
    if not payload["outcomes"]:
        return "no converted growth action yet: nothing to measure"
    lines = []
    for o in payload["outcomes"]:
        a = o["anchor"]
        lines.append(f"growth action {a['growth_action_id']} {a['action_type']} {a['subject_id']} "
                     f"→ {a['downstream_type']} #{a['downstream_id']} ({a['downstream_state']}); "
                     f"effective_at {a['effective_at'] or '— (not applied)'}; "
                     f"state {o['measurement_state']}")  # fmt: skip
        for c in o["checkpoints"]:
            lines.append(f"  {c['name']}: {c['state']}"
                         + (f" ({', '.join(c['reasons'])})" if c["reasons"] else ""))
            lines += [f"    - {obs}" for obs in c["observations"]]
    lines.append("no cause is claimed; no success score")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
