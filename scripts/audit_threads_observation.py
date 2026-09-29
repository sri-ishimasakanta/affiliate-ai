"""管理用 CLI: 保存した外の観察の実行の、画面の証拠と照合を表示する (T6.5B.4、**読むだけ**)。

    uv run python scripts/audit_threads_observation.py --run 2
    uv run python scripts/audit_threads_observation.py --run 2 --json

DB を読むだけ (書かない)。Threads にもブラウザにも触れない。画面の証拠 (``visual_evidence``) は
実行の記録の ``visual_audit`` (スクロールごとの画面、T6.5B.4 以降) か、照合の記録
(``app/config/threads_observation_reviews.json``) から。**証拠があること ≠ 照らしたこと**:
``visual_verified`` は、照らして合った (``matched``) 投稿だけ。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.social.threads.observer.visual_audit import (  # noqa: E402
    load_reviews,
    run_report,
    run_review,
)

EXIT_OK = 0
EXIT_NO_RUN = 2


def render(report: dict) -> str:
    pct = "-" if report["coverage_pct"] is None else report["coverage_pct"]
    lines = [
        f"観察の実行 {report['run_id']} の画面の証拠 (読むだけ)",
        f"  受け入れた投稿: {report['accepted_posts']} 件",
        f"  画面の証拠あり: {report['posts_with_visual_evidence']} 件 / 無し: "
        f"{len(report['posts_without_visual_evidence'])} 件 (覆い {pct}%)",
        f"  スクロールごとの画面の記録: {'あり' if report['frame_audit_recorded'] else 'なし'} / "
        f"照合の記録: {'あり' if report['review_recorded'] else 'なし'}",
        f"  照合: {report['visual_crosscheck']}",
        f"  状態: {report['visual_status']}",
    ]
    for key in report["posts_without_visual_evidence"]:
        lines.append(f"  画面の証拠が無い投稿: {key}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, session_factory=None, reviews: dict | None = None
         ) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", type=int, required=True, help="観察の実行の id")
    parser.add_argument("--json", action="store_true", help="JSON で出す")
    args = parser.parse_args(argv)
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    from sqlalchemy import select

    from app.models.threads_observer import (
        ThreadsExternalObservation,
        ThreadsExternalPost,
        ThreadsObserverRun,
    )

    with session_factory() as session:
        run = session.get(ThreadsObserverRun, args.run)
        if run is None:
            print(f"run {args.run} not found")
            return EXIT_NO_RUN
        obs, post = ThreadsExternalObservation, ThreadsExternalPost
        keys = list(dict.fromkeys(session.scalars(
            select(post.external_post_key).join(obs, obs.post_id == post.id)
            .where(obs.run_id == args.run).order_by(obs.id))))  # fmt: skip
        artifacts = dict(run.artifacts_json or {})
        session.rollback()
    review = run_review(args.run, reviews if reviews is not None else load_reviews())
    report = run_report(args.run, keys, artifacts, review)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
