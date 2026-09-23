"""管理用 CLI: 公開済み Threads 投稿の指標を取り込む (T4)。

    # 既定は PLAN。Meta には 1 度も問い合わせない
    uv run python scripts/import_threads_insights.py

    # 実際に取得して観測を 1 行積む
    uv run python scripts/import_threads_insights.py --execute

    # これまでの観測から言えることだけを出す (取得もしない)
    uv run python scripts/import_threads_insights.py --report

**読むだけのコマンドである。** 投稿もしないし、提案の文面も状態も触らない。
Threads への書き込み呼び出しは常に 0 件。

取得できなかった指標は 0 で埋めない。「観測できなかった」と「0 だった」は別物で、
これを混ぜると公開直後の投稿が「成績の悪い投稿」に化ける。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.threads_insights_service import (  # noqa: E402
    ThreadsInsightsError,
    ThreadsInsightsService,
)

EXIT_OK = 0
EXIT_REFUSED = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication-id", type=int, default=None)
    parser.add_argument("--execute", action="store_true", help="実際に取得する (既定は PLAN)")
    parser.add_argument("--report", action="store_true", help="既存の観測だけから所見を出す")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    with SessionLocal() as session:
        service = ThreadsInsightsService(session, settings=settings)
        try:
            payload = _dispatch(service, args)
        except ThreadsInsightsError as exc:
            print(f"refused: {exc.reason}")
            return EXIT_REFUSED

        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\nJSON written to {args.json_path}")
    return EXIT_OK


def _dispatch(service: ThreadsInsightsService, args) -> dict:
    if args.report:
        return _print_report(service.report())
    if not args.execute:
        return _print_plan(service.plan(publication_id=args.publication_id))
    outcome = service.collect(publication_id=args.publication_id, execute=True)
    print("=== threads insights import ===")
    print(f"checked   = {outcome.checked}")
    print(f"imported  = {outcome.imported}")
    print(f"unchanged = {outcome.unchanged}")
    print(f"failed    = {outcome.failed}")
    for detail in outcome.details:
        print(f"  - publication {detail['publication_id']}: {detail['result']}")
        if detail.get("values"):
            for name, value in detail["values"].items():
                print(f"      {name:10}= {value}")
        if detail.get("missing"):
            print(f"      missing (0 ではなく未観測): {', '.join(detail['missing'])}")
        if detail.get("reason"):
            print(f"      reason: {detail['reason']}")
    print("\n読み取りのみ。Threads への書き込み呼び出し = 0。")
    return outcome.as_dict()


def _print_plan(plan: dict) -> dict:
    print("=== threads insights import (PLAN) ===")
    print(f"policy_version     = {plan['policy_version']}")
    print(f"supported metrics  = {', '.join(plan['supported_metrics'])}")
    print(f"unsupported        = {', '.join(plan['unsupported_metrics'])} (公式に存在しない)")
    print(f"threads config     = {plan['threads_config']}")
    print(f"threads writes     = {plan['threads_writes']}")
    print(f"publications       = {len(plan['publications'])}")
    for row in plan["publications"]:
        print(f"\n  publication {row['publication_id']} (media {row['media_id']})")
        print(f"    published_at   = {row['published_at']}")
        print(f"    age            = {row['maturity']['age_hours']}h")
        print(f"    maturity       = {row['maturity']['stage']}")
        print(f"    comparable     = {row['maturity']['comparable']}")
        for note in row["maturity"]["notes"]:
            print(f"      note: {note}")
        last = row["last_snapshot"]
        print(f"    last snapshot  = {last['observed_at'] if last else '(none yet)'}")
    print("\nPLAN のみ。Meta には 1 度も問い合わせていない。")
    print("実際に取得するには --execute を付ける。")
    return plan


def _print_report(report: dict) -> dict:
    print("=== threads performance report ===")
    print(f"generated_at   = {report['generated_at']}")
    print(f"policy_version = {report['policy_version']}")
    print(f"publications   = {report['publication_count']} (比較可能: {report['mature_count']})")
    print(f"比較に必要な本数 = {report['minimum_mature_posts_per_dimension']} / 次元")

    for post in report["publications"]:
        print(f"\n  publication {post['publication_id']} / article {post['article_id']}")
        print(
            f"    angle={post['angle']} link_mode={post['link_mode']} "
            f"length={post['character_count']}文字 ({post['length_bucket']})"
        )
        print(f"    maturity   = {post['maturity']['stage']} ({post['maturity']['age_hours']}h)")
        print(f"    observed   = {post['observed_at'] or '(未取得)'}")
        for name, value in post["metrics"].items():
            print(f"      {name:10}= {'(未観測)' if value is None else value}")
        ratio = post["interactions_per_view"]
        print(f"    interactions/view = {'(判定しない)' if ratio is None else ratio}")
        print(f"    observations = {', '.join(post['observations']) or '(なし)'}")

    for label, key in (
        ("角度", "by_angle"),
        ("リンク有無", "by_link_mode"),
        ("長さ", "by_length_bucket"),
        ("公開時刻(UTC)", "by_published_hour_utc"),
        ("曜日", "by_published_weekday"),
    ):
        groups = report[key]
        print(f"\n  --- {label} ---")
        if not groups:
            print("    比較できる投稿がまだ無い")
            continue
        for value, data in groups.items():
            note = f" / {data['note']}" if data["note"] else ""
            print(f"    {value}: n={data['sample']} views(median)={data['median_views']}{note}")

    attribution = report["website_attribution"]
    print("\n  --- サイト側の帰属 ---")
    print(f"    リンク付き投稿 = {len(attribution['linked_publications'])}")
    if attribution["unavailable_reason"]:
        print(f"    利用不可: {attribution['unavailable_reason']}")
    for limit in attribution["limitations"]:
        print(f"    注意: {limit}")

    print("\n  --- 次に試すこと ---")
    for rec in report["recommendations"]:
        print(f"    [{rec['type']}] {rec['reason']}")
        print(f"      -> {rec['action']}")

    print("\n  --- 前提 ---")
    for caveat in report["caveats"]:
        print(f"    {caveat}")
    return report


if __name__ == "__main__":
    raise SystemExit(main())
