"""Google Ads の値の取り直し (C10-2)。**既定は PLAN (呼ばない・書かない)。**

    uv run python scripts/refresh_google_ads_metrics.py                        # PLAN
    uv run python scripts/refresh_google_ads_metrics.py --execute --expect-terms <N>

既存の Historical Metrics の呼び出しを、提供元ごとに一括で (1,000 語まで 1 回)。既存の Keyword は
いつもの signal (search_demand v2 / commercial_intent v2 / trend) を作る。発見の候補の語は
**Keyword にしない** (候補の ``evidence_json["google_ads"]`` に保存するだけ)。記事・計画の依頼・
Growth の承認は作らない。期待値が PLAN と違えば何も呼ばない。取り直しの実行は人の判断。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv=None, *, session_factory=None, settings=None, collection=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expect-terms", type=int, dest="expect_terms")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    if args.execute and args.expect_terms is None:
        parser.error("--execute needs --expect-terms (from the PLAN)")
    from app.services.google_ads_refresh_service import (
        GoogleAdsRefreshError,
        GoogleAdsRefreshService,
    )

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        service = GoogleAdsRefreshService(session, settings=settings, collection=collection)
        if not args.execute:
            plan = service.plan()
            print(json.dumps({k: v for k, v in plan.items()}, ensure_ascii=False, indent=2))
            print(f"PLAN: {plan['terms']} term(s) in {plan['calls_if_run']} batched call(s); "
                  "external calls = 0, database writes = 0; keywords created = 0")
            return 0
        try:
            result = service.execute(expect_terms=args.expect_terms)
        except GoogleAdsRefreshError as exc:
            print(f"refused: {exc.reason}")
            return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
