"""管理用 CLI: プロジェクトの現在の状態を 1 つの報告にまとめる (T7。**読むだけ**)。

    uv run python scripts/generate_project_state.py                 # 手元 + 安全な読み取り
    uv run python scripts/generate_project_state.py --offline       # 外部を読まない
    uv run python scripts/generate_project_state.py --with-tests    # pytest もその場で実行
    uv run python scripts/generate_project_state.py --strict        # 契約の失敗があれば 1
    uv run python scripts/generate_project_state.py --compare reports/older.json  # 意味のある違い

``--strict`` の契約は ``app/project_state/strict.py`` (止めるべき食い違い・critical / high の
不変条件の失敗・必要な出どころが読めない・報告の形の違い、のときだけ 1)。``--compare`` は
時刻などの毎回変わる値を無視して違いを ``reports/project_state_diff_latest.{json,md}`` に書く。

出力: ``reports/project_state_latest.json`` と ``.md`` (``reports/`` は git 管理外。毎回
作り直す)。長く効く決定は ``docs/decision-log/YYYY-Www.md`` (git で追う) に、まだ無いもの
だけを足す (``--no-decision-log`` で足さない)。

読むもの: git (読み取りのコマンド)・ruff / alembic check / git diff --check (手元)・アプリの DB
(SQLite は mode=ro)・WordPress の REST (GET だけ。``--offline`` では読まない)・Windows の
スケジュールされたタスク (Get-ScheduledTask)。書かないもの: WordPress・Threads・スケジューラ・
DB。``/go/`` と Threads の API には問い合わせない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.project_state import compare, strict  # noqa: E402
from app.project_state.decision_log import append_entries  # noqa: E402
from app.project_state.generator import StateContext, build_report, write_reports  # noqa: E402


def main(argv=None, *, context: StateContext | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="WordPress などの外部を読まない")
    parser.add_argument("--with-tests", action="store_true", help="pytest をその場で実行する")
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--markdown-only", action="store_true")
    parser.add_argument("--strict", action="store_true", help="契約の失敗があれば終了コード 1")
    parser.add_argument("--compare", type=Path, help="前の報告 (JSON) と比べる")
    parser.add_argument("--no-decision-log", action="store_true")
    args = parser.parse_args(argv)
    older = None
    if args.compare:
        try:
            older = json.loads(args.compare.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"cannot read the report to compare with ({type(exc).__name__})")
            return 2

    if context is None:
        from app.config.settings import get_settings

        settings = get_settings()
        factory = None
        if not args.offline and settings.wordpress_configured:
            from app.wordpress.client import WordPressClient

            def factory():
                return WordPressClient(settings)

        context = StateContext(
            root=ROOT,
            now=datetime.now(UTC),
            database_url=settings.database_url,
            offline=args.offline,
            with_tests=args.with_tests,
            wordpress_client_factory=factory,
        )
    report = build_report(context)
    written = write_reports(
        context.root, report, json_out=not args.markdown_only, markdown_out=not args.json_only
    )
    appended = []
    if not args.no_decision_log:
        appended = append_entries(
            context.root,
            [d for d in report["decisions"] if d["supported"]],
            moment=context.now,
        )
    blocking = [w for w in report["warnings"] if w["blocking"]]
    print(f"phase: {report['project']['current_phase']}  mode: {report['mode']}")
    print(
        f"warnings: {len(report['warnings'])} ({len(blocking)} blocking); "
        f"drift: {len(report['drift'])}; next actions: {len(report['next_actions'])}"
    )
    for name, prov in report["source_freshness"].items():
        print(f"  {name:16} {prov['status']:12} {prov['source']} ({prov['freshness']})")
    for path in written:
        print(f"wrote {path}")
    if appended:
        print(f"decision log: added {', '.join(appended)}")
    if older is not None:
        diff = compare.compare(older, report)
        out = context.root / "reports"
        out.mkdir(parents=True, exist_ok=True)
        (out / "project_state_diff_latest.json").write_text(
            json.dumps(diff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out / "project_state_diff_latest.md").write_text(compare.render(diff), encoding="utf-8")
        print(compare.render(diff).rstrip())
    print("read-only: nothing was written to WordPress, Threads, the scheduler or the database")
    failures = strict.failures(report)
    if args.strict:
        for reason in failures:
            print(f"strict: {reason}")
        print(f"strict: {'FAIL' if failures else 'ok'}")
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
