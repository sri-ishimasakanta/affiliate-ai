"""管理用 CLI: 承認済み変更の適用 (C9.2)。

    # 既定は PLAN -- いま適用できるかを判定するだけで、何も書かない
    uv run python scripts/apply_approved_change.py 1

    # 実際に適用する (承認済み + 陳腐化していない場合のみ)
    uv run python scripts/apply_approved_change.py 1 --execute

    # 巻き戻しに必要な素材を見る (実行はしない)
    uv run python scripts/apply_approved_change.py 1 --rollback-plan --application-id 3

``--execute`` を付けない限り **WordPress にも DB にも書かない**。付けた場合も、

- request が ``approved``
- 承認が **いまの** ``proposal_hash`` に対するもの
- 記事本文が提案時点から変わっていない
- リンク先がまだ公開されていて canonical も同じ
- 同じリンクがまだ存在しない

のすべてを満たしたときだけ進む。ひとつでも崩れていれば ``blocked`` で止まる。

書き込みは **既存の managed 経路** (編集改訂 -> artifact 承認 -> WordPress 更新 ->
読み戻し -> 必要なら照合) をそのまま通す。新しい更新スタックは作らない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.change_application_service import ChangeApplicationService  # noqa: E402
from app.services.change_request_service import ChangeRequestError  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_BLOCKED = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request_id", type=int, help="承認済み change request の id")
    parser.add_argument("--execute", action="store_true", help="実際に適用する (既定は PLAN のみ)")
    parser.add_argument("--idempotency-key", help="同じ適用を二重に走らせないためのキー")
    parser.add_argument(
        "--rollback-plan", action="store_true", help="巻き戻し素材を表示する (実行はしない)"
    )
    parser.add_argument("--application-id", type=int, help="--rollback-plan で使う適用 id")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    service = ChangeApplicationService(SessionLocal, settings=settings)

    try:
        if args.rollback_plan:
            if args.application_id is None:
                print("refused: --rollback-plan には --application-id が必要")
                return EXIT_REFUSED
            payload = service.rollback_plan(args.application_id)
            _print_rollback(payload)
        else:
            outcome = service.apply(
                args.request_id,
                execute=args.execute,
                idempotency_key=args.idempotency_key,
            )
            payload = outcome.as_dict()
            _print_outcome(outcome)
    except ChangeRequestError as exc:
        print(f"refused: {exc.reason}")
        return EXIT_REFUSED

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nJSON written to {args.json_path}")

    if not args.rollback_plan and payload.get("blocked_reasons"):
        return EXIT_BLOCKED
    return EXIT_OK


def _print_outcome(outcome) -> None:
    print("=== change application ===")
    print(f"request_id          = {outcome.request_id}")
    print(f"article_id          = {outcome.article_id}")
    print(f"executed            = {outcome.executed}")
    print(f"outcome             = {outcome.outcome}")
    print(f"proposal_hash       = {outcome.proposal_hash}")
    print(f"wordpress_post_id   = {outcome.wordpress_post_id}")
    print(f"pre_modified_gmt    = {outcome.wordpress_pre_modified_gmt}")
    print(f"post_modified_gmt   = {outcome.wordpress_post_modified_gmt}")
    print(f"editorial_revision  = {outcome.editorial_revision_id}")
    print(f"content_update_run  = {outcome.content_update_run_id}")
    print(f"reconciliation      = {outcome.reconciliation_id}")
    print(f"application_id      = {outcome.application_id}")
    for reason in outcome.blocked_reasons:
        print(f"  BLOCKED: {reason}")
    for warning in outcome.warnings:
        print(f"  warning: {warning}")
    if outcome.error_message:
        print(f"  error: {outcome.error_category}: {outcome.error_message}")
    if not outcome.executed and not outcome.blocked_reasons:
        print("\nPLAN のみ。実際に適用するには --execute を付ける。")


def _print_rollback(payload: dict) -> None:
    print("=== rollback plan (実行はしない) ===")
    print(f"change_application_id = {payload['change_application_id']}")
    print(f"article_id            = {payload['article_id']}")
    print(f"outcome               = {payload['outcome']}")
    print(f"rollback_available    = {payload['rollback_available']}")
    print(f"rollback_body_hash    = {payload['rollback_body_hash']}")
    print(f"current_body_hash     = {payload['current_body_hash']}")
    print(f"current matches applied = {payload['current_body_matches_applied']}")
    for line in payload["instructions"]:
        print(f"  {line}")


if __name__ == "__main__":
    raise SystemExit(main())
