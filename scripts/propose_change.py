"""管理用 CLI: 候補 1 件から、正確な変更提案を 1 つ作る (C9.1)。

    # 何が提案されるかだけ見る (既定。DB にも WordPress にも書かない)
    uv run python scripts/propose_change.py --candidate-id 84

    # 提案を awaiting_approval として永続化する
    uv run python scripts/propose_change.py --candidate-id 84 --execute

    # 機械可読な出力
    uv run python scripts/propose_change.py --candidate-id 84 --json proposal.json

**承認も適用もしない。** ``--execute`` が書くのは ``change_requests`` の 1 行だけで、
記事にも WordPress にも触れない。作られた request は人が
``scripts/manage_change_requests.py approve`` で承認するまで動かない。

候補 id は **明示的に指定する**。候補を自動で提案に変えることはしない
(C6/C7 は助言であって指示ではない)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.article.draft_promotion_canonical import compute_text_hash  # noqa: E402
from app.change.internal_link import build_internal_link_proposal  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.models import CHANGE_ADD_INTERNAL_LINK, Article, SeoImprovementCandidate  # noqa: E402
from app.services.change_request_service import (  # noqa: E402
    ChangeRequestError,
    ChangeRequestService,
)

EXIT_OK = 0
EXIT_REFUSED = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-id", type=int, required=True, help="C6 候補の id")
    parser.add_argument(
        "--change-type",
        default=CHANGE_ADD_INTERNAL_LINK,
        help=f"変更種別 (V1 で生成できるのは {CHANGE_ADD_INTERNAL_LINK} のみ)",
    )
    parser.add_argument(
        "--execute", action="store_true", help="提案を awaiting_approval として保存する"
    )
    parser.add_argument("--idempotency-key", help="同じ提案を二重に作らないためのキー")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        service = ChangeRequestService(session)
        if args.execute:
            try:
                request = service.propose_from_seo_candidate(
                    candidate_id=args.candidate_id,
                    change_type=args.change_type,
                    idempotency_key=args.idempotency_key,
                )
            except ChangeRequestError as exc:
                print(f"refused: {exc.reason}")
                return EXIT_REFUSED
            payload = {
                "persisted": True,
                "change_request_id": request.id,
                "status": request.status,
                "proposal_version": request.proposal_version,
                "proposal_hash": request.proposal_hash,
                "article_id": request.article_id,
                "target_article_id": request.target_article_id,
                "expected_source_body_hash": request.expected_source_body_hash,
                "proposed_body_hash": request.proposed_body_hash,
                "rationale": request.rationale,
                "proposal": request.proposal_json,
            }
        else:
            try:
                payload = _preview(session, args)
            except ChangeRequestError as exc:
                print(f"refused: {exc.reason}")
                return EXIT_REFUSED

        _print(payload)
        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\nJSON written to {args.json_path}")
    return EXIT_OK


def _preview(session, args) -> dict:
    """保存せずに、同じ規則で何が提案されるかを組み立てる。"""

    candidate = session.get(SeoImprovementCandidate, args.candidate_id)
    if candidate is None:
        raise ChangeRequestError(f"seo candidate {args.candidate_id} not found")
    source = session.get(Article, candidate.article_id)
    target_id = (candidate.evidence_json or {}).get("target_article_id")
    target = session.get(Article, target_id) if target_id else None
    if source is None or target is None:
        raise ChangeRequestError("source or target article is missing")

    proposal, rejection = build_internal_link_proposal(
        source_article_id=source.id,
        source_body=source.body or "",
        target_article_id=target.id,
        target_title=target.title or "",
        target_url=target.published_url or "",
        body_hasher=compute_text_hash,
    )
    if proposal is None:
        raise ChangeRequestError(f"{rejection.reason_code}: {rejection.detail}")
    return {
        "persisted": False,
        "change_request_id": None,
        "status": "preview",
        "article_id": source.id,
        "target_article_id": target.id,
        "candidate_id": candidate.id,
        "candidate_priority": candidate.priority,
        "expected_source_body_hash": proposal.source_body_hash,
        "proposed_body_hash": proposal.proposed_body_hash,
        "proposal": proposal.as_dict(),
    }


def _print(payload: dict) -> None:
    proposal = payload.get("proposal") or {}
    print("=== change proposal ===")
    print(f"persisted           = {payload['persisted']}")
    print(f"change_request_id   = {payload.get('change_request_id')}")
    print(f"status              = {payload.get('status')}")
    print(
        f"article             = {payload.get('article_id')} -> {payload.get('target_article_id')}"
    )
    print(f"proposal_hash       = {payload.get('proposal_hash', '(not persisted)')}")
    print(f"source_body_hash    = {payload.get('expected_source_body_hash')}")
    print(f"proposed_body_hash  = {payload.get('proposed_body_hash')}")
    print(f"anchor_text         = {proposal.get('anchor_text')}")
    print(f"target_url          = {proposal.get('target_url')}")
    print(f"insertion_line      = {proposal.get('insertion_line')}")
    for warning in proposal.get("warnings") or []:
        print(f"  warning: {warning}")
    print("\n--- diff ---")
    print(proposal.get("unified_diff", ""))
    print("\nこの提案は人が承認するまで適用されない。")
    print(
        "承認: uv run python scripts/manage_change_requests.py approve <id> --proposal-hash <hash>"
    )


if __name__ == "__main__":
    raise SystemExit(main())
