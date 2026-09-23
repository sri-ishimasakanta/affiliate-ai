"""管理用 CLI: 記事 1 本から Threads 投稿案を作る (T2)。

    # 1) 生成用の prompt を出す (外部呼び出しなし)
    uv run python scripts/propose_threads_posts.py --article-id 21 --print-prompt

    # 2) 生成結果を検査するだけ (既定。DB にも外部にも書かない)
    uv run python scripts/propose_threads_posts.py --article-id 21 --input out.json

    # 3) 検査を通った案を awaiting_approval として保存する
    uv run python scripts/propose_threads_posts.py --article-id 21 --input out.json --execute

生成そのものは、このリポジトリの記事生成と同じく **外部で人が動かす**。prompt は
ここで決定的に組み立て、結果 (JSON) を ``--input`` で受け取る。

**承認も公開もしない。** 保存されるのは ``awaiting_approval`` の案だけで、承認は
``send_mobile_approval.py`` が携帯へ送り、公開は T3 の別操作である。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.services.threads_proposal_service import (  # noqa: E402
    ThreadsProposalError,
    ThreadsProposalService,
)
from app.social.threads.policy import get_policy  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--article-id", type=int, required=True)
    parser.add_argument(
        "--angle",
        action="append",
        dest="angles",
        help="切り口 (繰り返し指定可。既定はポリシーの全切り口)",
    )
    parser.add_argument("--print-prompt", action="store_true", help="prompt だけを出力する")
    parser.add_argument("--input", dest="input_path", help="生成結果 (JSON) のパス")
    parser.add_argument("--execute", action="store_true", help="検査を通った案を保存する")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    policy = get_policy()
    with SessionLocal() as session:
        service = ThreadsProposalService(session, policy=policy)
        try:
            payload = _dispatch(service, policy, args)
        except ThreadsProposalError as exc:
            print(f"refused: {exc.reason}")
            return EXIT_REFUSED

        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\nJSON written to {args.json_path}")
    return EXIT_OK


def _dispatch(service: ThreadsProposalService, policy, args) -> dict:
    if args.print_prompt:
        package = service.build_prompt(article_id=args.article_id, angles=args.angles)
        print(package.rendered_prompt)
        print(f"\n--- prompt_hash: {package.prompt_hash} ---")
        print("この prompt を外部で実行し、返ってきた JSON を --input で渡す。")
        return package.as_dict()

    if not args.input_path:
        raise ThreadsProposalError("--input (generated JSON) or --print-prompt is required")
    generated = Path(args.input_path).read_text(encoding="utf-8")

    if not args.execute:
        prepared = service.plan(article_id=args.article_id, generated_output=generated)
        print("=== threads post proposals (PLAN) ===")
        print(f"article            = {prepared.source_article_id} {prepared.source_article_title}")
        print(f"body hash (frozen) = {prepared.source_article_body_hash[:16]}")
        print(f"policy / generator = {prepared.policy_version} / {prepared.generator_version}")
        print(f"would create       = {prepared.acceptable}")
        print(f"rejected           = {len(prepared.rejected)}")
        for candidate in prepared.candidates:
            print(
                f"\n  [{candidate['angle']}] {candidate['character_count']} chars "
                f"link={candidate['link_mode']} hash={candidate['proposal_hash'][:16]}"
            )
            print(f"    {candidate['preview']}")
            for warning in candidate["warnings"]:
                print(f"    warning: {warning}")
        for rejected in prepared.rejected:
            print(f"\n  REJECTED [{rejected['angle']}]: {'; '.join(rejected['errors'])}")
            print(f"    {rejected['preview']}")
        print("\nPLAN のみ。保存するには --execute を付ける。")
        print("保存しても承認にはならない。承認は send_mobile_approval.py。")
        return prepared.as_dict()

    rows = service.persist(article_id=args.article_id, generated_output=generated)
    print("=== threads post proposals stored ===")
    for row in rows:
        print(
            f"  #{row.id} [{row.angle}] {row.character_count} chars "
            f"link={row.link_mode} status={row.status} hash={row.proposal_hash[:16]}"
        )
    print("\n承認はされていない。携帯へ送るには:")
    print(
        "  uv run python scripts/send_mobile_approval.py "
        "--subject-type threads_post --subject-id <id> --execute"
    )
    return {
        "stored": [
            {
                "id": row.id,
                "angle": row.angle,
                "status": row.status,
                "proposal_hash": row.proposal_hash,
                "character_count": row.character_count,
                "link_mode": row.link_mode,
                "destination_url": row.destination_url,
            }
            for row in rows
        ]
    }


if __name__ == "__main__":
    raise SystemExit(main())
