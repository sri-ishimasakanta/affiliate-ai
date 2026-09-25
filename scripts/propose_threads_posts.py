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

T5.5: prompt には T5 の学習からの **弱い参考** が入る (証拠が足りなければ中立)。
``--print-prompt`` が出す ``--learning-as-of`` と ``--guidance-fingerprint`` を
``--input`` のときにも渡すと、prompt と同じ参考であることを確かめてから記録する。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import UTC, datetime  # noqa: E402

from app.config.database import SessionLocal  # noqa: E402
from app.services.threads_proposal_service import (  # noqa: E402
    ThreadsProposalError,
    ThreadsProposalService,
)
from app.social.threads.policy import get_policy  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 2


def main(argv: list[str] | None = None, *, session_factory=None, service_overrides=None) -> int:
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
    parser.add_argument(
        "--learning-as-of",
        help="学習の参考を作る時刻 (ISO 8601。既定は現在)。prompt と同じ値を渡す",
    )
    parser.add_argument(
        "--guidance-fingerprint",
        help="prompt を作ったときの参考の指紋。違えば拒否する",
    )
    args = parser.parse_args(argv)

    policy = get_policy()
    with (session_factory or SessionLocal)() as session:
        service = ThreadsProposalService(session, policy=policy, **(service_overrides or {}))
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
    # 学習の参考は、省略時も 1 つの時刻に固定する (prompt と検査で同じ参考を使う)。
    as_of = (
        datetime.fromisoformat(args.learning_as_of)
        if args.learning_as_of
        else datetime.now(UTC).replace(microsecond=0)
    )
    if args.print_prompt:
        package = service.build_prompt(
            article_id=args.article_id, angles=args.angles, learning_as_of=as_of
        )
        print(package.rendered_prompt)
        print(f"\n--- prompt_hash: {package.prompt_hash} ---")
        for line in format_learning(package.guidance.as_dict()):
            print(line)
        print("\nこの prompt を外部で実行し、返ってきた JSON を次のように渡す:")
        print(
            f"  --input out.json --learning-as-of {as_of.isoformat()} "
            f"--guidance-fingerprint {package.guidance.fingerprint}"
        )
        return package.as_dict()

    if not args.input_path:
        raise ThreadsProposalError("--input (generated JSON) or --print-prompt is required")
    generated = Path(args.input_path).read_text(encoding="utf-8")

    if not args.execute:
        prepared = service.plan(
            article_id=args.article_id,
            generated_output=generated,
            learning_as_of=as_of,
            expected_guidance=args.guidance_fingerprint,
        )
        print("=== threads post proposals (PLAN) ===")
        print(f"article            = {prepared.source_article_id} {prepared.source_article_title}")
        print(f"body hash (frozen) = {prepared.source_article_body_hash[:16]}")
        print(f"policy / generator = {prepared.policy_version} / {prepared.generator_version}")
        print(f"would create       = {prepared.acceptable}")
        print(f"rejected           = {len(prepared.rejected)}")
        for line in format_learning(prepared.learning):
            print(line)
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

    rows = service.persist(
        article_id=args.article_id,
        generated_output=generated,
        learning_as_of=as_of,
        expected_guidance=args.guidance_fingerprint,
    )
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


def format_learning(learning: dict) -> list[str]:
    """学習の参考を短く見せる (通常の出力を埋めない)。理由は 1 行ずつ出す。"""

    lines = [
        "",
        f"learning policy      = {learning['source_policy_version']} ({learning['source_schema']})",
        f"learning as_of       = {learning['as_of_local'] or learning['as_of']}",
        f"learning evidence    = {learning['evidence_status']}",
        f"guidance applied     = {learning['mode']}",
        f"guidance fingerprint = {learning['fingerprint'][:16]}",
    ]
    if "verified_against_prompt" in learning:
        lines.append(
            f"guidance verified    = {learning['verified_against_prompt']} "
            "(matches the prompt's fingerprint)"
            if learning["verified_against_prompt"]
            else "guidance verified    = False (pass --guidance-fingerprint to check)"
        )
    for preference in learning["preferences"]:
        flags = ["weak"]
        if preference["tentative"]:
            flags.append("tentative")
        if not preference["actionable"]:
            flags.append("informational: the generator cannot produce this value")
        context = (
            " [publication context only; the worker's timing is unchanged]"
            if preference["dimension"] in ("topic", "daypart", "weekday")
            else ""
        )
        lines.append(
            f"  {' '.join(flags)} {preference['direction']} "
            f"{preference['dimension']}={preference['value']}{context}"
        )
        for evidence in preference["evidence"]:
            lines.append(
                f"    evidence: n={evidence['mature_posts']} vs n={evidence['other_mature_posts']} "
                f"mature posts; median {evidence['metric']} {evidence['median']} vs "
                f"{evidence['other_median']} ({evidence['compared_with']})"
                + ("; narrowing" if evidence["weakening"] else "")
            )
    for neutral in learning["neutral_dimensions"]:
        lines.append(f"  {neutral['dimension']}: neutral ({neutral['reason']})")
    for note in learning.get("diversity_notes", ()):
        lines.append(f"  diversity: {note}")
    for note in learning["notes"]:
        lines.append(f"  note: {note}")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
