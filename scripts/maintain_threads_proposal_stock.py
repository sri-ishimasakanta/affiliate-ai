"""管理用 CLI: Threads の投稿案の在庫を保守する (T6)。

    # 既定は PLAN (DB にもファイルにも書かない。Threads にもメールにも触れない)
    uv run python scripts/maintain_threads_proposal_stock.py

    # 1 回だけ保守する: 届いた生成結果を取り込み、必要なら生成の依頼を出す
    uv run python scripts/maintain_threads_proposal_stock.py --execute

    # 届いた生成結果を取り込むだけ (新しい依頼は絶対に出さない。T6.1)
    uv run python scripts/maintain_threads_proposal_stock.py --collect-only --execute

**承認も却下も公開もしない。** 保存される提案は ``awaiting_approval`` で、人への依頼は
既存の承認 digest (T4.2) が通知の時間帯の中で送る。1 回で保存するのは最大 3 本。

来歴の列 (migration ``afc2f36bb3ca``) が無い DB では、PLAN は動くが保存も依頼もしない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.threads_proposal_stock_service import (  # noqa: E402
    ThreadsProposalStockService,
)

EXIT_OK = 0


def main(
    argv: list[str] | None = None,
    *,
    session_factory=None,
    settings=None,
    overrides=None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="1 回だけ保守する (既定は PLAN)")
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="届いた生成結果を取り込むだけ。新しい生成の依頼は出さない",
    )
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()

    with session_factory() as session:
        service = ThreadsProposalStockService(session, settings=settings, **(overrides or {}))
        result = service.maintain(now=now, execute=args.execute, collect_only=args.collect_only)
    plan = result["plan"] if args.execute else result
    print(format_plan(plan, executed=args.execute))
    if args.execute:
        print(format_outcome(result))
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        print(f"\nJSON written to {args.json_path}")
    return EXIT_OK


def format_plan(plan: dict, *, executed: bool) -> str:
    stock = plan["stock"]
    counts = stock["counts"]
    learning = plan["learning"]
    provider = plan["provider"]
    migration = plan["migration"]
    lines = [
        f"=== threads proposal stock ({'EXECUTE' if executed else 'PLAN'}) ===",
        f"analysis time      = {plan['generated_at_local']}",
        f"policy             = {plan['policy_version']}",
        f"mode               = {plan['mode']}"
        + (
            " (submission suppressed: no new generation request will be sent)"
            if plan["submission_suppressed"]
            else ""
        ),
        f"usable stock       = {stock['usable']} (advisory floor {stock['target_low']}, "
        f"ceiling {stock['target_high']}; not a quota)",
        "  "
        + " ".join(
            f"{k}={counts[k]}"
            for k in ("prepared", "requested", "approved_unpublished", "scheduled")
        ),
        "  not usable: "
        + " ".join(
            f"{k}={counts[k]}"
            for k in ("held", "expired", "stale", "rejected", "superseded", "published")
        ),
        f"topics (usable)    = {_mapping(stock['usable_topics'])}",
        f"angles (usable)    = {_mapping(stock['usable_angles'])}",
        f"links (usable)     = {_mapping(stock['usable_link_modes'])}",
        f"learning guidance  = {learning['mode']} ({learning['evidence_status']}, "
        f"policy {learning['learning_policy_version']}, "
        f"fingerprint {learning['fingerprint'][:16]})",
        f"generation needed  = {stock['needs_generation']}",
    ]
    lines += [f"  reason: {reason}" for reason in stock["reasons"]]
    lines.append(
        f"would request      = {plan['would_request']} "
        f"(max {plan['max_new_proposals_per_cycle']} per cycle, one per article)"
    )
    lines += [f"  blocked: {reason}" for reason in plan["blocked_by"]]
    if stock["requests"]:
        lines.append("planned requests:")
        for request in stock["requests"]:
            lines.append(
                f"  article {request['article_id']} [{request['article_title']}] "
                f"angle={request['angle']} link={request['link_mode']}"
            )
            lines += [f"      - {reason}" for reason in request["reasons"]]
    if stock["skipped_articles"]:
        lines.append(f"articles not chosen = {_mapping(stock['skipped_articles'])}")
    lines.append(
        f"provider           = {provider['name']} available={provider['available']} "
        f"({'synchronous' if provider['synchronous'] else 'asynchronous'})"
    )
    lines.append(f"  {provider['reason']}")
    lines.append(
        f"pending requests   = {len(plan['pending_requests'])} "
        f"(with a response to collect: {plan['would_collect']})"
    )
    for request in plan["pending_requests"]:
        lines.append(
            f"  {request['request_id']} article {request['article_id']} {request['angles']} "
            f"response={'yes' if request['has_response'] else 'no'} stale={request['stale']}"
        )
    lines.append(
        "migration          = learning provenance column "
        + (
            "present; saving allowed"
            if migration["can_save"]
            else f"MISSING (needs {migration['required_revision']}); saving and new requests "
            "are blocked"
        )
    )
    review = stock["re_review"]
    lines.append(
        "re-review          = "
        + (", ".join(f"#{r['proposal_id']} ({r['reason']})" for r in review) or "none")
    )
    last = plan.get("last_status") or {}
    lines.append(f"last maintenance   = {last.get('last_maintenance_at') or '(never)'}")
    if not executed:
        lines.append(
            "\nPLAN only: nothing was written, requested, approved or published. "
            "Run with --execute to maintain once."
        )
    return "\n".join(lines)


def format_outcome(outcome: dict) -> str:
    lines = ["", "--- executed ---"]
    lines.append(f"proposals created  = {len(outcome['created'])} {outcome['created']}")
    lines.append(f"requests created   = {len(outcome['requests_created'])}")
    for request_id in outcome["requests_created"]:
        lines.append(
            f"  {request_id}: run pending/{request_id}.prompt.txt, save the JSON as "
            f"pending/{request_id}.response.json"
        )
    for item in outcome["skipped"]:
        lines.append(f"  skipped {item['request_id']}: {item['reason']}")
    for item in outcome["failures"]:
        lines.append(f"  FAILED {item['request_id']}: {item['reason']}")
    for request_id in outcome["stale_requests"]:
        lines.append(f"  stale request moved to failed/: {request_id}")
    lines.append("new proposals are awaiting_approval; the approval digest asks a human.")
    return "\n".join(lines)


def _mapping(values: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in values.items()) or "(none)"


if __name__ == "__main__":
    raise SystemExit(main())
