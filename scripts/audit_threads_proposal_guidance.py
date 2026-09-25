"""管理用 CLI: 保存した Threads 投稿案の学習の来歴を監査する (T6、**読むだけ**)。

    uv run python scripts/audit_threads_proposal_guidance.py --proposal-id 12

提案に残した来歴 (``learning_guidance_json``) の ``as_of`` で、T5 の学習から参考を作り直し、
指紋を比べる。T5.1 の保証により、``as_of`` の後に入ったデータで結果は変わらない。

終了コード: 0 = match / 1 = mismatch / 2 = 来歴なし・migration 前・見つからない。
DB には書かない。Threads にもメールにも触れない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.threads_proposal_service import (  # noqa: E402
    ThreadsProposalError,
    ThreadsProposalService,
)

EXIT_MATCH = 0
EXIT_MISMATCH = 1
EXIT_UNAVAILABLE = 2


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal-id", type=int, required=True)
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        try:
            result = ThreadsProposalService(session).audit_guidance(args.proposal_id)
        except ThreadsProposalError as exc:
            print(f"refused: {exc.reason}")
            return EXIT_UNAVAILABLE

    print("=== threads proposal guidance audit (read-only) ===")
    print(f"proposal   = #{result['proposal_id']} ({result['status']})")
    print(f"result     = {result['result']}")
    if "reason" in result:
        print(f"reason     = {result['reason']}")
    if "saved" in result:
        print(f"as_of      = {result['as_of']}")
        for side in ("saved", "rebuilt"):
            data = result[side]
            print(
                f"{side:10} = {data['fingerprint']} mode={data['mode']} "
                f"evidence={data['evidence_status']} policy={data['learning_policy_version']}"
            )
        print(
            f"verified against prompt at save time = {result['saved']['verified_against_prompt']}"
        )
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return {"match": EXIT_MATCH, "mismatch": EXIT_MISMATCH}.get(result["result"], EXIT_UNAVAILABLE)


if __name__ == "__main__":
    raise SystemExit(main())
