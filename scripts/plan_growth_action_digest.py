"""Growth Action のまとめ (digest) の PLAN (C9-A)。**送らない・書かない。**

``manage_growth_actions.py digest-plan`` と同じもの (選び方は ``app.growth.digest``: いま動ける・
まだ扱われていない・同じ状態で知らせていない候補から、成分の順で 5 件まで。1 つの点数は作らない)。
Threads の提案の承認のまとめとは別物。

    uv run python scripts/plan_growth_action_digest.py
    uv run python scripts/plan_growth_action_digest.py --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.growth_action_digest_service import GrowthActionDigestService  # noqa: E402
from app.services.growth_action_service import build_inbox  # noqa: E402
from scripts.manage_growth_actions import render_digest  # noqa: E402


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", dest="as_of")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    with session_factory() as session:
        plan = GrowthActionDigestService(session, settings=settings,
                                         inbox_builder=build_inbox).plan(now=as_of)
        session.rollback()
    if args.format == "json":
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_digest(plan))
    print("PLAN only: emails = 0, database writes = 0, WordPress writes = 0, Threads writes = 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
