"""管理用 CLI: C11 の ASP・提携案件・リンクの棚卸し (2026-10-01)。**読むだけ。外に問い合わせない。**

    uv run python scripts/affiliate_inventory.py providers      # 提供元ごと
    uv run python scripts/affiliate_inventory.py programs       # 案件ごと
    uv run python scripts/affiliate_inventory.py program 5      # 1 つの案件
    uv run python scripts/affiliate_inventory.py coverage       # 記事ごとの収益の導線
    uv run python scripts/affiliate_inventory.py missing-links  # 案件はあるがリンクの無い記事など
    uv run python scripts/affiliate_inventory.py stale          # 一度も確かめていない・古い確認
    uv run python scripts/affiliate_inventory.py attribution    # 帰属のできる度合い
    uv run python scripts/affiliate_inventory.py actions        # 人が ASP でする作業の一覧
    uv run python scripts/affiliate_inventory.py ops            # 日々の運用で見るもの
    uv run python scripts/affiliate_inventory.py report [--out reports/affiliate/inventory.json]

    # 人が ASP の画面で確かめた事実を足す (既定は PLAN。--execute で書く)
    uv run python scripts/affiliate_inventory.py verify 5 --evidence provider_dashboard
        --status approved
        --cookie-window-days 90 --source "PartnerStack dashboard" --by human
        --observed-at 2026-10-02T10:00:00+09:00 [--execute]

出力にトラッキング URL・token の中身・秘密は出さない (有る / 無い だけ)。リンクを作らない・
置き換えない・パラメータを足さない。``verify`` は ``data/affiliate/`` (git 管理外) に追記するだけ。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

READ_COMMANDS = ("providers", "programs", "coverage", "missing-links", "stale", "attribution",
                 "actions", "ops", "report")  # fmt: skip


def _bool(text: str) -> bool:
    if text not in ("true", "false"):
        raise argparse.ArgumentTypeError("true or false")
    return text == "true"


def _tri(text: str):
    """true / false / unknown (画面で見たが分からなかった)。書かなければ記録しない。"""

    if text not in ("true", "false", "unknown"):
        raise argparse.ArgumentTypeError("true, false or unknown")
    return {"true": True, "false": False}.get(text, "unknown")


def _parser() -> argparse.ArgumentParser:
    from app.revenue.affiliate_inventory import (
        COMMISSION_TYPES,
        EVIDENCE_KINDS,
        NOTICE_STATES,
        PROVIDER_STATUSES,
        REAPPLICATION_PLANS,
        REJECTION_REASONS,
    )

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in READ_COMMANDS:
        cmd = sub.add_parser(name)
        cmd.add_argument("--max-verification-age-days", type=int,
                         help="judge a verification as stale after this many days (unset: no "
                              "staleness judgement, only never-verified)")
        if name == "report":
            cmd.add_argument("--out")
    show = sub.add_parser("program")
    show.add_argument("program_id", type=int)
    ver = sub.add_parser("verify")
    ver.add_argument("program_id", type=int)
    ver.add_argument("--evidence", choices=EVIDENCE_KINDS, required=True,
                     help="provider_dashboard / provider_email: checked now at the provider; "
                          "human_recollection: remembered (never shown as provider-verified)")
    ver.add_argument("--status", choices=PROVIDER_STATUSES)
    ver.add_argument("--account-registered", type=_tri)
    ver.add_argument("--applied-on", help="YYYY-MM-DD")
    ver.add_argument("--decided-on", help="YYYY-MM-DD")
    ver.add_argument("--rejection-reason", choices=REJECTION_REASONS)
    ver.add_argument("--reapply-allowed", type=_tri)
    ver.add_argument("--reapplication-plan", choices=REAPPLICATION_PLANS)
    ver.add_argument("--provider-program-id")
    ver.add_argument("--link-id")
    ver.add_argument("--actual-provider", help="the platform actually used (e.g. Impact)")
    ver.add_argument("--subid-supported", type=_tri)
    ver.add_argument("--click-reporting", type=_tri)
    ver.add_argument("--conversion-reporting", type=_tri)
    ver.add_argument("--source-attribution", type=_tri,
                     help="conversions come back with the article / source reference")
    ver.add_argument("--cookie-window-days", type=int)
    ver.add_argument("--tracking-url-obtained", type=_bool)
    ver.add_argument("--commission-terms-confirmed", type=_bool)
    ver.add_argument("--commission-type", choices=COMMISSION_TYPES)
    ver.add_argument("--commission-value", type=float)
    ver.add_argument("--commission-currency")
    ver.add_argument("--landing-host", help="host name only (no URL)")
    ver.add_argument("--pause-end-notice", choices=NOTICE_STATES)
    ver.add_argument("--notice-effective-date", help="YYYY-MM-DD")
    ver.add_argument("--source", required=True)
    ver.add_argument("--by", required=True)
    ver.add_argument("--observed-at", required=True)
    ver.add_argument("--execute", action="store_true")
    return parser


def _select(report: dict, command: str) -> object:
    ops = report["operations"]
    if command == "providers":
        return {"counts": report["counts"], "providers": report["providers"]}
    if command == "programs":
        return report["programs"]
    if command == "coverage":
        return {"counts": report["counts"]["articles"], "articles": report["coverage"]}
    if command == "missing-links":
        return {k: ops[k] for k in ("catalog_active_without_tracking_url",
                                    "tracking_url_without_placement",
                                    "articles_without_monetization_path",
                                    "articles_with_unknown_program_status",
                                    "linked_articles_with_unlinked_programs")} | {
            "articles_program_without_link": [aid for aid, c in report["coverage"].items()
                                              if c["state"] == "program_without_link"]}
    if command == "stale":
        return {k: ops[k] for k in ("never_verified_programs", "stale_verification_programs",
                                    "paused_or_ended_programs", "unknown_status_programs",
                                    "articles_needing_replacement")}
    if command == "attribution":
        return {"counts": report["counts"]["attribution"],
                "programs": [{k: r[k] for k in ("id", "name", "provider", "attribution",
                                                "attribution_reason")}
                             for r in report["programs"]],
                "subid_design": report["subid_design"]}
    if command == "actions":
        from app.revenue.affiliate_inventory import PRIORITY_RULES

        return {"priority_rules": PRIORITY_RULES, "items": report["human_action_queue"]}
    if command == "ops":
        return ops
    return report


def main(argv=None, *, session_factory=None, now: datetime | None = None,
         verifications_path: Path | None = None) -> int:  # fmt: skip
    from app.services.affiliate_inventory_service import (
        VERIFICATIONS_PATH,
        AffiliateInventoryService,
        InventoryError,
    )

    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    now = now or datetime.now(UTC)
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        service = AffiliateInventoryService(session,
                                            verifications_path=verifications_path
                                            or VERIFICATIONS_PATH)  # fmt: skip
        try:
            if args.command == "verify":
                fields = {"status_at_provider": args.status,
                          "provider_program_id": args.provider_program_id,
                          "link_id": args.link_id, "actual_provider": args.actual_provider,
                          "subid_supported": args.subid_supported,
                          "click_reporting_supported": args.click_reporting,
                          "conversion_reporting_supported": args.conversion_reporting,
                          "content_source_attribution_supported": args.source_attribution,
                          "cookie_window_days": args.cookie_window_days,
                          "tracking_url_obtained": args.tracking_url_obtained,
                          "commission_terms_confirmed": args.commission_terms_confirmed,
                          "commission_type_observed": args.commission_type,
                          "commission_value_observed": args.commission_value,
                          "commission_currency_observed": args.commission_currency,
                          "landing_host_observed": args.landing_host,
                          "pause_end_notice": args.pause_end_notice,
                          "notice_effective_date": args.notice_effective_date,
                          "account_registered": args.account_registered,
                          "applied_on": args.applied_on, "decided_on": args.decided_on,
                          "rejection_reason": args.rejection_reason,
                          "reapply_allowed": args.reapply_allowed,
                          "reapplication_plan": args.reapplication_plan}
                result = service.verify(program_id=args.program_id, source=args.source,
                                        evidence_kind=args.evidence,
                                        verified_by=args.by, observed_at=args.observed_at,
                                        fields=fields, execute=args.execute, now=now)
            else:
                report = service.inventory(now=now, verification_max_age_days=getattr(
                    args, "max_verification_age_days", None))  # fmt: skip
                if args.command == "program":
                    result = next((r for r in report["programs"] if r["id"] == args.program_id),
                                  None)
                    if result is None:
                        raise InventoryError(f"program {args.program_id} does not exist")
                else:
                    result = _select(report, args.command)
        except InventoryError as exc:
            print(f"refused: {exc}")
            return 2
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if getattr(args, "out", None):
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)
    print("read-only: nothing was sent to any provider; no link was created or replaced"
          if args.command != "verify" else "local only: nothing was sent to any provider")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
