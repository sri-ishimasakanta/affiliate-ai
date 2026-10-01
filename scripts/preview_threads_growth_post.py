"""管理用 CLI: Growth Post の下見 (2026-10-01)。**提案を保存しない・承認も公開もしない。**

    # 既定: その日の書き方の順を見るだけ (生成器を呼ばない)
    uv run python scripts/preview_threads_growth_post.py --date 2026-10-02

    # 生成器を呼んで 3 案を作り、検査する (1 案に 1 回。その日の呼び出しの記録には数えない)
    uv run python scripts/preview_threads_growth_post.py --date 2026-10-02 --generate 3

書くのは ``reports/threads/growth-previews/`` (git 管理外) だけ。DB の提案・その日の Growth の
記録・Threads には触れない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = Path("reports/threads/growth-previews")


def main(argv=None, *, session_factory=None, settings=None, client="auto", root: Path = ROOT,
         now: datetime | None = None) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True, help="the JST date of the growth post")
    parser.add_argument("--generate", type=int, default=0, choices=(0, 1, 2, 3))
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from app.operations.policy import get_policy
    from app.services.threads_growth_service import ThreadsGrowthService

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if client == "auto":
        client = None
        if args.generate:
            from app.config.settings import get_settings
            from app.services.threads_openai_provider import build_responses_client

            client = build_responses_client(settings or get_settings())
    day = date.fromisoformat(args.date)
    with session_factory() as session:
        service = ThreadsGrowthService(session, timezone=get_policy().timezone, client=client)
        previews = service.preview(day=day, count=max(args.generate, 3),
                                   generate=bool(args.generate))  # fmt: skip
    now = now or datetime.now(UTC)
    out = root / OUT
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{day.isoformat()}-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"date_jst": day.isoformat(), "generated": bool(args.generate),
                                "previews": previews}, ensure_ascii=False, indent=2,
                               default=str) + "\n", encoding="utf-8")  # fmt: skip
    for i, item in enumerate(previews, start=1):
        print(f"--- preview {i}: {item['strategy']['signature']} (topic {item.get('topic')})")
        if "body" in item:
            print(item["body"])
            print(f"ok={item['ok']} problems={item['problems']}")
    print(f"wrote {path}")
    print("preview only: no proposal saved, no approval, no publication")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
