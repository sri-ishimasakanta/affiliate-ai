"""管理用 CLI: 適用済み変更の before/after 観測 (C9.3)。

    uv run python scripts/report_change_effects.py --window-days 28
    uv run python scripts/report_change_effects.py --request-id 1 --json effects.json

read-only。DB にも WordPress にも書かない。

**因果は主張しない。** 出力は「同じ長さの窓の同じ指標を並べたもの」+「それを
信じてよいか」だけで、成熟していなければ ``insufficient_data`` と明示する。

暦日の境界は運用ポリシーのタイムゾーン (本番では ``Asia/Tokyo``) で決まる。
保存されている適用時刻は UTC のままで、表示と窓の計算で変換するだけである。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.change.effect import (  # noqa: E402
    DEFAULT_MINIMUM_IMPRESSIONS,
    DEFAULT_WINDOW_DAYS,
)
from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.change_effect_service import ChangeEffectService  # noqa: E402

EXIT_OK = 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--minimum-impressions", type=int, default=DEFAULT_MINIMUM_IMPRESSIONS)
    parser.add_argument("--request-id", type=int, help="1 件の change request に絞る")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    with SessionLocal() as session:
        report = ChangeEffectService(session, settings=settings).build(
            window_days=args.window_days,
            minimum_impressions=args.minimum_impressions,
            request_id=args.request_id,
        )

    print("=== change effects ===")
    print(f"generated_at        = {report.generated_at}")
    print(f"reporting timezone  = {report.reporting_timezone}")
    print(f"window_days         = {report.window_days}")
    print(f"gsc coverage        = {report.gsc_coverage_through}")
    print(f"ga4 coverage        = {report.ga4_coverage_through}")
    print(f"trusted clicks from = {report.trusted_click_measurement_start_at}")
    print(f"applied changes     = {len(report.effects)}")
    for note in report.notes:
        print(f"  note: {note}")

    for effect in report.effects:
        print(
            f"\n-- application {effect.change_application_id} (request {effect.change_request_id})"
        )
        print(f"   article        = {effect.article_id} {effect.article_url}")
        print(
            f"   change_date    = {effect.change_date} ({effect.reporting_timezone})"
            f"  type={effect.change_type}"
        )
        print(f"   applied_at     = {effect.applied_at} (UTC stored)")
        print(f"   pre  window    = {effect.pre_window['start']} .. {effect.pre_window['end']}")
        print(f"   post window    = {effect.post_window['start']} .. {effect.post_window['end']}")
        print(f"   maturity       = {effect.maturity['status']} {effect.maturity['reasons']}")
        print(f"   causal_claim   = {effect.causal_claim}")
        print(f"   pre            = {effect.pre}")
        print(f"   post           = {effect.post}")
        print(f"   deltas         = {effect.deltas}")
        for caveat in effect.caveats:
            print(f"   caveat: {caveat}")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON written to {args.json_path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
