"""管理用 CLI: 携帯で下された決定をローカルへ取り込む (C8.8)。

    # 何が起きるかだけ見る (既定。ローカルには何も書かない)
    uv run python scripts/sync_mobile_approvals.py

    # 実際に C9 へ記録する
    uv run python scripts/sync_mobile_approvals.py --execute

この CLI は **決定を運ぶだけ** である。決定を作り出すことはない:

- 中継に人の決定が無ければ、何も起きない。
- 候補の優先度から承認を作らない。
- 自動承認も自動却下もしない。
- **実行はしない。** WordPress にも Threads にも触れない。

記録は必ず既存の ``ChangeRequestService`` を通る。したがって提案 hash の一致・
版の一致・陳腐化判定は、PC で承認したときとまったく同じである。違うのは
記録される決定者 (``human-mobile``) だけ。

実行したい場合は、subject に応じた別コマンドを人が実行する:

    記事変更 : uv run python scripts/apply_approved_change.py <id> --execute
    Threads  : uv run python scripts/publish_threads_post.py --proposal-id <id> --execute
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.mobile_approval_service import MobileApprovalService  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="決定を C9 へ記録する (既定は PLAN)")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    with SessionLocal() as session:
        outcome = MobileApprovalService(session, settings=settings).sync(execute=args.execute)

    print("=== mobile approval sync ===")
    print(f"executed = {outcome.executed}")
    print(f"fetched  = {outcome.fetched}")
    print(f"applied  = {outcome.applied}")
    print(f"skipped  = {outcome.skipped}")
    print(f"failed   = {outcome.failed}")
    for detail in outcome.details:
        print(
            f"  {detail.get('relay_session_id')}: {detail['result']}"
            + (f" ({detail['reason']})" if detail.get("reason") else "")
        )
    if not args.execute:
        print("\nPLAN のみ。記録するには --execute を付ける。")
    print("承認は実行ではない。記事変更の適用も Threads の投稿も、別のコマンドで人が実行する。")
    print("  記事変更 : uv run python scripts/apply_approved_change.py <id> --execute")
    print("  Threads  : uv run python scripts/publish_threads_post.py --proposal-id <id> --execute")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON written to {args.json_path}")
    return EXIT_OK if outcome.failed == 0 else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
