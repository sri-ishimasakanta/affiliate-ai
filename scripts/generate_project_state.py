"""管理用 CLI: プロジェクトの現在の状態を 1 つの報告にまとめる (T7。**読むだけ**)。

    uv run python scripts/generate_project_state.py                 # 手元 + 安全な読み取り
    uv run python scripts/generate_project_state.py --offline       # 外部を読まない
    uv run python scripts/generate_project_state.py --with-tests    # pytest もその場で実行
    uv run python scripts/generate_project_state.py --strict        # 止めるべき警告があれば 1

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
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.project_state.decision_log import append_entries  # noqa: E402
from app.project_state.generator import StateContext, build_report, write_reports  # noqa: E402


def main(argv=None, *, context: StateContext | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="WordPress などの外部を読まない")
    parser.add_argument("--with-tests", action="store_true", help="pytest をその場で実行する")
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--markdown-only", action="store_true")
    parser.add_argument("--strict", action="store_true", help="止めるべき警告があれば終了コード 1")
    parser.add_argument("--no-decision-log", action="store_true")
    args = parser.parse_args(argv)

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
        f"next actions: {len(report['next_actions'])}"
    )
    for name, prov in report["source_freshness"].items():
        print(f"  {name:16} {prov['status']:12} {prov['source']} ({prov['freshness']})")
    for path in written:
        print(f"wrote {path}")
    if appended:
        print(f"decision log: added {', '.join(appended)}")
    print("read-only: nothing was written to WordPress, Threads, the scheduler or the database")
    return 1 if args.strict and blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
