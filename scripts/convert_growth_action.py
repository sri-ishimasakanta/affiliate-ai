"""承認した Growth Action を既存の流れへ渡す (C9 Batch 3 / C9-B)。**既定は PLAN (書かない)。**

    uv run python scripts/convert_growth_action.py <growth_action_id>
    uv run python scripts/convert_growth_action.py <growth_action_id> --format json
    uv run python scripts/convert_growth_action.py <growth_action_id> --execute
    uv run python scripts/convert_growth_action.py <growth_action_id> --execute \
        --expected-source-hash <hash from the plan>   # 変更の準備 (本文・メタ・配置)

``--execute`` で書くのは **手元の表だけ** (内部リンクの見直しなら ``change_requests`` に
awaiting_approval の依頼 1 つ + C9 の記録)。WordPress・Threads・OpenAI・メール・外の API には
触れない。作った変更の依頼は、その流れの人の承認 (``manage_change_requests.py approve``) と
適用 (``apply_approved_change.py``) を別に待つ。Growth Action の承認はそれらの承認ではない。
C9-B: Threads の提案 → 記事を指定した生成の依頼、新しい記事 → 記事の計画の依頼、本文・メタ・
配置 → 変更の準備の依頼 (どれも ``growth_handoff_requests`` の 1 行だけ。OpenAI・Threads・WordPress
を呼ばない)。依頼の人の判断と結びつけは ``manage_growth_actions.py handoff ...``。Growth の投稿は
計画だけ (Growth の流れが担う)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.growth_action_conversion_service import (  # noqa: E402
    GrowthActionConversionService,
)
from app.services.growth_action_service import GrowthActionError  # noqa: E402

SIDE_EFFECTS = ("WordPress writes = 0, Threads writes = 0, OpenAI calls = 0, emails = 0, "
                "external calls = 0")


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("growth_action_id", type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-source-hash", dest="expected_source_hash", default=None,
                        help="計画で見た元の hash (変更の準備は必須。違えば断る)")
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
    code = 0
    with session_factory() as session:
        service = GrowthActionConversionService(session, settings=settings)
        try:
            if args.execute:
                result = service.execute(args.growth_action_id, now=now,
                                         expected_source_hash=args.expected_source_hash)
            else:
                result = service.plan(args.growth_action_id, now=now)
                session.rollback()
        except GrowthActionError as exc:
            session.rollback()
            print(f"refused: {exc.reason}")
            code = 2
            result = None
    if result is not None:
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(render(result, executed=args.execute))
    print(f"side effects: {SIDE_EFFECTS}")
    return code


def render(result: dict, *, executed: bool) -> str:
    plan = result["plan"]
    head = ("EXECUTED" if result.get("executed") else
            "ALREADY CONVERTED (no new write)" if result.get("idempotent_replay") else "PLAN")
    lines = [f"conversion {head} — growth action {plan['growth_action_id']} "
             f"{plan['opportunity_key']} (revision {plan['revision']})",
             f"review {plan['review_id']} ({plan['review_status']}); execution mode "
             f"{plan['execution_mode']} → {plan['target_workflow']}",
             f"idempotency key {plan['idempotency_key']}; plan hash {plan['plan_hash'][:16]}"]
    if plan.get("missing"):
        lines.append(f"not executable: {plan['missing']}")
    lines.append("checks:")
    lines += [f"  [{'ok' if c['ok'] else 'NO'}] {c['name']}: {c['detail']}"
              for c in result["checks"]]
    lines.append("expected local writes: " + ("; ".join(plan["expected_local_writes"]) or "none"))
    lines.append("expected external writes: none")
    if plan.get("frozen_preview") is not None:
        frozen = plan["frozen_preview"]
        lines.append(f"meaning: {plan['meaning']}")
        detail = {k: frozen[k] for k in ("lane", "requested_angle", "change_type", "keyword")
                  if frozen.get(k) is not None}
        lines.append(f"frozen: {detail}")
        lines.append(f"source hash {plan['source_hash']}")
    for d in result.get("downstream") or []:
        lines.append(f"downstream {d['type']} #{d['id']}: {d['state']}; effective_at "
                     f"{d.get('effective_at') or '— (not applied)'}")
    if not executed and result.get("executable"):
        extra = (f" --expected-source-hash {plan['source_hash']}"
                 if plan.get("target_workflow") == "change_preparation_request" else "")
        lines.append(f"re-run with --execute{extra} to hand off (local write only)")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
