"""affiliate_opportunity の再導出と再スコアを PLAN / EXECUTE で行う CLI。

    uv run python scripts/rederive_affiliate_opportunity.py                  # PLAN (既定・write 0)
    uv run python scripts/rederive_affiliate_opportunity.py --format json    # PLAN を JSON で
    uv run python scripts/rederive_affiliate_opportunity.py --execute        # 明示したときだけ書く

catalog hygiene を適用したあと、live catalog と食い違っている affiliate_opportunity Signal を
導出し直し、**その値が変わった既存 score だけ** を付け直す。PLAN は SELECT だけ (write 0) で、
EXECUTE が保存するのと同じ計算を使って新しい値を見せる。``--execute`` は

  A. pool 全件の affiliate_opportunity を通常の service path で導出 (append-only)
  B. 「既に score がある」「その score の affiliate_opportunity と値が違う」「7 component が
     揃っている」keyword **だけ** を再スコア (対象は DB から決める。ID は直書きしない)

の順で行い、各 phase のあとに行数を検証する。想定と違えば止める (黙って続けない)。
``--expect-keywords`` / ``--expect-rescores`` を付けると、その数と違う PLAN では何も書かない。
冪等: 変わるものが無ければ write 0 で ``already current`` を返す。

scoring の式 / 重み / fit policy / catalog / article / link / snapshot は一切触らない。
外部 API / LLM / network は使わない。secret / URL / customer ID は出力しない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.services.affiliate_signal_transition_service import (  # noqa: E402
    DECISION_BLOCKED_INCOMPLETE,
    DECISION_NEVER_SCORED,
    DECISION_RESCORE,
    DECISION_UNCHANGED,
    AffiliateSignalTransitionService,
    KeywordOutcome,
    TransitionPlan,
    TransitionRefusedError,
    TransitionResult,
)

EXIT_OK = 0
EXIT_REFUSED = 1  # 期待値と違う / 付け直せない score がある (何も書いていない)
EXIT_UNEXPECTED = 3

_LABEL = {
    DECISION_RESCORE: "RESCORE (affiliate value changed)",
    DECISION_UNCHANGED: "unchanged (scored, same affiliate value)",
    DECISION_NEVER_SCORED: "derive only (never scored)",
    DECISION_BLOCKED_INCOMPLETE: "BLOCKED (scored but missing required signals)",
}


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _outcome_dict(o: KeywordOutcome) -> dict[str, object]:
    return {
        "keyword_id": o.keyword_id,
        "keyword": o.keyword,
        "decision": o.decision,
        "current_signal_value": o.current_signal_value,
        "current_score_value": o.current_score_value,
        "proposed_value": o.proposed_value,
        "signal_changed": o.signal_changed,
        "score_value_changed": o.value_changed,
        "current_total": o.current_total,
        "proposed_total": o.proposed_total,
        "missing_components": list(o.missing_components),
    }


def _summary(plan: TransitionPlan) -> dict[str, object]:
    return {
        "keywords_considered": plan.keyword_count,
        "signal_values_changing": plan.signal_value_changes,
        "rescore": len(plan.rescore),
        "unchanged_scored": len(plan.unchanged_scored),
        "never_scored": len(plan.never_scored),
        "blocked": len(plan.blocked),
        "rescore_keyword_ids": list(plan.rescore_ids),
        "expected_signal_inserts": plan.expected_signal_inserts,
        "expected_score_inserts": plan.expected_score_inserts,
        "expected_score_signal_inserts": plan.expected_score_signal_inserts,
        "has_work": plan.has_work,
    }


def _print_table(plan: TransitionPlan, *, result: TransitionResult | None) -> None:
    mode = "EXECUTE" if result is not None else "PLAN (default: zero writes)"
    print(f"=== affiliate_opportunity transition - {mode} ===")
    s = _summary(plan)
    print(
        f"keywords={s['keywords_considered']} signal_values_changing="
        f"{s['signal_values_changing']} rescore={s['rescore']} "
        f"unchanged_scored={s['unchanged_scored']} never_scored={s['never_scored']} "
        f"blocked={s['blocked']}"
    )
    print(
        f"expected inserts: signals={s['expected_signal_inserts']} "
        f"scores={s['expected_score_inserts']} "
        f"score_signals={s['expected_score_signal_inserts']}"
    )

    print("\n-- re-score set (scored keywords whose affiliate value changes) --")
    if not plan.rescore:
        print("  (none)")
    for o in plan.rescore:
        print(
            f"  [{o.keyword_id:>3}] {o.keyword:28} affiliate "
            f"{_fmt(o.current_score_value)} -> {_fmt(o.proposed_value)} | total "
            f"{_fmt(o.current_total)} -> {_fmt(o.proposed_total)}"
        )

    for decision in (DECISION_BLOCKED_INCOMPLETE, DECISION_UNCHANGED, DECISION_NEVER_SCORED):
        rows = [o for o in plan.outcomes if o.decision == decision]
        if not rows:
            continue
        print(f"\n-- {_LABEL[decision]} ({len(rows)}) --")
        for o in rows:
            extra = ""
            if o.missing_components:
                extra = f" | missing: {', '.join(o.missing_components)}"
            marker = " *signal value changes*" if o.signal_changed else ""
            print(
                f"  [{o.keyword_id:>3}] {o.keyword:28} affiliate "
                f"{_fmt(o.current_signal_value)} -> {_fmt(o.proposed_value)}{extra}{marker}"
            )

    changes = plan.rank_changes
    print(f"\n-- expected rank changes among scored keywords ({len(changes)}) --")
    if not changes:
        print("  (ranking unchanged)")
    for keyword_id, keyword, before, after in changes:
        print(f"  [{keyword_id:>3}] {keyword:28} rank {before} -> {after}")

    missing_any = sorted(
        {c for o in plan.outcomes if o.has_score for c in o.missing_components}
    )
    print(
        "\nall required non-affiliate signals present for every scored keyword: "
        f"{not missing_any}" + (f" (missing: {', '.join(missing_any)})" if missing_any else "")
    )

    if result is None:
        if plan.has_work:
            print("\nNothing was written. Re-run with --execute to apply.")
        else:
            print("\nAlready current: nothing would change. --execute would be a no-op.")
        return

    if result.already_current:
        print("\nalready current: nothing was written (no duplicate history created).")
        return
    print(
        f"\nwritten: {result.inserted_signals} affiliate signal(s), "
        f"{result.inserted_scores} score(s), "
        f"{result.inserted_score_signals} score-signal link(s)"
    )
    print(f"re-scored keyword ids: {list(result.rescored_keyword_ids)}")


def run(
    *,
    execute: bool = False,
    output_format: str = "table",
    expect_keywords: int | None = None,
    expect_rescores: int | None = None,
    allow_blocked: bool = False,
    session_factory=SessionLocal,
) -> int:
    with session_factory() as session:
        service = AffiliateSignalTransitionService(session)
        result: TransitionResult | None = None
        try:
            if execute:
                result = service.execute(
                    expect_keywords=expect_keywords,
                    expect_rescores=expect_rescores,
                    allow_blocked=allow_blocked,
                )
                plan = result.plan
            else:
                plan = service.plan()
                service.assert_expectations(  # PLAN でも期待値は検証する
                    plan,
                    expect_keywords=expect_keywords,
                    expect_rescores=expect_rescores,
                    allow_blocked=True,  # PLAN は blocked を報告するだけ (書かないので拒否しない)
                )
        except TransitionRefusedError as exc:
            print(f"REFUSED (nothing was written): {exc}", file=sys.stderr)
            return EXIT_REFUSED

    if output_format == "json":
        payload = {
            "mode": "execute" if execute else "plan",
            "summary": _summary(plan),
            "keywords": [_outcome_dict(o) for o in plan.outcomes],
            "rank_changes": [
                {"keyword_id": k, "keyword": t, "current_rank": a, "proposed_rank": b}
                for k, t, a, b in plan.rank_changes
            ],
            "written": (
                {
                    "already_current": result.already_current,
                    "signals": result.inserted_signals,
                    "scores": result.inserted_scores,
                    "score_signals": result.inserted_score_signals,
                    "rescored_keyword_ids": list(result.rescored_keyword_ids),
                }
                if result is not None
                else None
            ),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(plan, result=result)

    if not execute and plan.blocked:
        return EXIT_REFUSED
    return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rederive_affiliate_opportunity",
        description=(
            "affiliate_opportunity を再導出し、値が変わった既存 score だけを付け直す "
            "(PLAN 既定 / --execute で適用)"
        ),
    )
    parser.add_argument("--execute", action="store_true", help="明示したときだけ DB へ書く")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument(
        "--expect-keywords",
        type=int,
        metavar="N",
        help="pool がこの件数でなければ何も書かない (transition 固有の guard)",
    )
    parser.add_argument(
        "--expect-rescores",
        type=int,
        metavar="N",
        help="再スコア対象がこの件数でなければ何も書かない (transition 固有の guard)",
    )
    parser.add_argument(
        "--allow-blocked",
        action="store_true",
        help="付け直せない score (signal 不足) があっても導出だけは進める",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        # SessionLocal は default 引数ではなく呼び出し時に解決する。
        return run(
            execute=args.execute,
            output_format=args.format,
            expect_keywords=args.expect_keywords,
            expect_rescores=args.expect_rescores,
            allow_blocked=args.allow_blocked,
            session_factory=SessionLocal,
        )
    except Exception as exc:  # noqa: BLE001 - 値を出さず種別だけ報告する
        print(f"unexpected error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
