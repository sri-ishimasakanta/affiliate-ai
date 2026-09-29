"""commercial_intent を保存済みの Google Ads の値から V2 で導き直す (C10-A)。**既定は PLAN。**

    uv run python scripts/rederive_commercial_intent.py                    # PLAN (書かない)
    uv run python scripts/rederive_commercial_intent.py --format json
    uv run python scripts/rederive_commercial_intent.py --execute \\
        --expect-rederive <N> --expect-rescore <M>

Google Ads を呼ばない (最新の commercial_intent の ``raw_data`` にある入札・competition を使う)。
``--execute`` で書くのは ``keyword_signals`` の追記 (元の観測の時刻のまま) と、値の変わった
既存 score の付け直しだけ。期待値が PLAN と違えば何も書かずに断る。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.commercial_intent_rederive_service import (  # noqa: E402
    CommercialIntentRederiveService,
    RederiveRefusedError,
)


def main(argv=None, *, session_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expect-rederive", type=int, dest="expect_rederive")
    parser.add_argument("--expect-rescore", type=int, dest="expect_rescore")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    if args.execute and (args.expect_rederive is None or args.expect_rescore is None):
        parser.error("--execute needs --expect-rederive and --expect-rescore (from the PLAN)")
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        service = CommercialIntentRederiveService(session)
        if args.execute:
            try:
                result = service.execute(expect_rederive=args.expect_rederive,
                                         expect_rescore=args.expect_rescore)
            except RederiveRefusedError as exc:
                print(f"refused: {exc.reason}")
                return 2
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        plan = service.plan().as_dict()
    if args.format == "json":
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(plan))
    print("PLAN: database writes = 0, external calls = 0")
    return 0


def render(plan: dict) -> str:
    c = plan["counts"]
    lines = [f"commercial_intent re-derivation PLAN → {plan['normalizer']['name']} "
             f"{plan['normalizer']['version']} (stored Google Ads values only)",
             f"keywords {c['keywords']}; " + ", ".join(f"{k}={v}"
                                                   for k, v in c["by_decision"].items()),
             f"value changes {c['value_changes']}; signal inserts {c['expected_signal_inserts']}; "
             f"rescores {c['expected_rescores']}; market evidence "
             + ", ".join(f"{k}={v}" for k, v in c["market_evidence"].items()), ""]
    for i in plan["items"]:
        if i["decision"] != "rederive":
            continue
        mark = "*" if i["value_changed"] else " "
        lines.append(f"{mark} [{i['keyword_id']}] {i['keyword']}: {i['current_value']} "
                     f"({i['current_version']}) → {i['new_value']} [{i['market_evidence_state']}]"
                     + (f" flags={','.join(i['quality_flags'])}" if i["quality_flags"] else "")
                     + (" rescore" if i["rescore"] else "")
                     + (f" rescore blocked: missing {','.join(i['rescore_blocked'])}"
                        if i["rescore_blocked"] else ""))
    missing = [i for i in plan["items"] if i["decision"] == "no_stored_metrics"]
    if missing:
        lines.append(f"no stored Google Ads metrics (needs an external refresh; not called): "
                     f"{len(missing)} keyword(s)")
    lines.append(f"re-run with --execute --expect-rederive {c['expected_signal_inserts']} "
                 f"--expect-rescore {c['expected_rescores']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
