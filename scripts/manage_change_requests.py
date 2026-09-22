"""管理用 CLI: 変更提案の閲覧・承認・却下 (C9.1)。

    # 一覧 (既定は全件。status で絞れる)
    uv run python scripts/manage_change_requests.py list --status awaiting_approval

    # 1 件を全文・差分つきで読む (承認する前に必ずこれを読む)
    uv run python scripts/manage_change_requests.py show 1

    # 承認 -- **読んだ提案の hash を明示する**
    uv run python scripts/manage_change_requests.py approve 1 --proposal-hash <hash>

    # 書き込み前に失敗した適用を、同じ承認のまま再試行可能に戻す
    uv run python scripts/manage_change_requests.py retry 1 --proposal-hash <hash>

    # 却下 -- 理由は必須
    uv run python scripts/manage_change_requests.py reject 1 --reason "リンク先が弱い"

承認は ``--proposal-hash`` を要求する。提案が作り直されていれば hash が変わるので、
**古い承認が新しい文章に移ることはない**。

承認しても WordPress は何も変わらない。適用は
``scripts/apply_approved_change.py`` が別途行い、そこでも既定は PLAN である。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.services.change_request_service import (  # noqa: E402
    ChangeRequestError,
    ChangeRequestService,
)

EXIT_OK = 0
EXIT_REFUSED = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="提案の一覧")
    listing.add_argument("--status", help="status で絞る (例: awaiting_approval)")

    show = sub.add_parser("show", help="1 件を差分つきで読む")
    show.add_argument("request_id", type=int)

    approve = sub.add_parser("approve", help="この 1 つの提案を承認する")
    approve.add_argument("request_id", type=int)
    approve.add_argument("--proposal-hash", required=True, help="読んだ提案の hash")
    approve.add_argument("--decided-by", default="human")
    approve.add_argument("--reason", help="承認理由 (任意)")

    retry = sub.add_parser(
        "retry", help="書き込み前に失敗した適用を、同じ承認のまま再試行可能に戻す"
    )
    retry.add_argument("request_id", type=int)
    retry.add_argument("--proposal-hash", required=True, help="いまの提案 hash")
    retry.add_argument("--reason", help="再試行する理由 (任意)")

    reject = sub.add_parser("reject", help="提案を却下する")
    reject.add_argument("request_id", type=int)
    reject.add_argument("--reason", required=True, help="却下理由 (必須)")
    reject.add_argument("--decided-by", default="human")

    args = parser.parse_args(argv)

    with SessionLocal() as session:
        service = ChangeRequestService(session)
        try:
            payload = _dispatch(service, args)
        except ChangeRequestError as exc:
            print(f"refused: {exc.reason}")
            return EXIT_REFUSED

        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\nJSON written to {args.json_path}")
    return EXIT_OK


def _dispatch(service: ChangeRequestService, args) -> dict:
    if args.command == "list":
        requests = service.list_requests(status=args.status)
        rows = [_summary(service, request) for request in requests]
        print("=== change requests ===")
        print(f"count = {len(rows)}")
        for row in rows:
            print(
                f"  #{row['id']:<4} {row['status']:<18} v{row['proposal_version']} "
                f"article {row['article_id']} -> {row['target_article_id']} "
                f"{row['change_type']} hash={row['proposal_hash'][:16]}"
            )
        return {"requests": rows}

    if args.command == "show":
        request = service.list_requests()
        request = next((r for r in request if r.id == args.request_id), None)
        if request is None:
            raise ChangeRequestError(f"change request {args.request_id} not found")
        report = service.evaluate_staleness(request)
        approval = service.latest_approval(request)
        proposal = request.proposal_json or {}
        print("=== change request ===")
        print(f"id                  = {request.id}")
        print(f"status              = {request.status}")
        print(f"change_type         = {request.change_type}")
        print(f"article             = {request.article_id} -> {request.target_article_id}")
        print(f"proposal_version    = {request.proposal_version}")
        print(f"proposal_hash       = {request.proposal_hash}")
        print(f"source_body_hash    = {request.expected_source_body_hash}")
        print(f"proposed_body_hash  = {request.proposed_body_hash}")
        print(f"source_candidate    = {request.source_engine}:{request.source_candidate_id} ")
        print(f"candidate_priority  = {request.source_candidate_priority}")
        print(f"policy_version      = {request.source_policy_version}")
        print(f"stale               = {report.stale}")
        for reason in report.reasons:
            print(f"  stale reason: {reason}")
        print(f"candidate_present   = {report.candidate_currently_present}")
        print(f"approved            = {bool(approval)}")
        print(f"\nrationale: {request.rationale}")
        print("\n--- diff ---")
        print(proposal.get("unified_diff", ""))
        print("\n--- inserted paragraph ---")
        print(proposal.get("inserted_paragraph", ""))
        print(
            "\n承認する場合: manage_change_requests.py approve "
            f"{request.id} --proposal-hash {request.proposal_hash}"
        )
        return {
            "request": _summary(service, request),
            "proposal": proposal,
            "stale": report.stale,
            "stale_reasons": list(report.reasons),
            "candidate_currently_present": report.candidate_currently_present,
            "approved": bool(approval),
        }

    if args.command == "retry":
        request = service.reopen_for_retry(
            args.request_id, proposal_hash=args.proposal_hash, reason=args.reason
        )
        print(f"reopened request {request.id} for retry -> status {request.status}")
        print(f"status_reason = {request.status_reason}")
        print("承認は作り直していない。失敗した適用の履歴もそのまま残っている。")
        print("適用は apply_approved_change.py --execute (別コマンド)。")
        return {
            "change_request_id": request.id,
            "status": request.status,
            "status_reason": request.status_reason,
            "proposal_hash": request.proposal_hash,
        }

    if args.command == "approve":
        approval = service.approve(
            args.request_id,
            proposal_hash=args.proposal_hash,
            decided_by=args.decided_by,
            reason=args.reason,
        )
        print(f"approved request {args.request_id} (approval {approval.id})")
        print(f"approved_proposal_hash = {approval.approved_proposal_hash}")
        print("WordPress はまだ何も変わっていない。適用は apply_approved_change.py。")
        return {
            "approval_id": approval.id,
            "change_request_id": approval.change_request_id,
            "approved_proposal_hash": approval.approved_proposal_hash,
            "approved_proposal_version": approval.approved_proposal_version,
            "candidate_present_at_decision": approval.candidate_present_at_decision,
        }

    approval = service.reject(args.request_id, reason=args.reason, decided_by=args.decided_by)
    print(f"rejected request {args.request_id} (record {approval.id})")
    return {
        "approval_id": approval.id,
        "change_request_id": approval.change_request_id,
        "decision": approval.decision,
        "reason": approval.reason,
    }


def _summary(service: ChangeRequestService, request) -> dict:
    return {
        "id": request.id,
        "status": request.status,
        "change_type": request.change_type,
        "article_id": request.article_id,
        "target_article_id": request.target_article_id,
        "proposal_version": request.proposal_version,
        "proposal_hash": request.proposal_hash,
        "source_engine": request.source_engine,
        "source_candidate_id": request.source_candidate_id,
        "source_candidate_priority": request.source_candidate_priority,
        "created_at": request.created_at.isoformat() if request.created_at else None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
