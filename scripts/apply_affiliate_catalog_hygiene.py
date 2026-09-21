"""Affiliate catalog の match_terms hygiene を PLAN / EXECUTE で適用する CLI。

    uv run python scripts/apply_affiliate_catalog_hygiene.py                 # PLAN (既定・write 0)
    uv run python scripts/apply_affiliate_catalog_hygiene.py --format json   # PLAN を JSON で
    uv run python scripts/apply_affiliate_catalog_hygiene.py --execute       # 明示したときだけ書く

宣言は ``app/config/affiliate_catalog_hygiene.json`` (version 管理)。PLAN は SELECT だけ (write 0)。
``--execute`` は全 program の現在の match_terms が宣言の ``before`` と完全一致するときだけ、1
transaction で適用する (drift / program 不在が 1 件でもあれば何も書かず exit 1)。冪等: 適用済みの
再実行は write 0。``match_terms`` 以外 (score / signal / article / link / status / URL) は触らない。
外部 API / LLM / network は使わない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.affiliate.catalog_hygiene import (  # noqa: E402
    DEFAULT_HYGIENE_PATH,
    STATUS_APPLIED,
    STATUS_DRIFT,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    HygieneConfigError,
    load_hygiene_config,
)
from app.config.database import SessionLocal  # noqa: E402
from app.services.affiliate_catalog_hygiene_service import (  # noqa: E402
    AffiliateCatalogHygieneService,
    HygieneOutcome,
    HygienePlan,
    HygieneRefusedError,
)

EXIT_OK = 0
EXIT_REFUSED = 1  # drift / program 不在 (PLAN では「適用できない」の意味)
EXIT_CONFIG = 2
EXIT_UNEXPECTED = 3

_LABEL = {
    STATUS_PENDING: "PENDING (would remove)",
    STATUS_APPLIED: "ALREADY APPLIED (no-op)",
    STATUS_DRIFT: "DRIFT (current terms differ from the declared before; NOT applied)",
    STATUS_NOT_FOUND: "PROGRAM NOT FOUND (NOT applied)",
}


def _outcome_dict(o: HygieneOutcome) -> dict[str, object]:
    return {
        "id": o.change.id,
        "program": o.change.program,
        "provider": o.change.provider,
        "program_id": o.program_id,
        "status": o.status,
        "current": list(o.current) if o.current is not None else None,
        "before": list(o.change.before),
        "remove": list(o.change.remove),
        "after": list(o.change.after),
        "reason": o.change.reason,
        "evidence": o.change.evidence,
    }


def _summary(plan: HygienePlan) -> dict[str, object]:
    return {
        "changes": len(plan.outcomes),
        "pending": plan.pending,
        "already_applied": plan.already_applied,
        "blocked": len(plan.blocked),
        "terms_to_remove": plan.terms_to_remove,
        "executable": plan.executable,
    }


def _print_table(plan: HygienePlan, *, executed: tuple[str, ...] | None) -> None:
    mode = "EXECUTE" if executed is not None else "PLAN (default: zero writes)"
    print(f"=== Affiliate catalog hygiene - {mode} ===")
    s = _summary(plan)
    print(
        f"changes={s['changes']} pending={s['pending']} already_applied={s['already_applied']} "
        f"blocked={s['blocked']} terms_to_remove={s['terms_to_remove']}"
    )
    for o in plan.outcomes:
        head = f"[{o.change.id}] {o.change.program} (provider={o.change.provider})"
        print(f"\n{head} - {_LABEL[o.status]}")
        if o.status == STATUS_DRIFT and o.current is not None:
            print(f"    current: {list(o.current)}")
        print(f"    remove : {list(o.change.remove)}")
        print(f"    before : {list(o.change.before)}")
        print(f"    after  : {list(o.change.after)}")
        print(f"    reason : {o.change.reason}")
    if executed is not None:
        print(f"\napplied: {len(executed)} change(s): {', '.join(executed) or '-'}")
    elif plan.executable and plan.pending:
        print("\nNothing was written. Re-run with --execute to apply (all-or-nothing).")
    elif not plan.executable:
        print("\nNOT executable: resolve the drift / missing program above. Nothing was written.")


def run(
    *,
    config_path: str | Path = DEFAULT_HYGIENE_PATH,
    execute: bool = False,
    output_format: str = "table",
    session_factory=SessionLocal,
) -> int:
    try:
        spec = load_hygiene_config(config_path)
    except HygieneConfigError as exc:
        for error in exc.errors:
            print(f"hygiene config error: {error}", file=sys.stderr)
        return EXIT_CONFIG

    with session_factory() as session:
        service = AffiliateCatalogHygieneService(session)
        if not execute:
            plan = service.plan(spec)
            executed = None
        else:
            try:
                result = service.execute(spec)
            except HygieneRefusedError as exc:
                print(f"REFUSED (nothing was written): {exc}", file=sys.stderr)
                _print_table(service.plan(spec), executed=None)
                return EXIT_REFUSED
            plan, executed = result.plan, result.applied

    if output_format == "json":
        payload = {
            "mode": "execute" if execute else "plan",
            "summary": _summary(plan),
            "changes": [_outcome_dict(o) for o in plan.outcomes],
            "applied": list(executed) if executed is not None else [],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(plan, executed=executed)
    if not execute and not plan.executable:
        return EXIT_REFUSED
    return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apply_affiliate_catalog_hygiene",
        description="match_terms の宣言的な削除を PLAN (既定) / --execute で適用する",
    )
    parser.add_argument("--config", default=str(DEFAULT_HYGIENE_PATH), metavar="PATH")
    parser.add_argument("--execute", action="store_true", help="明示したときだけ DB へ書く")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return run(config_path=args.config, execute=args.execute, output_format=args.format)
    except Exception as exc:  # noqa: BLE001 - 値 (URL / credential) を出さず種別だけ報告する
        print(f"unexpected error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
