"""管理用 CLI: 承認済み Threads 投稿案を公開する (T3)。

    # 既定は PLAN。Meta には 1 度も書き込まない
    uv run python scripts/publish_threads_post.py --proposal-id 1

    # 実際に公開する (人が明示したときだけ)
    uv run python scripts/publish_threads_post.py --proposal-id 1 --execute

    # 応答を取りこぼした試行を、コンテナ状態の照会で確定させる
    uv run python scripts/publish_threads_post.py --proposal-id 1 --reconcile

**承認は公開ではない。** 携帯での承認は「公開してよい」状態にするだけで、外へ
出るのはこのコマンドを人が ``--execute`` 付きで実行したときだけである。
スケジューラからは決して呼ばれない。

送るのは承認された文字列そのもの。ここで文章も URL も作り直さない。

二重投稿は構造的に防ぐ: 提案 1 件につき公開行は 1 つで、media id を持つ行は
二度と公開しない。応答が不明なときは ``uncertain`` で止まり、照合するまで
再送しない。

T4.3: 前回の公開から 120 分空いていなければ、手動でも公開しない。人が理由を付けて
明示したときだけ、**間隔だけ** を上書きできる (理由は公開の行に残る)::

    uv run python scripts/publish_threads_post.py --proposal-id 3 --execute \
        --override-gap --override-reason "..."

他の公開が不確定・照合待ちなら、どの提案も公開しない (上書きもできない)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.threads_publication_service import (  # noqa: E402
    ManualGapOverride,
    ThreadsPublicationError,
    ThreadsPublicationService,
)

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNCERTAIN = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal-id", type=int, required=True)
    parser.add_argument("--execute", action="store_true", help="実際に公開する (既定は PLAN)")
    parser.add_argument(
        "--reconcile", action="store_true", help="不明な試行をコンテナ状態で確定させる"
    )
    parser.add_argument(
        "--override-gap",
        action="store_true",
        help="前回の公開からの 120 分の間隔だけを上書きする (理由が必須)",
    )
    parser.add_argument("--override-reason", default=None, help="間隔を上書きする理由")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    try:
        args.gap_override = _gap_override(args)
    except ValueError as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED

    settings = get_settings()
    with SessionLocal() as session:
        service = ThreadsPublicationService(session, settings=settings)
        try:
            payload, code = _dispatch(service, args)
        except ThreadsPublicationError as exc:
            print(f"refused: {exc.reason}")
            return EXIT_REFUSED

        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\nJSON written to {args.json_path}")
    return code


def _gap_override(args) -> ManualGapOverride | None:
    """フラグと理由がそろったときだけ上書きを作る。片方だけなら拒否する。"""

    if not args.override_gap:
        if args.override_reason is not None:
            raise ValueError("--override-reason was given without --override-gap")
        return None
    if not (args.override_reason or "").strip():
        raise ValueError("--override-gap requires --override-reason")
    return ManualGapOverride(reason=args.override_reason)


def _dispatch(service: ThreadsPublicationService, args) -> tuple[dict, int]:
    if args.reconcile:
        outcome = service.reconcile(proposal_id=args.proposal_id)
        print("=== threads publication reconcile ===")
        print(f"proposal_id        = {outcome.proposal_id}")
        print(f"publication_id     = {outcome.publication_id}")
        print(f"outcome            = {outcome.outcome}")
        print(f"creation_id        = {outcome.creation_id}")
        print(f"media_id           = {outcome.media_id}")
        for note in outcome.notes:
            print(f"  note: {note}")
        return outcome.as_dict(), (EXIT_UNCERTAIN if outcome.outcome == "uncertain" else EXIT_OK)

    if not args.execute:
        plan = service.plan(proposal_id=args.proposal_id, gap_override=args.gap_override)
        print("=== threads publish (PLAN) ===")
        print(f"proposal_id        = {plan.proposal_id}")
        print(f"source article     = {plan.source_article_id} {plan.source_article_title}")
        print(f"angle              = {plan.angle}")
        print(f"characters         = {plan.character_count}")
        print(f"link_mode          = {plan.link_mode}")
        print(f"destination        = {plan.destination_url or '(none)'}")
        print(f"proposal_hash      = {plan.proposal_hash[:16]}")
        print(f"proposal status    = {plan.proposal_status}")
        print(f"stale              = {plan.stale} {plan.stale_reasons or ''}")
        print(f"prior publication  = {plan.existing_publication or '(none)'}")
        print(f"threads config     = {plan.threads_config}")
        gap = plan.gap or {}
        if gap.get("last_publication_id"):
            print(
                f"posting gap        = {gap.get('minutes')} min since publication "
                f"{gap.get('last_publication_id')}; elapsed={gap.get('elapsed')}"
                + (" (OVERRIDDEN)" if gap.get("overridden") else "")
            )
        if plan.gap_overridden_reason:
            print(f"gap override       = {plan.gap_overridden_reason}")
        print(f"eligible           = {plan.ok}")
        for reason in plan.blocked_reasons:
            print(f"  BLOCKED: {reason}")
        print("\n--- 公開される文字列 (承認されたもの) ---")
        print(plan.publish_text)
        print("---")
        if plan.would_call:
            print("\nこの PLAN で --execute した場合に起きる外部呼び出し:")
            for call in plan.would_call:
                print(f"  - {call}")
        print("\nPLAN のみ。Meta には 1 度も書き込んでいない。")
        print("実際に公開するには --execute を付ける (人が明示したときだけ)。")
        return plan.as_dict(), (EXIT_OK if plan.ok else EXIT_REFUSED)

    outcome = service.publish(
        proposal_id=args.proposal_id, execute=True, gap_override=args.gap_override
    )
    print("=== threads publish ===")
    print(f"proposal_id        = {outcome.proposal_id}")
    print(f"publication_id     = {outcome.publication_id}")
    print(f"executed           = {outcome.executed}")
    print(f"outcome            = {outcome.outcome}")
    print(f"creation_id        = {outcome.creation_id}")
    print(f"media_id           = {outcome.media_id}")
    print(f"permalink          = {outcome.permalink}")
    print(f"reconciliation     = {outcome.reconciliation_required}")
    for reason in outcome.blocked_reasons:
        print(f"  BLOCKED: {reason}")
    for note in outcome.notes:
        print(f"  note: {note}")
    if outcome.error:
        print(f"  error: {outcome.error['category']}: {outcome.error['reason']}")
    if outcome.outcome == "uncertain":
        print("\n出たかどうか分からない。**再送しないこと。**")
        print(
            "  uv run python scripts/publish_threads_post.py "
            f"--proposal-id {outcome.proposal_id} --reconcile"
        )
        return outcome.as_dict(), EXIT_UNCERTAIN
    return outcome.as_dict(), (EXIT_OK if outcome.outcome == "published" else EXIT_REFUSED)


if __name__ == "__main__":
    raise SystemExit(main())
